"""One player on every device, the way Spotify does it.

Of all the places someone has klabnet open, one is *active*: the one making
the sound. It reports what it's playing here (the song, where it's got to,
the queue, what's up next). Their other devices each hold a stream open
(server-sent events), show the same thing, and send their button presses
here to be handed on to the active one instead of playing anything
themselves. Any of them can take over ("Play here"): it claims the session
and the one that was playing goes quiet.

With nobody active the last thing played stays here, paused where it
stopped, so the next device to press Play carries on from it.

Nothing is stored. It all lives in this process's memory, which is right
for one uvicorn process (what the Dockerfile runs) and wrong for several.
After a restart the streams reconnect and the active device says it all
again.

Under /api/music-requests/ for the same reason playlists are: Caddy only
forwards an explicit list of /api prefixes and that one is Music's.
Registered before music_requests.router so nothing here is ever taken for
its /api/music-requests/{request_id}.
"""

import asyncio
import json
import re
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth import get_username
from .playlists import _clean_song

router = APIRouter()

BASE = "/api/music-requests/session"

MAX_DEVICES = 12            # streams one person can hold open
MAX_SONGS = 2000            # in the queue, and in what's up next
MAX_BODY_BYTES = 1_500_000
KEEPALIVE = 15              # seconds between comments on a quiet stream (proxies drop silent ones)
POLL = 5                    # how often a quiet stream checks its client is still there
# The active device's stream closing isn't it going away: a phone changes
# network, a laptop wakes up. It has this long to come back first.
GRACE = 10
# Paused this long, the active device stops being "where it's playing":
# pressing Play somewhere else then plays there, not in another room.
IDLE_RELEASE = 10 * 60
FORGET_AFTER = 24 * 3600    # a session nobody has touched or is connected to

_DEVICE_RE = re.compile(r"[A-Za-z0-9_-]{8,40}")
_COMMANDS = {"play", "resume", "pause", "seek", "next", "prev", "queue", "list", "set", "take", "no"}


class _Device:
    __slots__ = ("id", "name", "platform", "inbox")

    def __init__(self, device_id: str, name: str, platform: str):
        self.id = device_id
        self.name = name
        self.platform = platform
        self.inbox: asyncio.Queue = asyncio.Queue(maxsize=256)


class _Session:
    """One person's player."""

    def __init__(self):
        self.devices: dict[str, _Device] = {}
        self.active = ""          # the device making the sound, or nobody
        self.now: dict | None = None
        self.now_at = 0           # when `now` was true, in ms on this clock
        self.queue: list[dict] = []
        self.playlist: list[dict] = []
        self.dropped_at = 0.0     # when the active device's stream closed (0: it's there)
        self.dropped_ms = 0       # ...and what `_ms()` was then
        self.gone: dict[str, float] = {}  # devices whose stream closed lately, and when
        self.touched = time.monotonic()


_sessions: dict[str, _Session] = {}


def _ms() -> int:
    return int(time.time() * 1000)


def _error(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message, **extra})


def _unauthenticated() -> JSONResponse:
    return _error(401, "not authenticated")


def active_device(username: str) -> str:
    """The device someone's music is coming out of, for presence ("" if none)."""
    s = _sessions.get(username)
    return s.active if s else ""


def device_gone(username: str, device: str) -> bool:
    """A device that had a stream here and has closed it: its page has gone,
    so presence stops believing the last thing it said (routers/presence.py)
    rather than waiting for that to go stale."""
    s = _sessions.get(username)
    return bool(s and device in s.gone)


def _position(s: _Session, at_ms: int = 0) -> float:
    """Where the song has got to by now (or had by `at_ms`)."""
    now = s.now
    if not now:
        return 0.0
    pos = now["pos"]
    if now["playing"] and not now["stalled"]:
        pos += max(0, (at_ms or _ms()) - s.now_at) / 1000
    return min(pos, now["dur"]) if now["dur"] else pos


def _snapshot(s: _Session, parts=("now", "queue", "playlist", "active")) -> dict:
    """The named parts of a session, as a stream event. Who's connected and
    this clock's time go with every one."""
    out: dict = {
        "t": _ms(),
        "devices": [{"id": d.id, "name": d.name, "platform": d.platform} for d in s.devices.values()],
        "active": s.active,
    }
    if "now" in parts:
        out["now"] = dict(s.now, at=s.now_at) if s.now else None
    if "queue" in parts:
        out["queue"] = s.queue
    if "playlist" in parts:
        out["playlist"] = s.playlist
    return out


