"""Together: everything people are doing with each other right now, for the
top of Social, and watch parties (a video watched in step).

GET /api/posts/rooms is the whole picture in one answer, so Social polls
one thing:

  watch      watch parties (below)
  listen     listening parties: someone being listened along with
             (presence's partyHost), the song, and everyone following
  tv         TV channels someone is watching (routers/tv.py)
  radio      radio stations someone is tuned in to (routers/radio.py)
  voice      voice channels with people in them (routers/voice.py)
  listening  people playing music on their own (not in a party, not on
             the radio): the ambient layer under the rooms

A watch party is a video and who's watching it. Anyone in it can play,
pause, seek or put on another video, and everyone follows (a couch: the
last press wins). The room keeps "playing or not, at this position, as of
this server time"; each page polls it about once a second while it's in
one and lines its player up with that. A member not heard from in
MEMBER_TTL seconds has gone; the host leaving passes the room to whoever
joined next, and the last one out ends it.

Rooms live in this process's memory only, like the session
(routers/session.py): one uvicorn process. After a restart they're gone
and each page finds its room missing and says so. Under /api/posts/
because Caddy only forwards a fixed list of /api prefixes."""
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_username
from ..db import get_db

router = APIRouter()
BASE = "/api/posts/rooms"

MEMBER_TTL = 20      # seconds without a poll before a member has gone
MAX_ROOMS = 50
MAX_MEMBERS = 30

# id -> room. members: username -> last heard from (time.time()), in the
# order they joined (dicts keep it).
_rooms: dict[str, dict] = {}
# username -> the room they're in.
_member_of: dict[str, str] = {}


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _signed_in(request: Request) -> str | None:
    u = get_username(request)
    return None if u == "anonymous" else u


def _video(video_id: int) -> dict | None:
    """What a room needs to know of a video, if it's there and ready."""
    from .videos import _urls
    with get_db() as db:
        r = db.execute(
            """SELECT v.*, p.username FROM videos v JOIN posts p ON p.id = v.post_id
               WHERE v.post_id = ?""", (video_id,)).fetchone()
    if r is None or r["status"] != "ready":
        return None
    u = _urls(r)
    return {"id": r["post_id"], "title": r["title"] or "Untitled", "username": r["username"],
            "duration": r["duration"], "poster": u["poster"], "posterSmall": u["posterSmall"]}


def _sweep() -> None:
    now = time.time()
    for rid, room in list(_rooms.items()):
        for u in [u for u, at in room["members"].items() if now - at > MEMBER_TTL]:
            _drop(room, u)
        if not room["members"]:
            _end(rid)


def _drop(room: dict, username: str) -> None:
    room["members"].pop(username, None)
    if _member_of.get(username) == room["id"]:
        _member_of.pop(username, None)
    if room["host"] == username and room["members"]:
        room["host"] = next(iter(room["members"]))


def _end(rid: str) -> None:
    room = _rooms.pop(rid, None)
    if room:
        for u in list(room["members"]):
            if _member_of.get(u) == rid:
                _member_of.pop(u, None)


def _leave_any(username: str) -> None:
    rid = _member_of.get(username)
    room = _rooms.get(rid) if rid else None
    if room:
        _drop(room, username)
        if not room["members"]:
            _end(rid)
    _member_of.pop(username, None)


def _position(room: dict, now: float) -> float:
    pos = room["position"] + (now - room["at"] if room["playing"] else 0)
    d = room["video"].get("duration") or 0
    return min(pos, d) if d else pos


def _json(room: dict, now: float | None = None) -> dict:
    now = now or time.time()
    return {
        "id": room["id"], "video": room["video"], "host": room["host"],
        "people": list(room["members"]),
        # Where it's at: `position` as of `at` (this server's clock), and
        # moving on from there while `playing`.
        "playing": room["playing"], "position": round(room["position"], 2), "at": room["at"],
        "positionNow": round(_position(room, now), 1),
        "by": room["by"], "seq": room["seq"], "created": room["created"],
    }


def room_of(username: str) -> dict | None:
    """The watch party someone's in ({id, title}), for presence."""
    rid = _member_of.get(username)
    room = _rooms.get(rid) if rid else None
    return {"id": room["id"], "title": room["video"]["title"]} if room else None


async def _body(request: Request) -> dict:
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


def _secs(v, default: float = 0.0) -> float:
    try:
        return max(0.0, min(float(v), 86400.0))
    except (TypeError, ValueError):
        return default


# ── Together ──

def _invisible(username: str) -> bool:
    from .presence import _latest
    return _latest(username).get("status") == "invisible"


def _listen_parties_and_listeners() -> tuple[list, list]:
    from .presence import _beats, _best, live
    followers: dict[str, list[str]] = {}
    live_of: dict[str, dict] = {}
    for u in list(_beats):
        if _invisible(u):
            continue
        b = _best(u)
        if not b:
            continue
        lv = live(u)
        if lv:
            live_of[u] = lv
        if b.get("party_host") and b["party_host"] != u:
            followers.setdefault(b["party_host"], []).append(u)
    parties, in_party = [], set()
    for host, fs in followers.items():
        lv = live_of.get(host)
        if not lv or lv.get("radio") or not lv.get("songId"):
            continue
        parties.append({"host": host, "people": [host] + sorted(fs), "now": lv})
        in_party.update([host, *fs])
    listening = [
        {"username": u, "now": lv} for u, lv in live_of.items()
        if u not in in_party and not lv.get("radio") and lv.get("songId") and lv.get("playing")
    ]
    listening.sort(key=lambda x: x["username"])
    parties.sort(key=lambda p: -len(p["people"]))
    return parties, listening


