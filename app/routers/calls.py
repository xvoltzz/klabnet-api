"""Calls: ring someone, by name or by their klabnet extension.

The owner (2026-11-20): "also can we add direct calls? maybe everyone gets
a klabnet extension!", then "a dialer would be cool too".

- Extensions: everyone gets a three-digit number, first come first served
  (in the order klabnet first saw them), from 100 to 799. 8xx and 9xx are
  kept back for later (voice channels, TV channels). You can move yours to
  any free number; an admin can give anyone any number, swapping with
  whoever had it.
- Ringing: every page holds a stream open here (server-sent events, like
  the music session's, routers/session.py). Placing a call rings every one
  of the callee's devices that has its stream open; the first to accept
  takes it and the rest stop. Nobody there, do not disturb, or already in
  a call: it's a missed call at once, without ringing. No answer in RING_FOR
  seconds: missed.
- The call itself is a LiveKit room of its own (klabnet-call-<id>), with
  the same token as a voice channel's (routers/voice.py), handed only to
  the two people in it once it's been answered. Ending the call deletes the
  room, so nobody is left in it.
- Ringing and live calls are in this process's memory (one uvicorn
  process). A row goes in `calls` when one ends: the call log, and the
  recap's "missed call from …". A restart ends the calls in progress.

Under /api/posts/ because Caddy only forwards a fixed list of /api prefixes.
"""

import asyncio
import json
import re
import secrets
import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth import get_groups, get_username, is_admin
from ..config import LIVEKIT_URL
from ..db import get_db
from . import presence
from .voice import TOKEN_TTL, _enabled, _livekit, _token

router = APIRouter()
BASE = "/api/posts/calls"

EXT_LO, EXT_HI = 100, 799     # people's numbers; 800-999 kept for later
RING_FOR = 30                 # seconds before a call nobody answers is missed
DROP_GRACE = 15               # a device in a call whose stream goes: this long to come back
CALLER_GRACE = 6              # ...while it's still ringing
KEEPALIVE = 15
POLL = 5
MAX_DEVICES = 12
MAX_FAVORITES = 24
LOG_LIMIT = 60

_DEVICE_RE = re.compile(r"[A-Za-z0-9_-]{8,40}")
ROOM_PREFIX = "klabnet-call-"
# Outcomes that are a missed call for the person called (cancelled: the
# caller gave up while it rang).
MISSED = ("missed", "cancelled", "dnd", "busy", "unavailable")


class _Device:
    __slots__ = ("id", "inbox")

    def __init__(self, device_id: str):
        self.id = device_id
        self.inbox: asyncio.Queue = asyncio.Queue(maxsize=128)


# username -> {device id: _Device}
_streams: dict[str, dict[str, _Device]] = {}
# call id -> call (a dict; see _new)
_calls: dict[str, dict] = {}


def _error(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message, **extra})


def _ms() -> int:
    return int(time.time() * 1000)


def _sql_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ms else ""


def _iso(s: str) -> str:
    return s.replace(" ", "T") + "Z" if s and "T" not in s else s


async def _body(request: Request) -> dict:
    try:
        raw = await request.body()
        if len(raw) > 4096:
            return {}
        body = json.loads(raw or b"{}")
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


# ── Extensions ──

def _assign(db) -> dict[str, int]:
    """Everyone's extension, giving one to anybody who hasn't got one yet
    (in the order klabnet first saw them)."""
    have = {r["username"]: r["ext"] for r in db.execute("SELECT username, ext FROM extensions")}
    missing = [r["username"] for r in db.execute("SELECT username FROM users ORDER BY first_seen, username")
               if r["username"] not in have and not r["username"].endswith("-bot") and r["username"] != "anonymous"]
    if missing:
        taken = set(have.values())
        free = (n for n in range(EXT_LO, EXT_HI + 1) if n not in taken)
        for u in missing:
            n = next(free, None)
            if n is None:
                break
            db.execute("INSERT OR IGNORE INTO extensions(username, ext) VALUES (?, ?)", (u, n))
            have[u] = n
        db.commit()
    return have


def _resolve(db, to: str) -> str | None:
    """A username from what was dialled: an extension or a name."""
    to = str(to or "").strip().lower()
    if not to:
        return None
    if to.isdigit():
        exts = _assign(db)
        n = int(to)
        return next((u for u, e in exts.items() if e == n), None)
    row = db.execute("SELECT username FROM users WHERE lower(username) = ?", (to.lstrip("@"),)).fetchone()
    return row["username"] if row and not row["username"].endswith("-bot") else None


@router.get(f"{BASE}/extensions")
def extensions(request: Request):
    if get_username(request) == "anonymous":
        return _error(401, "not authenticated")
    with get_db() as db:
        exts = _assign(db)
    return {"extensions": exts, "range": [EXT_LO, EXT_HI]}


