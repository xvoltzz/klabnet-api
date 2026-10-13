import asyncio
import re
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_username
from ..config import AWAY_AFTER, PRESENCE_TTL
from ..db import get_db
from . import session

router = APIRouter()

# The latest beat from each of someone's devices (username -> device ->
# beat), for choosing which one their card shows. Only this process's
# memory: a beat is stale in PRESENCE_TTL seconds anyway.
_beats: dict[str, dict[str, dict]] = {}


def _shown(username: str, device: str, beat: dict) -> dict:
    """The beat someone's card shows, now that this one has arrived.

    Presence is one row per person, and every device they have klabnet
    open on used to write its own song over it: playing on one and idle on
    another, the card flipped between the two every few seconds. Now the
    row is always the device that's really playing: the one their session
    says is active (routers/session.py) while it plays, else one playing
    its own music, else the active one paused, else one with a song
    loaded, else the one they touched last. A device that's only showing
    what another is playing (`remote`) comes after any that isn't.

    The phone apps aren't in the session and send no device id: together
    they're one device, "", ranked like any other by what it's playing."""
    _beats.setdefault(username, {})[device] = beat
    return _best(username, device)


def _best(username: str, keep: str = "") -> dict | None:
    """The beat to show of those still current (`keep`: one just heard from)."""
    now = time.time()
    devices = _beats.get(username, {})
    # Stale ones go, and so do ones whose page is known to have closed: a
    # device that was playing when it went mustn't outrank the others.
    for d in [d for d, b in devices.items()
              if now - b["at"] > PRESENCE_TTL or (d != keep and session.device_gone(username, d))]:
        del devices[d]
    if not devices:
        _beats.pop(username, None)
        return None
    active = session.active_device(username)

    def rank(item):
        d, b = item
        own = not b["remote"]
        mine = bool(active) and d == active
        return (mine and b["playing"], own and b["playing"], mine, own and bool(b["song"]), own, b["used"], b["at"])

    best = max(devices.items(), key=rank)[1]
    # A listening party is joined on one device; whichever it is, it shows.
    party = best["party_host"] or next((b["party_host"] for b in devices.values() if b["party_host"]), "")
    return dict(best, party_host=party)


def _unauthenticated() -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": "not authenticated"})