def _tv() -> list:
    from . import tv
    watchers = tv._watchers()
    if not watchers:
        return []
    ids = list(watchers)
    marks = ",".join("?" * len(ids))
    with get_db() as db:
        rows = db.execute(f"SELECT * FROM tv_channels WHERE id IN ({marks})", ids).fetchall()
        lineups = tv._lineups(db, ids)
    now = time.time()
    out = []
    for r in rows:
        nn = tv._now_next(lineups.get(r["id"], []), r["epoch"], now)
        if not nn["now"]:
            continue
        out.append({"id": r["id"], "name": r["name"], "number": r["number"], "color": r["color"] or None,
                    "cover": r["cover"] or None, "onNow": nn["now"], "people": sorted(watchers[r["id"]])})
    out.sort(key=lambda c: (-len(c["people"]), c["number"]))
    return out


def _radio() -> list:
    from . import radio
    out = []
    for sid, people in radio._listeners().items():
        people = [u for u in people if not _invisible(u)]
        st = radio.now_on(sid) if people else None
        if st:
            out.append(dict(st, people=sorted(people)))
    out.sort(key=lambda s: (-len(s["people"]), s["frequency"]))
    return out


def _voice() -> list:
    from . import voice
    if not voice._enabled():
        return []
    rooms, _ = voice._occupancy()
    if not rooms:
        return []
    with get_db() as db:
        chans = voice._channels(db)
    out = []
    for c in chans:
        members = rooms.get(f"{voice.ROOM_PREFIX}{c['id']}", [])
        if members:
            out.append({
                "id": c["id"], "name": c["name"], "people": [m["username"] for m in members],
                # Who's sharing their screen (only people in the channel can watch).
                "sharing": [m["username"] for m in members if m.get("sharing")],
            })
    return out


@router.get(BASE)
def together(request: Request):
    me = _signed_in(request)
    if not me:
        return _error(401, "not authenticated")
    _sweep()
    now = time.time()
    parties, listening = _listen_parties_and_listeners()
    watch = [_json(r, now) for r in _rooms.values()]
    watch.sort(key=lambda r: (-len(r["people"]), -r["created"]))
    return {
        "now": now,
        "mine": _member_of.get(me),
        "watch": watch,
        "listen": parties,
        "tv": _tv(),
        "radio": _radio(),
        "voice": _voice(),
        "listening": listening,
    }


# ── Watch parties ──

@router.post(BASE)
async def create_room(request: Request):
    """Start a watch party on a video (you're its host), where you are in it."""
    me = _signed_in(request)
    if not me:
        return _error(401, "not authenticated")
    body = await _body(request)
    try:
        vid = int(body.get("video"))
    except (TypeError, ValueError):
        return _error(400, "which video?")
    v = _video(vid)
    if not v:
        return _error(404, "that video isn't ready to watch")
    _sweep()
    _leave_any(me)
    if len(_rooms) >= MAX_ROOMS:
        return _error(429, "too many watch parties at once")
    now = time.time()
    rid = secrets.token_urlsafe(6)
    room = {"id": rid, "video": v, "host": me, "members": {me: now},
            "playing": bool(body.get("playing", False)), "position": _secs(body.get("position")),
            "at": now, "by": me, "seq": 1, "created": now}
    _rooms[rid] = room
    _member_of[me] = rid
    return dict(_json(room, now), now=now)


def _room_or_404(rid: str):
    _sweep()
    room = _rooms.get(rid)
    return room, (None if room else _error(404, "that watch party has ended"))


@router.get(f"{BASE}/{{rid}}")
def get_room(rid: str, request: Request):
    """The room as it is now. A member asking is a member still here."""
    me = _signed_in(request)
    if not me:
        return _error(401, "not authenticated")
    room, err = _room_or_404(rid)
    if err:
        return err
    now = time.time()
    if me in room["members"]:
        room["members"][me] = now
    return dict(_json(room, now), now=now, member=me in room["members"])


@router.post(f"{BASE}/{{rid}}/join")
def join_room(rid: str, request: Request):
    me = _signed_in(request)
    if not me:
        return _error(401, "not authenticated")
    room, err = _room_or_404(rid)
    if err:
        return err
    if me not in room["members"]:
        if len(room["members"]) >= MAX_MEMBERS:
            return _error(429, "that watch party is full")
        _leave_any(me)
    room["members"][me] = time.time()
    _member_of[me] = rid
    now = time.time()
    return dict(_json(room, now), now=now, member=True)


@router.post(f"{BASE}/{{rid}}/state")
async def set_state(rid: str, request: Request):
    """Someone in the room pressed play or pause, moved, or put on another
    video: that's where everyone is now."""
    me = _signed_in(request)
    if not me:
        return _error(401, "not authenticated")
    room, err = _room_or_404(rid)
    if err:
        return err
    if me not in room["members"]:
        return _error(403, "you're not in this watch party")
    body = await _body(request)
    now = time.time()
    if body.get("video") is not None:
        try:
            v = _video(int(body["video"]))
        except (TypeError, ValueError):
            v = None
        if not v:
            return _error(404, "that video isn't ready to watch")
        room["video"] = v
    room["playing"] = bool(body.get("playing", room["playing"]))
    room["position"] = _secs(body.get("position"), _position(room, now))
    room["at"] = now
    room["by"] = me
    room["seq"] += 1
    room["members"][me] = now
    return dict(_json(room, now), now=now, member=True)


@router.post(f"{BASE}/{{rid}}/leave")
def leave_room(rid: str, request: Request):
    me = _signed_in(request)
    if not me:
        return _error(401, "not authenticated")
    room = _rooms.get(rid)
    if room and me in room["members"]:
        _drop(room, me)
        if not room["members"]:
            _end(rid)
    return {"ok": True}