@router.put(f"{BASE}/extensions/{{username}}")
async def set_extension(username: str, request: Request):
    """Move someone to another number: yourself to a free one, or (an
    admin) anyone to any, swapping with whoever had it."""
    me = get_username(request)
    if me == "anonymous":
        return _error(401, "not authenticated")
    username = username.strip().lower()
    admin = is_admin(get_groups(request))
    if username != me and not admin:
        return _error(403, "you can only change your own extension")
    try:
        ext = int((await _body(request)).get("ext"))
    except (TypeError, ValueError):
        return _error(400, "an extension is three digits")
    if not EXT_LO <= ext <= EXT_HI:
        return _error(400, f"extensions go from {EXT_LO} to {EXT_HI}")
    with get_db() as db:
        exts = _assign(db)
        if username not in exts:
            return _error(404, "no one by that name")
        holder = next((u for u, e in exts.items() if e == ext), None)
        if holder == username:
            return {"username": username, "ext": ext}
        if holder and not admin:
            return _error(409, f"{ext} is {holder}'s", holder=holder)
        old = exts[username]
        # UNIQUE: park the other one on 0 while they trade.
        if holder:
            db.execute("UPDATE extensions SET ext = 0 WHERE username = ?", (holder,))
        db.execute("UPDATE extensions SET ext = ? WHERE username = ?", (ext, username))
        if holder:
            db.execute("UPDATE extensions SET ext = ? WHERE username = ?", (old, holder))
        db.commit()
    return {"username": username, "ext": ext, "swapped": holder or None}


# ── Speed dial ──

@router.get(f"{BASE}/favorites")
def favorites(request: Request):
    me = get_username(request)
    if me == "anonymous":
        return _error(401, "not authenticated")
    with get_db() as db:
        rows = db.execute("SELECT fav FROM call_favorites WHERE username = ? ORDER BY position, fav", (me,)).fetchall()
    return {"favorites": [r["fav"] for r in rows]}


@router.put(f"{BASE}/favorites")
async def put_favorites(request: Request):
    me = get_username(request)
    if me == "anonymous":
        return _error(401, "not authenticated")
    raw = (await _body(request)).get("favorites")
    if not isinstance(raw, list):
        return _error(400, "favorites is a list of usernames")
    favs = []
    for u in raw:
        u = str(u or "").strip().lower()[:64]
        if u and u != me and u not in favs:
            favs.append(u)
    favs = favs[:MAX_FAVORITES]
    with get_db() as db:
        db.execute("DELETE FROM call_favorites WHERE username = ?", (me,))
        db.executemany("INSERT INTO call_favorites(username, fav, position) VALUES (?, ?, ?)",
                       [(me, u, i) for i, u in enumerate(favs)])
        db.commit()
    return {"favorites": favs}


# ── Calls in progress ──

def _public(c: dict) -> dict:
    return {k: c[k] for k in ("id", "from", "to", "state", "outcome", "started", "answered", "ended",
                              "callerDevice", "device", "endedBy")}


def _post(d: _Device, event: str, data: dict) -> None:
    try:
        d.inbox.put_nowait((event, data))
    except asyncio.QueueFull:
        _end_stream(d)


def _end_stream(d: _Device) -> None:
    while not d.inbox.empty():
        d.inbox.get_nowait()
    d.inbox.put_nowait(None)


def _tell(c: dict) -> None:
    """Both people's devices hear every change to a call."""
    data = {"call": _public(c), "t": _ms()}
    for u in {c["from"], c["to"]}:
        for d in list((_streams.get(u) or {}).values()):
            _post(d, "call", data)


def _call_of(username: str) -> dict | None:
    return next((c for c in _calls.values() if c["state"] != "ended" and username in (c["from"], c["to"])), None)


def _log(c: dict) -> None:
    secs = max(0, round((c["ended"] - c["answered"]) / 1000)) if c["answered"] else 0
    try:
        with get_db() as db:
            db.execute(
                "INSERT OR REPLACE INTO calls(id, caller, callee, started, answered, ended, outcome, seconds) VALUES (?,?,?,?,?,?,?,?)",
                (c["id"], c["from"], c["to"], _sql_time(c["started"]), _sql_time(c["answered"]), _sql_time(c["ended"]), c["outcome"], secs))
            db.commit()
    except Exception:
        pass


def _finish(c: dict, outcome: str, by: str = "") -> None:
    """A call is over (or never started): say so, write it down, and close
    its room so nobody's left in it."""
    if c["state"] == "ended":
        return
    was_live = c["state"] == "live"
    c.update(state="ended", outcome=outcome, ended=_ms(), endedBy=by)
    for h in c.pop("_timers", []):
        h.cancel()
    _tell(c)
    _log(c)
    if was_live and _enabled():
        try:
            _livekit("DeleteRoom", {"room": ROOM_PREFIX + c["id"]}, {"roomCreate": True})
        except (httpx.HTTPError, ValueError):
            pass
    # Kept a minute, so a page that asks about it late hears how it ended.
    try:
        asyncio.get_running_loop().call_later(60, lambda: _calls.pop(c["id"], None))
    except RuntimeError:
        _calls.pop(c["id"], None)