def _post(device: _Device, event: str, data: dict) -> None:
    try:
        device.inbox.put_nowait((event, data))
    except asyncio.QueueFull:
        # A stream nobody is reading. End it: the page reconnects and is
        # told everything afresh, which beats a backlog of old news.
        _end(device)


def _end(device: _Device) -> None:
    while not device.inbox.empty():
        device.inbox.get_nowait()
    device.inbox.put_nowait(None)


def _send(s: _Session, parts=(), skip: str = "") -> None:
    data = _snapshot(s, parts)
    for d in list(s.devices.values()):
        if d.id != skip:
            _post(d, "sync", data)


def _release(s: _Session) -> None:
    """Nobody's active any more. What was playing stays, paused where it was
    (where it was when its device was last heard from, if that device just
    went: a closed page stopped playing then, not when the wait ran out)."""
    if s.now:
        s.now = dict(s.now, pos=_position(s, s.dropped_ms if s.dropped_at else 0), playing=False, stalled=False)
        s.now_at = _ms()
    s.active = ""
    s.dropped_at = 0.0
    s.dropped_ms = 0
    _send(s, ("now",))


def _check(s: _Session) -> None:
    if not s.active:
        return
    if s.active not in s.devices:
        if s.dropped_at and time.monotonic() - s.dropped_at >= GRACE:
            _release(s)
    elif s.now and not s.now["playing"] and _ms() - s.now_at >= IDLE_RELEASE * 1000:
        _release(s)


def _forget_old() -> None:
    cutoff = time.monotonic() - FORGET_AFTER
    for username in [u for u, s in _sessions.items() if not s.devices and s.touched < cutoff]:
        del _sessions[username]


def _number(v, lo: float, hi: float, default: float = 0.0) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return default
    return max(lo, min(float(v), hi))


def _clean_songs(raw) -> list[dict]:
    """Like playlists' own, but a queue can hold the same song twice."""
    out = []
    for item in raw if isinstance(raw, list) else []:
        song = _clean_song(item)
        if song:
            out.append(song)
            if len(out) >= MAX_SONGS:
                break
    return out