@router.post("/api/presence")
async def post_presence(request: Request):
    """Called by the dashboard every ~8s to broadcast what you're listening to."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})

    song       = str(body.get("song",   "")).strip()[:200]
    artist     = str(body.get("artist", "")).strip()[:200]
    song_id    = str(body.get("songId", "")).strip()[:100]
    playing    = bool(body.get("playing", False))
    party_host = str(body.get("partyHost", "")).strip()[:100]
    # Which of their devices this is (a random id per open page), and
    # whether it's only showing what another one is playing. Older pages
    # send neither and count as one device.
    device     = re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("device", "")))[:40]
    remote     = bool(body.get("remote", False))
    # What they're on, for the icon on their card: "app:windows",
    # "pwa:ios", "web:macos:firefox". Just letters and colons.
    platform   = re.sub(r"[^a-z:]", "", str(body.get("platform", "")).lower())[:40]
    # Away. activeAgo: seconds since this device last saw its person use
    # it (keyboard, mouse, touch), or absent if it can't tell. idleKnown:
    # it sees the whole computer (the desktop app asking the OS, Chrome's
    # idle detection), not just input inside klabnet, so a big activeAgo
    # really means they're away rather than working in another window.
    try:
        active_ago = body.get("activeAgo")
        active_ago = None if active_ago is None else max(0, min(int(active_ago), 30 * 86400))
    except (TypeError, ValueError):
        active_ago = None
    idle_known = bool(body.get("idleKnown", False)) and active_ago is not None
    # lockedFor: the screen's been locked this many seconds. Away at once,
    # unless they've used another device since it locked.
    try:
        locked_for = body.get("lockedFor")
        locked_for = None if locked_for is None else max(0, min(int(locked_for), 30 * 86400))
    except (TypeError, ValueError):
        locked_for = None

    now = time.time()
    shown = _shown(username, device, {
        "song": song, "artist": artist, "song_id": song_id, "playing": playing, "party_host": party_host,
        "platform": platform, "remote": remote, "at": now,
        "used": now - active_ago if active_ago is not None else 0.0,
    })
    # The busiest write there is (every ~8s from every open tab): off the
    # event loop, one connection and one commit.
    await asyncio.to_thread(
        _save_presence, username, shown["song"], shown["artist"], shown["song_id"], shown["playing"],
        shown["party_host"], shown["platform"], active_ago, idle_known, locked_for)
    return {"ok": True}


def _save_presence(username, song, artist, song_id, playing, party_host, platform, active_ago=None, idle_known=False, locked_for=None):
    now = datetime.utcnow()
    fmt = "%Y-%m-%d %H:%M:%S"
    active_at = "" if active_ago is None else (now - timedelta(seconds=active_ago)).strftime(fmt)
    known_at = now.strftime(fmt) if idle_known else ''
    locked_at = "" if locked_for is None else (now - timedelta(seconds=locked_for)).strftime(fmt)
    with get_db() as db:
        db.execute(
            """INSERT INTO users(username) VALUES(?)
               ON CONFLICT(username) DO UPDATE SET last_seen=datetime('now')""",
            (username,),
        )
        db.execute(
            """INSERT INTO presence(username, song, artist, song_id, playing, party_host, platform, updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(username) DO UPDATE SET
                   song       = excluded.song,
                   artist     = excluded.artist,
                   song_id    = excluded.song_id,
                   playing    = excluded.playing,
                   party_host = excluded.party_host,
                   platform   = excluded.platform,
                   updated    = excluded.updated""",
            (username, song, artist, song_id, 1 if playing else 0, party_host, platform),
        )
        # The latest activity any of their devices has seen wins (strings
        # in this format compare in time order): busy on the phone isn't
        # away because the PC's been idle.
        if active_at:
            db.execute(
                "UPDATE presence SET active_at = MAX(active_at, ?) WHERE username = ?",
                (active_at, username),
            )
        if known_at:
            db.execute("UPDATE presence SET idle_known_at = ? WHERE username = ?", (known_at, username))
        if locked_at:
            db.execute(
                "UPDATE presence SET locked_at = ?, lock_seen_at = ? WHERE username = ?",
                (locked_at, now.strftime(fmt), username),
            )
        db.commit()


@router.get("/api/presence")
def get_presence(request: Request):
    """Returns all listeners active within the last PRESENCE_TTL seconds."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()

    now = datetime.utcnow()
    cutoff = (now - timedelta(seconds=PRESENCE_TTL)).strftime("%Y-%m-%d %H:%M:%S")
    away_cutoff = (now - timedelta(seconds=AWAY_AFTER)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as db:
        rows = db.execute(
            """SELECT username, song, artist, song_id, playing, party_host, platform, updated, active_at, idle_known_at, locked_at, lock_seen_at
               FROM   presence
               WHERE  updated >= ?
               ORDER  BY updated DESC""",
            (cutoff,),
        ).fetchall()
        # Everyone this service has ever seen, most recently active first.
        # The frontend used to derive its offline roster from Matrix room
        # membership, which meant it couldn't draw anybody until the SDK
        # had downloaded, logged in and finished an initial sync — several
        # seconds of the rail visibly filling in. This is the same list and
        # it's already here, so it arrives with the very first poll.
        roster_rows = db.execute(
            """SELECT username, last_seen FROM users ORDER BY last_seen DESC"""
        ).fetchall()

    listeners = [
        {
            "username":  r["username"],
            "song":      r["song"],
            "artist":    r["artist"],
            "songId":    r["song_id"],
            "playing":   bool(r["playing"]),
            "partyHost": r["party_host"] or None,
            "platform":  r["platform"] or None,
            "updated":   r["updated"],
            # awaySince is when they last used anything (UTC).
            **({"away": True, "awaySince": r["active_at"]} if _away(r, cutoff, away_cutoff) else {"away": False}),
        }
        for r in rows
    ]
    roster = [
        {"username": r["username"], "lastSeen": r["last_seen"]}
        for r in roster_rows
        # Service accounts aren't people; the frontend filtered these out
        # of the Matrix-derived roster too.
        if not r["username"].endswith("-bot")
    ]
    return {"listeners": listeners, "roster": roster}


def _away(r, cutoff, away_cutoff) -> bool:
    """Away: a device that sees the whole computer says nothing's been
    touched on any of theirs for AWAY_AFTER, or their screen is locked and
    nothing's been used since it locked."""
    if not r["active_at"]:
        return False
    idle = r["idle_known_at"] >= cutoff and r["active_at"] < away_cutoff
    locked = r["lock_seen_at"] >= cutoff and r["locked_at"] and r["active_at"] <= r["locked_at"]
    return bool(idle or locked)


@router.delete("/api/presence")
def clear_presence(request: Request, device: str = ""):
    """One of someone's devices has gone (signed out, or a phone app put
    away with nothing playing). They stay around, showing another device,
    if they have one still open."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    _beats.get(username, {}).pop(re.sub(r"[^A-Za-z0-9_-]", "", device)[:40], None)
    shown = _best(username)
    if shown:
        _save_presence(username, shown["song"], shown["artist"], shown["song_id"], shown["playing"],
                       shown["party_host"], shown["platform"])
        return {"ok": True}
    with get_db() as db:
        db.execute("DELETE FROM presence WHERE username=?", (username,))
        db.commit()
    return {"ok": True}