def _later(c: dict, secs: float, fn) -> None:
    c.setdefault("_timers", []).append(asyncio.get_running_loop().call_later(secs, fn))


def _ring_out(c: dict) -> None:
    if c["state"] == "ringing":
        _finish(c, "missed")


def _check_dropped(c: dict) -> None:
    """A device in the call (or the caller's, while it rings) whose stream
    closed and didn't come back: the call's over."""
    if c["state"] == "ended":
        return
    if c["callerDevice"] not in (_streams.get(c["from"]) or {}):
        _finish(c, "cancelled" if c["state"] == "ringing" else "answered", by=c["from"])
    elif c["state"] == "live" and c["device"] not in (_streams.get(c["to"]) or {}):
        _finish(c, "answered", by=c["to"])


def _status(username: str) -> str:
    try:
        return presence._latest(username).get("status") or ""
    except Exception:
        return ""


@router.get(BASE)
def probe(request: Request):
    """The call log (newest first), your extension, and the calls going on
    now that are yours. An API without calls answers 404/405 here."""
    me = get_username(request)
    if me == "anonymous":
        return _error(401, "not authenticated")
    with get_db() as db:
        exts = _assign(db)
        rows = db.execute(
            """SELECT id, caller, callee, started, answered, ended, outcome, seconds FROM calls
               WHERE caller = ? OR callee = ? ORDER BY ended DESC LIMIT ?""", (me, me, LOG_LIMIT)).fetchall()
    log = [{
        "id": r["id"], "with": r["callee"] if r["caller"] == me else r["caller"],
        "out": r["caller"] == me, "outcome": r["outcome"], "seconds": r["seconds"],
        "started": _iso(r["started"]), "ended": _iso(r["ended"]),
        "missed": r["callee"] == me and r["outcome"] in MISSED,
    } for r in rows]
    c = _call_of(me)
    return {"calls": True, "t": _ms(), "ext": exts.get(me), "log": log, "now": _public(c) if c else None,
            "voice": _enabled()}