def _clean_now(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    song = _clean_song(raw.get("song"))
    if not song:
        return None
    return {
        "song": song,
        "playing": raw.get("playing") is True,
        # Still "playing" but waiting on the network: the others stop
        # counting the seconds until it says it's moving again.
        "stalled": raw.get("stalled") is True,
        "pos": _number(raw.get("pos"), 0, 86400),
        "dur": _number(raw.get("dur"), 0, 86400),
        "index": int(_number(raw.get("index"), 0, MAX_SONGS)),
        "shuffle": raw.get("shuffle") is True,
        "repeat": raw.get("repeat") is True,
        "volume": _number(raw.get("volume"), 0, 1, 1.0),
        "muted": raw.get("muted") is True,
    }


def _clean_command(raw) -> dict | None:
    """A button pressed on one device, for another to act on."""
    if not isinstance(raw, dict) or raw.get("t") not in _COMMANDS:
        return None
    cmd: dict = {"t": raw["t"]}
    if "song" in raw:
        song = _clean_song(raw["song"])
        if not song:
            return None
        cmd["song"] = song
    for key in ("queue", "playlist"):
        if key in raw:
            cmd[key] = _clean_songs(raw[key])
    if "index" in raw:
        cmd["index"] = int(_number(raw["index"], 0, MAX_SONGS))
    if "pos" in raw:
        cmd["pos"] = _number(raw["pos"], 0, 86400)
    if "volume" in raw:
        cmd["volume"] = _number(raw["volume"], 0, 1, 1.0)
    for key in ("shuffle", "repeat", "muted", "playing"):
        if key in raw:
            cmd[key] = raw[key] is True
    return cmd


async def _body(request: Request) -> dict | None:
    try:
        if int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
            return None
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return None
        body = json.loads(raw)
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


@router.get(BASE)
def probe(request: Request):
    """So a page can tell an API with this from one without (which answers
    404 or 405 here) before it opens a stream."""
    if get_username(request) == "anonymous":
        return _unauthenticated()
    return {"session": True, "t": _ms()}


@router.get(f"{BASE}/stream")
async def stream(request: Request, device: str = "", name: str = "", platform: str = ""):
    """A device's end of the session: first everything as it stands
    ("hello"), then whatever changes ("sync") and anything it's asked to do
    ("cmd"), for as long as the page is open."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    if not _DEVICE_RE.fullmatch(device):
        return _error(400, "bad device id")
    _forget_old()
    s = _sessions.setdefault(username, _Session())
    if device not in s.devices and len(s.devices) >= MAX_DEVICES:
        return _error(429, "too many devices")

    me = _Device(device, " ".join(name.split())[:40], re.sub(r"[^a-z:]", "", platform.lower())[:40])
    old = s.devices.get(device)
    if old:
        _end(old)  # the same page reconnecting: its old stream ends
    s.devices[device] = me
    s.gone.pop(device, None)
    s.touched = time.monotonic()
    if s.active == device:
        s.dropped_at = 0.0
    _send(s, skip=device)
    loop = asyncio.get_running_loop()

    async def events():
        try:
            yield _event("hello", dict(_snapshot(s), you=device))
            quiet = 0
            while True:
                try:
                    item = await asyncio.wait_for(me.inbox.get(), POLL)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    _check(s)
                    quiet += POLL
                    if quiet >= KEEPALIVE:
                        quiet = 0
                        yield ": still here\n\n"
                    continue
                if item is None:
                    break
                quiet = 0
                yield _event(*item)
        finally:
            if s.devices.get(device) is me:
                del s.devices[device]
                s.touched = time.monotonic()
                s.gone = {d: t for d, t in s.gone.items() if s.touched - t < 120}
                s.gone[device] = s.touched
                if s.active == device:
                    s.dropped_at = time.monotonic()
                    s.dropped_ms = _ms()
                    loop.call_later(GRACE + 0.5, _check, s)
                _send(s)

    return StreamingResponse(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    })


def _connected(request: Request, body: dict | None):
    """(session, device id) for a request from a device with its stream
    open, or (None, the error to answer with)."""
    username = get_username(request)
    if username == "anonymous":
        return None, _unauthenticated()
    if body is None:
        return None, _error(400, "invalid JSON")
    device = str(body.get("device") or "")
    s = _sessions.get(username)
    if not s or device not in s.devices:
        # Its stream isn't open (yet, or any more): it can't be told
        # anything, so it can't be part of this. It retries once it is.
        return None, _error(409, "not connected", active=s.active if s else "")
    s.touched = time.monotonic()
    return s, device


@router.put(f"{BASE}/state")
async def put_state(request: Request):
    """What the active device is playing: `{device, now?, queue?, playlist?}`,
    each part sent when it changes. With `claim` the sender becomes the
    active device, whoever was; without it, only if nobody is."""
    body = await _body(request)
    s, device = _connected(request, body)
    if s is None:
        return device
    if s.active and s.active != device and body.get("claim") is not True:
        return _error(409, "another device is playing", active=s.active)

    parts = []
    if "now" in body:
        now = _clean_now(body["now"])
        if not now:
            return _error(400, "now needs a song")
        s.now, s.now_at = now, _ms()
        parts.append("now")
    if "queue" in body:
        s.queue = _clean_songs(body["queue"])
        parts.append("queue")
    if "playlist" in body:
        s.playlist = _clean_songs(body["playlist"])
        parts.append("playlist")
    took_over = s.active != device
    if took_over:
        if not s.now:
            return _error(400, "nothing to play")
        s.active = device
    s.dropped_at = 0.0
    if parts or took_over:
        _send(s, parts, skip=device)
    return {"ok": True, "active": s.active, "t": _ms()}


@router.post(f"{BASE}/cmd")
async def post_command(request: Request):
    """`{device, cmd, to?}`: something for the active device to do (or for
    the device named in `to`, which is how one is asked to take over)."""
    body = await _body(request)
    s, device = _connected(request, body)
    if s is None:
        return device
    cmd = _clean_command(body.get("cmd"))
    if not cmd:
        return _error(400, "bad command")
    target = s.devices.get(str(body.get("to") or "") or s.active)
    if not target or target.id == device:
        return _error(409, "that device isn't there", active=s.active)
    _post(target, "cmd", {"from": device, "cmd": cmd, "t": _ms()})
    return {"ok": True}


@router.post(f"{BASE}/release")
async def release(request: Request):
    """The active device is going (its page is closing): don't wait out the
    grace period to say so."""
    s, device = _connected(request, await _body(request))
    if s is None:
        return device
    s.gone[device] = time.monotonic()
    if s.active == device:
        _release(s)
    return {"ok": True}
