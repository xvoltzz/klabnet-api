"""Live streaming: someone streams from OBS, everyone else watches in klabnet.

The video never comes near this service. OBS sends it to LiveKit Ingress
(compose.yml's `ingress` service), which puts it in a LiveKit room of its
own, `live-<username>`, as a participant called `live-<username>`; viewers
join that room subscribe-only, like a voice channel nobody can talk in.

Two ways in, one stream key each, made the first time someone opens
Settings → Streaming:

  WHIP   OBS 30+'s "WHIP" service. Passed straight through, not transcoded
         (enable_transcoding false): what OBS encodes is what people get,
         bitrate, 60fps and all, under a second behind. WebRTC can't carry
         B-frames, so OBS has to send none.
  RTMP   for anything that only speaks RTMP. Ingress decodes and re-encodes
         it (CPU) as a 1080p60 simulcast ladder, a couple of seconds behind.

Whether someone's live is ListIngress's word (state ENDPOINT_PUBLISHING),
asked every few seconds by a thread here, which also counts who's watching
(ListParticipants) and keeps a row per stream (`live_streams`): started,
ended, title, game, peak viewers. A stream that drops comes back as the same
stream (and the same chat) if OBS reconnects within GRACE seconds.

Each stream has its own chat (`live_chat`, the radio's pattern: pages poll
GET …/chat?after=<last id>), so a new stream starts with an empty one.

Off (`enabled: false`, the site hides it) unless LiveKit is set up for voice
and LIVE_INGRESS=1. The tables are made here, on first use, not in db.py.
Under /api/posts/ because Caddy only forwards a fixed list of prefixes."""
import json
import re
import secrets
import threading
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_groups, get_username, is_admin
from ..config import LIVE_INGRESS, LIVE_RTMP_KBPS, LIVEKIT_API_URL, LIVEKIT_URL
from ..db import get_db
from . import voice

router = APIRouter()
BASE = "/api/posts/live"

ROOM_PREFIX = "live-"
INGRESS_PREFIX = "klabnet-live-"
POLL = 4.0            # how often LiveKit is asked who's live
GRACE = 20.0          # a dropped stream that comes back this soon is the same stream
TOKEN_TTL = 600
TITLE_MAX, GAME_MAX, DESC_MAX = 120, 60, 1000
_CHAT_KEEP, _CHAT_MAX = 300, 500
_USER_RE = re.compile(r"[a-z0-9._@-]{1,64}")