@router.get(f"{BASE}/stream")
async def stream(request: Request, device: str = ""):
    """A page's line in: first how things stand ("hello": the call you're
    in or being rung for), then every change to a call of yours ("call")."""
    me = get_username(request)
    if me == "anonymous":
        return _error(401, "not authenticated")
    if not _DEVICE_RE.fullmatch(device):
        return _error(400, "bad device id")
    mine = _streams.setdefault(me, {})
    if device not in mine and len(mine) >= MAX_DEVICES:
        return _error(429, "too many devices")
    d = _Device(device)
    old = mine.get(device)
    if old:
        _end_stream(old)
    mine[device] = d

    async def events():
        try:
            c = _call_of(me)
            yield f"event: hello\ndata: {json.dumps({'call': _public(c) if c else None, 't': _ms()})}\n\n"
            quiet = 0
            while True:
                try:
                    item = await asyncio.wait_for(d.inbox.get(), POLL)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    quiet += POLL
                    if quiet >= KEEPALIVE:
                        quiet = 0
                        yield ": still here\n\n"
                    continue
                if item is None:
                    break
                quiet = 0
                yield f"event: {item[0]}\ndata: {json.dumps(item[1], separators=(',', ':'))}\n\n"
        finally:
            if _streams.get(me, {}).get(device) is d:
                del _streams[me][device]
                if not _streams[me]:
                    del _streams[me]
                c = _call_of(me)
                if c and device in (c["callerDevice"], c["device"]):
                    asyncio.get_running_loop().call_later(
                        CALLER_GRACE if c["state"] == "ringing" else DROP_GRACE, _check_dropped, c)

    return StreamingResponse(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@router.post(BASE)
async def place(request: Request):
    """`{to, device}`: ring someone. `to` is a username or an extension."""
    me = get_username(request)
    if me == "anonymous":
        return _error(401, "not authenticated")
    body = await _body(request)
    device = str(body.get("device") or "")
    if device not in (_streams.get(me) or {}):
        return _error(409, "not connected")
    if not _enabled():
        return _error(503, "calls need voice set up")
    with get_db() as db:
        callee = _resolve(db, body.get("to"))
    if not callee:
        return _error(404, "nobody has that number" if str(body.get("to") or "").strip().isdigit() else "no one by that name")
    if callee == me:
        return _error(400, "that's you")
    if _call_of(me):
        return _error(409, "you're already in a call")
    now = _ms()
    c = {"id": secrets.token_urlsafe(9), "from": me, "to": callee, "state": "ringing", "outcome": "",
         "started": now, "answered": 0, "ended": 0, "callerDevice": device, "device": "", "endedBy": ""}
    _calls[c["id"]] = c
    # Not rung at all: nowhere to ring, asked not to be, or on another call.
    if _call_of(callee) and _call_of(callee) is not c:
        _finish(c, "busy")
    elif _status(callee) == "dnd":
        _finish(c, "dnd")
    elif not _streams.get(callee):
        _finish(c, "unavailable")
    else:
        _tell(c)
        _later(c, RING_FOR, lambda: _ring_out(c))
    return {"call": _public(c)}


def _mine(request: Request, call_id: str):
    me = get_username(request)
    if me == "anonymous":
        return None, None, _error(401, "not authenticated")
    c = _calls.get(call_id)
    if not c or me not in (c["from"], c["to"]):
        return None, None, _error(404, "no such call")
    return me, c, None


@router.post(f"{BASE}/{{call_id}}/accept")
async def accept(call_id: str, request: Request):
    me, c, err = _mine(request, call_id)
    if err:
        return err
    device = str((await _body(request)).get("device") or "")
    if me != c["to"]:
        return _error(403, "it's not your phone that's ringing")
    if c["state"] == "live":
        return _error(409, "answered on another device" if c["device"] != device else "already answered", call=_public(c))
    if c["state"] != "ringing":
        return _error(409, "that call has ended", call=_public(c))
    if device not in (_streams.get(me) or {}):
        return _error(409, "not connected")
    for h in c.pop("_timers", []):
        h.cancel()
    c.update(state="live", answered=_ms(), device=device)
    _tell(c)
    return {"call": _public(c)}


@router.post(f"{BASE}/{{call_id}}/decline")
async def decline(call_id: str, request: Request):
    """The callee says no (`reason: "dnd"` from a page that has do not
    disturb on: then it's a missed call, not a declined one)."""
    me, c, err = _mine(request, call_id)
    if err:
        return err
    if me != c["to"]:
        return _error(403, "it's your call: hang up instead")
    if c["state"] == "ringing":
        _finish(c, "dnd" if (await _body(request)).get("reason") == "dnd" else "declined", by=me)
    return {"call": _public(c)}


@router.post(f"{BASE}/{{call_id}}/hangup")
async def hangup(call_id: str, request: Request):
    """Either side ends it: the caller while it rings (cancelled), anyone
    once it's live."""
    me, c, err = _mine(request, call_id)
    if err:
        return err
    if c["state"] == "ringing":
        if me == c["from"]:
            _finish(c, "cancelled", by=me)
        else:
            _finish(c, "declined", by=me)
    elif c["state"] == "live":
        _finish(c, "answered", by=me)
    return {"call": _public(c)}


@router.post(f"{BASE}/{{call_id}}/token")
async def token(call_id: str, request: Request):
    """Into the call's room, once it's live, for the two devices in it."""
    me, c, err = _mine(request, call_id)
    if err:
        return err
    if c["state"] != "live":
        return _error(409, "that call isn't on", call=_public(c))
    device = str((await _body(request)).get("device") or "")
    if device != (c["callerDevice"] if me == c["from"] else c["device"]):
        return _error(403, "this call is on your other device")
    if not _enabled():
        return _error(503, "voice isn't set up")
    other = c["to"] if me == c["from"] else c["from"]
    t = _token({
        "sub": me, "name": me,
        "video": {
            "room": ROOM_PREFIX + c["id"], "roomJoin": True, "canSubscribe": True, "canPublish": True,
            "canPublishSources": ["microphone", "camera", "screen_share", "screen_share_audio"],
            "canPublishData": True, "canUpdateOwnMetadata": True,
        },
    }, TOKEN_TTL)
    return {"url": LIVEKIT_URL, "token": t, "room": ROOM_PREFIX + c["id"], "with": other}


def in_call(username: str) -> str:
    """Who someone's on a call with right now ('' if nobody), for presence."""
    c = _call_of(username)
    if not c or c["state"] != "live":
        return ""
    return c["to"] if c["from"] == username else c["from"]


def recap_items(db, me: str, at: str) -> list[dict]:
    """Missed calls since `at` (routers/recap.py)."""
    marks = ",".join("?" * len(MISSED))
    return [{"kind": "call", "at": _iso(r["ended"]), "who": [r["caller"]], "outcome": r["outcome"], "id": r["id"]}
            for r in db.execute(
                f"""SELECT id, caller, ended, outcome FROM calls WHERE callee = ? AND ended > ? AND outcome IN ({marks})
                    ORDER BY ended DESC LIMIT 20""", (me, at, *MISSED))]
