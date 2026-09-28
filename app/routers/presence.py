import asyncio
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_username
from ..config import PRESENCE_TTL
from ..db import get_db

router = APIRouter()


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
    # What they're on, for the icon on their card: "app:windows",
    # "pwa:ios", "web:macos:firefox". Just letters and colons.
    platform   = re.sub(r"[^a-z:]", "", str(body.get("platform", "")).lower())[:40]

    # The busiest write there is (every ~8s from every open tab): off the
    # event loop, one connection and one commit.
    await asyncio.to_thread(_save_presence, username, song, artist, song_id, playing, party_host, platform)
    return {"ok": True}


def _save_presence(username, song, artist, song_id, playing, party_host, platform):
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
        db.commit()


@router.get("/api/presence")
def get_presence(request: Request):
    """Returns all listeners active within the last PRESENCE_TTL seconds."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()

    cutoff = (datetime.utcnow() - timedelta(seconds=PRESENCE_TTL)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as db:
        rows = db.execute(
            """SELECT username, song, artist, song_id, playing, party_host, platform, updated
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


@router.delete("/api/presence")
def clear_presence(request: Request):
    """Let a user remove themselves from presence (e.g. on logout/close)."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    with get_db() as db:
        db.execute("DELETE FROM presence WHERE username=?", (username,))
        db.commit()
    return {"ok": True}