_http = httpx.Client(timeout=httpx.Timeout(4.0, connect=2.0))
_lock = threading.Lock()
# username -> {"since", "via", "video", "audio", "viewers": [usernames], "stream": id}
_live: dict[str, dict] = {}
_state = {"at": 0.0, "online": False, "ingress": {}, "thread": None}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_keys (
    username    TEXT PRIMARY KEY,
    whip_id     TEXT NOT NULL DEFAULT '',
    whip_key    TEXT NOT NULL DEFAULT '',
    whip_url    TEXT NOT NULL DEFAULT '',
    rtmp_id     TEXT NOT NULL DEFAULT '',
    rtmp_key    TEXT NOT NULL DEFAULT '',
    rtmp_url    TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    game        TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    created     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS live_streams (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    game        TEXT NOT NULL DEFAULT '',
    via         TEXT NOT NULL DEFAULT '',
    started     REAL NOT NULL,
    last_seen   REAL NOT NULL,
    ended       REAL,
    peak        INTEGER NOT NULL DEFAULT 0,
    video       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS live_streams_user ON live_streams(username, id);
CREATE INDEX IF NOT EXISTS live_streams_started ON live_streams(started);
CREATE TABLE IF NOT EXISTS live_chat (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    stream   INTEGER NOT NULL,
    username TEXT NOT NULL,
    body     TEXT NOT NULL,
    ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS live_chat_stream ON live_chat(stream, id);
"""
_schema_ready = False


def _db():
    global _schema_ready
    if not _schema_ready:
        with get_db() as db:
            db.executescript(_SCHEMA)
            db.commit()
        _schema_ready = True
    return get_db()


def _enabled() -> bool:
    return LIVE_INGRESS and voice._enabled()


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _signed_in(request: Request) -> str | None:
    u = get_username(request)
    return None if u == "anonymous" else u


def _twirp(service: str, method: str, body: dict, grant: dict) -> dict:
    r = _http.post(
        f"{LIVEKIT_API_URL}/twirp/livekit.{service}/{method}",
        json=body,
        headers={"Authorization": "Bearer " + voice._token({"video": grant}, 60)},
    )
    if r.status_code >= 400:
        try:
            msg = r.json().get("msg") or r.text
        except ValueError:
            msg = r.text
        raise httpx.HTTPStatusError(msg[:200], request=r.request, response=r)
    return r.json()


def _ingress(method: str, body: dict) -> dict:
    return _twirp("Ingress", method, body, {"ingressAdmin": True})


def _ns(v) -> float:
    try:
        return int(v or 0) / 1e9
    except (TypeError, ValueError):
        return 0.0


def _video_of(state: dict) -> dict | None:
    v = state.get("video") or {}
    if not v.get("width"):
        return None
    return {"width": v.get("width"), "height": v.get("height"), "fps": v.get("framerate") or 0,
            "bitrate": v.get("average_bitrate") or v.get("averageBitrate") or 0,
            "codec": (v.get("mime_type") or v.get("mimeType") or "").split("/")[-1]}


# ── Who's live: asked of LiveKit every POLL seconds ──

_poll_lock = threading.Lock()


def _poll_once() -> None:
    with _poll_lock:
        _poll()


def _poll() -> None:
    now = time.time()
    try:
        items = _ingress("ListIngress", {}).get("items") or []
        online = True
    except (httpx.HTTPError, ValueError):
        items, online = [], False
    publishing: dict[str, dict] = {}
    by_id = {}
    for it in items:
        name = it.get("name") or ""
        if not name.startswith(INGRESS_PREFIX):
            continue
        by_id[it.get("ingress_id") or it.get("ingressId")] = it
        st = it.get("state") or {}
        if st.get("status") != "ENDPOINT_PUBLISHING":
            continue
        user = (it.get("participant_identity") or it.get("participantIdentity") or "")[len(ROOM_PREFIX):]
        if not user:
            continue
        via = "whip" if (it.get("input_type") or it.get("inputType")) == "WHIP_INPUT" else "rtmp"
        publishing[user] = {"since": _ns(st.get("started_at") or st.get("startedAt")) or now, "via": via, "video": _video_of(st)}
    # Who's watching each one (everyone in the room but the stream itself).
    for user, p in publishing.items():
        room = ROOM_PREFIX + user
        viewers = []
        try:
            people = voice._livekit("ListParticipants", {"room": room}, {"roomAdmin": True, "room": room})
            for q in people.get("participants") or []:
                ident = q.get("identity") or ""
                if ident.startswith(ROOM_PREFIX) or q.get("state") == "DISCONNECTED":
                    continue
                who = q.get("name") or ident.split("#")[0]
                if who and who != user and who not in viewers:
                    viewers.append(who)
        except (httpx.HTTPError, ValueError):
            pass
        p["viewers"] = viewers
    if not online:
        # LiveKit didn't answer: say what we last knew, and close nothing.
        with _lock:
            _state.update(at=now, online=False)
        return
    with _db() as db:
        open_rows = {r["username"]: r for r in db.execute("SELECT * FROM live_streams WHERE ended IS NULL").fetchall()}
        for user, p in publishing.items():
            r = open_rows.pop(user, None)
            video = json.dumps(p["video"]) if p["video"] else ""
            if r is None:
                k = db.execute("SELECT title, game FROM live_keys WHERE username=?", (user,)).fetchone()
                cur = db.execute(
                    "INSERT INTO live_streams (username, title, game, via, started, last_seen, peak, video) VALUES (?,?,?,?,?,?,?,?)",
                    (user, k["title"] if k else "", k["game"] if k else "", p["via"], p["since"], now, len(p["viewers"]), video))
                p["stream"] = cur.lastrowid
            else:
                db.execute("UPDATE live_streams SET last_seen=?, peak=MAX(peak, ?), via=?, video=? WHERE id=?",
                           (now, len(p["viewers"]), p["via"], video or r["video"], r["id"]))
                p["stream"] = r["id"]
                p["since"] = r["started"]
        # Not publishing: over, once the grace has passed.
        for user, r in open_rows.items():
            if now - r["last_seen"] > GRACE:
                db.execute("UPDATE live_streams SET ended=? WHERE id=?", (r["last_seen"], r["id"]))
        db.commit()
    with _lock:
        _live.clear()
        _live.update(publishing)
        _state.update(at=now, online=True, ingress=by_id)


def _loop() -> None:
    while True:
        if _enabled():
            try:
                _poll_once()
            except Exception:  # never let the thread die
                pass
        time.sleep(POLL)


def _ensure_poller() -> None:
    with _lock:
        if _state["thread"] is None and _enabled():
            t = threading.Thread(target=_loop, name="live-poll", daemon=True)
            _state["thread"] = t
            t.start()
            first = True
        else:
            first = False
    if first:
        _poll_once()


def _snapshot() -> tuple[dict, bool]:
    _ensure_poller()
    with _lock:
        return {u: dict(p) for u, p in _live.items()}, _state["online"]


def live_of(username: str) -> dict | None:
    """For presence: {title, game, started} while someone's live."""
    if not _enabled():
        return None
    with _lock:
        p = _live.get(username)
        sid = p and p.get("stream")
    if not p:
        return None
    with _db() as db:
        r = db.execute("SELECT title, game FROM live_streams WHERE id=?", (sid,)).fetchone()
    return {"title": (r["title"] if r else "") or "", "game": (r["game"] if r else "") or "", "started": p["since"]}


def now_live() -> list:
    """Every stream that's on, newest first (Together, the Live page)."""
    if not _enabled():
        return []
    snap, _ = _snapshot()
    if not snap:
        return []
    with _db() as db:
        ids = [p.get("stream") for p in snap.values() if p.get("stream")]
        rows = {r["id"]: r for r in db.execute(
            f"SELECT * FROM live_streams WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()} if ids else {}
    out = []
    for user, p in snap.items():
        r = rows.get(p.get("stream"))
        out.append({
            "username": user, "streamId": p.get("stream"), "title": (r["title"] if r else "") or "",
            "game": (r["game"] if r else "") or "", "started": p["since"], "via": p["via"],
            "video": p["video"], "viewers": len(p.get("viewers") or []), "people": (p.get("viewers") or [])[:12],
            "peak": r["peak"] if r else 0,
        })
    out.sort(key=lambda s: -s["started"])
    return out


def started_since(t: float, me: str) -> list:
    """For the recap: streams other people started since t."""
    if not _enabled():
        return []
    with _db() as db:
        # A stream that's on, or lasted half a minute (not a false start);
        # one a person, their latest.
        rows = db.execute("SELECT id, username, title, game, started, ended FROM live_streams WHERE started >= ? AND username != ? "
                          "AND (ended IS NULL OR ended - started >= 30) ORDER BY started DESC LIMIT 30", (t, me)).fetchall()
    out, seen = [], set()
    for r in rows:
        if r["username"] in seen:
            continue
        seen.add(r["username"])
        out.append({"id": r["id"], "who": r["username"], "title": r["title"], "game": r["game"], "started": r["started"],
                    "live": r["ended"] is None})
    return out[:10]


def _stream_json(r) -> dict:
    return {"id": r["id"], "username": r["username"], "title": r["title"], "game": r["game"], "via": r["via"],
            "started": r["started"], "ended": r["ended"], "peak": r["peak"],
            "length": round((r["ended"] or r["last_seen"]) - r["started"])}


# ── Stream keys ──

def _rtmp_video() -> dict:
    """RTMP is transcoded: a 1080p60 ladder, the top rung at LIVE_RTMP_KBPS."""
    top = LIVE_RTMP_KBPS * 1000
    return {"source": "CAMERA", "options": {"video_codec": "H264_BASELINE", "frame_rate": 60, "layers": [
        {"quality": "HIGH", "width": 1920, "height": 1080, "bitrate": top},
        {"quality": "MEDIUM", "width": 1280, "height": 720, "bitrate": max(1_500_000, top // 2)},
        {"quality": "LOW", "width": 640, "height": 360, "bitrate": 800_000},
    ]}}


def _create(kind: str, username: str) -> dict:
    body = {
        "input_type": "WHIP_INPUT" if kind == "whip" else "RTMP_INPUT",
        "name": f"{INGRESS_PREFIX}{kind}-{username}",
        "room_name": ROOM_PREFIX + username,
        "participant_identity": ROOM_PREFIX + username,
        "participant_name": username,
        "enable_transcoding": kind != "whip",
    }
    if kind == "rtmp":
        body["video"] = _rtmp_video()
        body["audio"] = {"source": "MICROPHONE", "options": {"audio_codec": "OPUS", "bitrate": 160000, "channels": 2}}
    return _ingress("CreateIngress", body)


def _delete(ingress_id: str) -> None:
    if not ingress_id:
        return
    try:
        _ingress("DeleteIngress", {"ingress_id": ingress_id})
    except (httpx.HTTPError, ValueError):
        pass  # already gone


_key_locks: dict[str, threading.Lock] = {}


def _keys(db, username: str, renew: tuple = ()) -> dict:
    """The person's row, with both ingresses made (or remade) as needed.
    One at a time per person: two pages opening Settings at once would
    otherwise make two of each."""
    with _lock:
        lk = _key_locks.setdefault(username, threading.Lock())
    with lk:
        return _keys_locked(db, username, renew)


def _keys_locked(db, username: str, renew: tuple) -> dict:
    r = db.execute("SELECT * FROM live_keys WHERE username=?", (username,)).fetchone()
    if r is None:
        db.execute("INSERT INTO live_keys (username, created) VALUES (?, ?)", (username, time.time()))
        r = db.execute("SELECT * FROM live_keys WHERE username=?", (username,)).fetchone()
    row = dict(r)
    # This room's own list (the poller's may be a few seconds old).
    try:
        items = _ingress("ListIngress", {"room_name": ROOM_PREFIX + username}).get("items") or []
        known = {i.get("ingress_id") or i.get("ingressId"): i for i in items}
    except (httpx.HTTPError, ValueError):
        known = None
    for kind in ("whip", "rtmp"):
        iid = row[f"{kind}_id"]
        gone = known is not None and iid and iid not in known
        if kind in renew or not iid or gone:
            if kind in renew:
                _delete(iid)
            info = _create(kind, username)
            row[f"{kind}_id"] = info.get("ingress_id") or info.get("ingressId") or ""
            row[f"{kind}_key"] = info.get("stream_key") or info.get("streamKey") or ""
            row[f"{kind}_url"] = info.get("url") or ""
            db.execute(f"UPDATE live_keys SET {kind}_id=?, {kind}_key=?, {kind}_url=? WHERE username=?",
                       (row[f"{kind}_id"], row[f"{kind}_key"], row[f"{kind}_url"], username))
            db.commit()
    # Strays (a key replaced while LiveKit didn't answer): gone, so only
    # the keys shown in Settings work.
    for iid, it in (known or {}).items():
        if iid not in (row["whip_id"], row["rtmp_id"]) and (it.get("name") or "").startswith(INGRESS_PREFIX):
            _delete(iid)
    return row


def _setup_json(row: dict) -> dict:
    snap, online = _snapshot()
    mine = snap.get(row["username"])
    err = ""
    for kind in ("whip", "rtmp"):
        st = ((_state["ingress"].get(row[f"{kind}_id"]) or {}).get("state") or {})
        if st.get("status") == "ENDPOINT_ERROR" and st.get("error"):
            err = st["error"][:200]
    rtmp_url = row["rtmp_url"]
    return {
        "enabled": True, "online": online,
        "whip": {"url": row["whip_url"], "key": row["whip_key"]},
        "rtmp": {"url": rtmp_url, "key": row["rtmp_key"], "full": (rtmp_url.rstrip("/") + "/" + row["rtmp_key"]) if rtmp_url else ""},
        "title": row["title"], "game": row["game"], "description": row["description"],
        "live": bool(mine), "via": mine["via"] if mine else None, "video": mine["video"] if mine else None,
        "started": mine["since"] if mine else None, "viewers": len(mine.get("viewers") or []) if mine else 0,
        "error": err,
    }


async def _body(request: Request) -> dict:
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


@router.get(BASE)
def list_live(request: Request):
    """Who's live now, and the streams before."""
    if not _signed_in(request):
        return _error(401, "not authenticated")
    if not _enabled():
        return {"enabled": False, "online": False, "live": [], "recent": []}
    live = now_live()
    with _db() as db:
        recent = [_stream_json(r) for r in db.execute(
            "SELECT * FROM live_streams WHERE ended IS NOT NULL AND ended - started >= 30 ORDER BY started DESC LIMIT 12").fetchall()]
        meta = {r["username"]: r["description"] for r in db.execute(
            f"SELECT username, description FROM live_keys WHERE username IN ({','.join('?' * len(live))})",
            [s["username"] for s in live]).fetchall()} if live else {}
    for s in live:
        s["description"] = meta.get(s["username"], "")
    return {"enabled": True, "online": _state["online"], "now": time.time(), "live": live, "recent": recent}


@router.get(f"{BASE}/me")
def my_setup(request: Request):
    """Your stream keys (made the first time), what the next stream is
    called, and whether klabnet sees you live right now."""
    me = _signed_in(request)
    if not me:
        return _error(401, "not authenticated")
    if not _enabled():
        return {"enabled": False}
    _ensure_poller()
    try:
        with _db() as db:
            row = _keys(db, me)
    except (httpx.HTTPError, ValueError) as e:
        return _error(503, "LiveKit Ingress isn't answering: " + str(e)[:160])
    return _setup_json(row)


@router.post(f"{BASE}/me/rotate")
async def rotate(request: Request):
    """New stream keys (both, or `which`: whip | rtmp). The old ones stop
    working at once, and a stream on them ends."""
    me = _signed_in(request)
    if not me:
        return _error(401, "not authenticated")
    if not _enabled():
        return _error(503, "streaming isn't set up")
    which = (await _body(request)).get("which") or "both"
    renew = ("whip", "rtmp") if which == "both" else (which,) if which in ("whip", "rtmp") else None
    if not renew:
        return _error(400, "which: whip, rtmp or both")
    try:
        with _db() as db:
            row = _keys(db, me, renew)
    except (httpx.HTTPError, ValueError) as e:
        return _error(503, "LiveKit Ingress isn't answering: " + str(e)[:160])
    return _setup_json(row)


@router.patch(f"{BASE}/me")
async def edit_setup(request: Request):
    """Title, game and description: for the next stream, and the one on now."""
    me = _signed_in(request)
    if not me:
        return _error(401, "not authenticated")
    if not _enabled():
        return _error(503, "streaming isn't set up")
    b = await _body(request)
    fields = {}
    for key, cap in (("title", TITLE_MAX), ("game", GAME_MAX), ("description", DESC_MAX)):
        if key in b:
            v = str(b.get(key) or "").strip()
            fields[key] = (" ".join(v.split()) if key != "description" else v)[:cap]
    with _db() as db:
        if not db.execute("SELECT 1 FROM live_keys WHERE username=?", (me,)).fetchone():
            db.execute("INSERT INTO live_keys (username, created) VALUES (?, ?)", (me, time.time()))
        for k, v in fields.items():
            db.execute(f"UPDATE live_keys SET {k}=? WHERE username=?", (v, me))
            if k in ("title", "game"):
                db.execute(f"UPDATE live_streams SET {k}=? WHERE username=? AND ended IS NULL", (v, me))
        db.commit()
        r = db.execute("SELECT title, game, description FROM live_keys WHERE username=?", (me,)).fetchone()
    return dict(r)


def _user(raw: str) -> str | None:
    u = (raw or "").strip().lower()
    return u if _USER_RE.fullmatch(u) else None


def _current(db, user: str):
    """The stream on now (or within its grace), else the last one."""
    return db.execute("SELECT * FROM live_streams WHERE username=? ORDER BY id DESC LIMIT 1", (user,)).fetchone()


@router.get(f"{BASE}/{{user}}")
def one_stream(user: str, request: Request):
    if not _signed_in(request):
        return _error(401, "not authenticated")
    user = _user(user)
    if not user:
        return _error(404, "no such person")
    if not _enabled():
        return {"enabled": False}
    live = next((s for s in now_live() if s["username"] == user), None)
    with _db() as db:
        last = _current(db, user)
        k = db.execute("SELECT title, game, description FROM live_keys WHERE username=?", (user,)).fetchone()
    if live:
        live["description"] = k["description"] if k else ""
    return {"enabled": True, "now": time.time(), "live": live, "last": _stream_json(last) if last else None,
            "next": {"title": k["title"], "game": k["game"]} if k else None}


@router.post(f"{BASE}/{{user}}/token")
def viewer_token(user: str, request: Request):
    """A token to watch: into the stream's room, subscribe only. Each page
    is its own participant (username#random), so two tabs don't kick each
    other out; the name is the username, which is what's counted."""
    me = _signed_in(request)
    if not me:
        return _error(401, "not authenticated")
    user = _user(user)
    if not user:
        return _error(404, "no such person")
    if not _enabled():
        return _error(503, "streaming isn't set up")
    room = ROOM_PREFIX + user
    token = voice._token({
        "sub": f"{me}#{secrets.token_hex(3)}", "name": me,
        "video": {"room": room, "roomJoin": True, "canSubscribe": True, "canPublish": False,
                  "canPublishData": False, "canUpdateOwnMetadata": False},
    }, TOKEN_TTL)
    return {"url": LIVEKIT_URL, "token": token, "room": room, "identity": ROOM_PREFIX + user}


# ── A stream's chat ──

def _chat_json(r) -> dict:
    return {"id": r["id"], "username": r["username"], "body": r["body"], "ts": r["ts"]}


@router.get(f"{BASE}/{{user}}/chat")
def stream_chat(user: str, request: Request, after: int = 0, stream: int = 0):
    if not _signed_in(request):
        return _error(401, "not authenticated")
    user = _user(user)
    if not user:
        return _error(404, "no such person")
    with _db() as db:
        s = db.execute("SELECT id FROM live_streams WHERE id=? AND username=?", (stream, user)).fetchone() if stream else _current(db, user)
        if not s:
            return {"stream": None, "messages": []}
        if after > 0:
            rows = db.execute("SELECT * FROM live_chat WHERE stream=? AND id>? ORDER BY id LIMIT 200", (s["id"], after)).fetchall()
        else:
            rows = db.execute("SELECT * FROM (SELECT * FROM live_chat WHERE stream=? ORDER BY id DESC LIMIT 150) ORDER BY id", (s["id"],)).fetchall()
    return {"stream": s["id"], "messages": [_chat_json(r) for r in rows]}


@router.post(f"{BASE}/{{user}}/chat")
async def post_chat(user: str, request: Request):
    me = _signed_in(request)
    if not me:
        return _error(401, "not authenticated")
    user = _user(user)
    if not user:
        return _error(404, "no such person")
    body = str((await _body(request)).get("body") or "").strip()[:_CHAT_MAX]
    if not body:
        return _error(400, "say something")
    with _db() as db:
        s = db.execute("SELECT id FROM live_streams WHERE username=? AND ended IS NULL ORDER BY id DESC LIMIT 1", (user,)).fetchone()
        if not s:
            return _error(409, f"{user} isn't live")
        cur = db.execute("INSERT INTO live_chat (stream, username, body, ts) VALUES (?,?,?,?)", (s["id"], me, body, time.time()))
        # The newest _CHAT_KEEP of this stream (ids are shared by every
        # stream's chat, so lastrowid - KEEP kept fewer with two live).
        db.execute("DELETE FROM live_chat WHERE stream=? AND id < (SELECT id FROM live_chat WHERE stream=? "
                   "ORDER BY id DESC LIMIT 1 OFFSET ?)", (s["id"], s["id"], _CHAT_KEEP - 1))
        db.commit()
        row = db.execute("SELECT * FROM live_chat WHERE id=?", (cur.lastrowid,)).fetchone()
    return _chat_json(row)


@router.delete(f"{BASE}/chat/{{msg_id}}")
def delete_chat(msg_id: int, request: Request):
    """Whoever wrote it, the streamer or an admin."""
    me = _signed_in(request)
    if not me:
        return _error(401, "not authenticated")
    with _db() as db:
        r = db.execute("SELECT c.username, s.username AS streamer FROM live_chat c JOIN live_streams s ON s.id = c.stream WHERE c.id=?",
                       (msg_id,)).fetchone()
        if not r:
            return _error(404, "no such message")
        if me not in (r["username"], r["streamer"]) and not is_admin(get_groups(request)):
            return _error(403, "not yours")
        db.execute("DELETE FROM live_chat WHERE id=?", (msg_id,))
        db.commit()
    return {"ok": True}
