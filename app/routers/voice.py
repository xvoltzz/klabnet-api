"""Voice: drop-in channels, like Mumble's or Discord's.

The audio itself never comes near this service. It runs through a LiveKit
server on the homelab (compose.yml, the "voice" profile), which browsers
reach directly over WebRTC. What's here:

- the channels (a name each, in SQLite; admins add, rename and delete them)
- who's in each one right now, asked of LiveKit and cached for a moment,
  so people who haven't joined can see who has
- the token that lets the caller into a channel's room, signed with
  LiveKit's secret and carrying their klabnet username as the identity.
  That's the whole sign-in: LiveKit trusts the signature, and this service
  only signs for someone Authentik already let through.

One identity per person: joining from a second device takes over from the
first, the way LiveKit handles a duplicate identity.

Caddy forwards a fixed list of /api prefixes; /api/voice* has to be on it.
"""

import base64
import hashlib
import hmac
import json
import threading
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_groups, get_username, is_admin
from ..config import (
    LIVEKIT_API_KEY,
    LIVEKIT_API_SECRET,
    LIVEKIT_API_URL,
    LIVEKIT_URL,
    VOICE_CHANNEL_NAME_MAX_CHARS,
    VOICE_MAX_CHANNELS,
)
from ..db import get_db

router = APIRouter()

ROOM_PREFIX = "klabnet-voice-"
# Long enough to connect with; LiveKit hands a connected client fresh ones.
TOKEN_TTL = 600
# How stale "who's in there" may be. Every open klabnet asks every few
# seconds; LiveKit is asked at most this often however many are asking.
OCCUPANCY_TTL = 2.0

_http = httpx.Client(timeout=httpx.Timeout(3.0, connect=2.0))
_occupancy_lock = threading.Lock()
_occupancy_cache = {"at": 0.0, "rooms": {}, "online": False}


def _unauthenticated() -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": "not authenticated"})


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _enabled() -> bool:
    return bool(LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _token(claims: dict, ttl: int) -> str:
    """A LiveKit access token: an HS256 JWT issued by the API key."""
    now = int(time.time())
    payload = {"iss": LIVEKIT_API_KEY, "nbf": now - 10, "exp": now + ttl, **claims}
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(LIVEKIT_API_SECRET.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(sig)}"


def _livekit(method: str, body: dict, grant: dict) -> dict:
    """One call to LiveKit's room service (Twirp: JSON over POST)."""
    r = _http.post(
        f"{LIVEKIT_API_URL}/twirp/livekit.RoomService/{method}",
        json=body,
        headers={"Authorization": "Bearer " + _token({"video": grant}, 60)},
    )
    r.raise_for_status()
    return r.json()


def _member(p: dict) -> dict:
    # The site says "I muted myself" and "I deafened myself" in participant
    # attributes. The track's own mute bit can't stand in for the first: a
    # push-to-talk microphone is muted between words without its owner
    # being muted.
    attrs = p.get("attributes") or {}
    mic = [t for t in p.get("tracks") or [] if t.get("source") == "MICROPHONE"]
    return {
        "username": p.get("identity", ""),
        # No microphone at all (it was blocked) reads as muted too.
        "muted": not mic or attrs.get("mute") == "1",
        "deafened": attrs.get("deaf") == "1",
        "joined": int(p.get("joined_at") or p.get("joinedAt") or 0),
    }


def _occupancy() -> tuple[dict, bool]:
    """({room name: [member, ...]}, whether LiveKit answered)."""
    with _occupancy_lock:
        cache = _occupancy_cache
        if time.monotonic() - cache["at"] < OCCUPANCY_TTL:
            return cache["rooms"], cache["online"]
        rooms, online = {}, True
        try:
            # A room exists from its first join until a few minutes after
            # its last leave, so this is only ever the channels in use.
            for room in _livekit("ListRooms", {}, {"roomList": True}).get("rooms") or []:
                name = room.get("name", "")
                if not name.startswith(ROOM_PREFIX):
                    continue
                people = _livekit("ListParticipants", {"room": name}, {"roomAdmin": True, "room": name})
                members = [
                    _member(p) for p in people.get("participants") or []
                    if p.get("identity") and p.get("state") != "DISCONNECTED"
                ]
                if members:
                    rooms[name] = sorted(members, key=lambda m: (m["joined"], m["username"]))
        except (httpx.HTTPError, ValueError):
            rooms, online = {}, False
        cache.update(at=time.monotonic(), rooms=rooms, online=online)
        return rooms, online


def _forget_occupancy() -> None:
    with _occupancy_lock:
        _occupancy_cache["at"] = 0.0


def _channels(db) -> list:
    return db.execute("SELECT id, name FROM voice_channels ORDER BY position, id").fetchall()


def _clean_name(raw) -> str:
    return " ".join(str(raw or "").split())[:VOICE_CHANNEL_NAME_MAX_CHARS]


async def _json(request: Request):
    try:
        body = await request.json()
        return body if isinstance(body, dict) else None
    except Exception:
        return None


@router.get("/api/voice")
def get_voice(request: Request):
    """The channels and who's in them. `enabled` false means there's no
    LiveKit configured; `online` false means it isn't answering."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    can_manage = is_admin(get_groups(request))
    if not _enabled():
        return {"enabled": False, "online": False, "canManage": can_manage, "channels": []}
    rooms, online = _occupancy()
    with get_db() as db:
        channels = [
            {"id": c["id"], "name": c["name"], "members": rooms.get(f"{ROOM_PREFIX}{c['id']}", [])}
            for c in _channels(db)
        ]
    return {"enabled": True, "online": online, "canManage": can_manage, "channels": channels}


@router.post("/api/voice/token")
async def voice_token(request: Request):
    """What a browser needs to join a channel: where LiveKit is, and a
    token for that channel's room in the caller's name."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    if not _enabled():
        return _error(503, "voice isn't set up")
    body = await _json(request)
    try:
        channel_id = int((body or {}).get("channel"))
    except (TypeError, ValueError):
        return _error(400, "channel required")
    with get_db() as db:
        channel = db.execute("SELECT id, name FROM voice_channels WHERE id=?", (channel_id,)).fetchone()
    if not channel:
        return _error(404, "no such channel")
    room = f"{ROOM_PREFIX}{channel['id']}"
    token = _token(
        {
            "sub": username,
            "name": username,
            "video": {
                "room": room,
                "roomJoin": True,
                "canSubscribe": True,
                "canPublish": True,
                # Voice only: a microphone, nothing else.
                "canPublishSources": ["microphone"],
                "canPublishData": True,
                # For the muted/deafened flags others see (participant attributes).
                "canUpdateOwnMetadata": True,
            },
        },
        TOKEN_TTL,
    )
    _forget_occupancy()  # someone's about to walk in: the next look should see them
    return {"url": LIVEKIT_URL, "token": token, "channel": {"id": channel["id"], "name": channel["name"]}}


@router.post("/api/voice/channels")
async def create_channel(request: Request):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    if not is_admin(get_groups(request)):
        return _error(403, "admin access required")
    name = _clean_name((await _json(request) or {}).get("name"))
    if not name:
        return _error(400, "name required")
    with get_db() as db:
        existing = _channels(db)
        if len(existing) >= VOICE_MAX_CHANNELS:
            return _error(400, f"that's the limit of {VOICE_MAX_CHANNELS} channels")
        if any(c["name"].lower() == name.lower() for c in existing):
            return _error(409, "there's already a channel with that name")
        position = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM voice_channels").fetchone()[0]
        cur = db.execute(
            "INSERT INTO voice_channels(name, position, created_by) VALUES (?, ?, ?)", (name, position, username)
        )
        db.commit()
        return {"id": cur.lastrowid, "name": name, "members": []}


@router.patch("/api/voice/channels/{channel_id}")
async def rename_channel(channel_id: int, request: Request):
    if get_username(request) == "anonymous":
        return _unauthenticated()
    if not is_admin(get_groups(request)):
        return _error(403, "admin access required")
    name = _clean_name((await _json(request) or {}).get("name"))
    if not name:
        return _error(400, "name required")
    with get_db() as db:
        if not db.execute("SELECT 1 FROM voice_channels WHERE id=?", (channel_id,)).fetchone():
            return _error(404, "no such channel")
        if any(c["name"].lower() == name.lower() and c["id"] != channel_id for c in _channels(db)):
            return _error(409, "there's already a channel with that name")
        db.execute("UPDATE voice_channels SET name=? WHERE id=?", (name, channel_id))
        db.commit()
    return {"id": channel_id, "name": name}


@router.delete("/api/voice/channels/{channel_id}")
def delete_channel(channel_id: int, request: Request):
    """Deletes the channel and puts out whoever's in it."""
    if get_username(request) == "anonymous":
        return _unauthenticated()
    if not is_admin(get_groups(request)):
        return _error(403, "admin access required")
    with get_db() as db:
        if not db.execute("SELECT 1 FROM voice_channels WHERE id=?", (channel_id,)).fetchone():
            return _error(404, "no such channel")
        if len(_channels(db)) <= 1:
            return _error(400, "keep at least one channel")
        db.execute("DELETE FROM voice_channels WHERE id=?", (channel_id,))
        db.commit()
    if _enabled():
        try:
            _livekit("DeleteRoom", {"room": f"{ROOM_PREFIX}{channel_id}"}, {"roomCreate": True})
        except (httpx.HTTPError, ValueError):
            pass  # no such room (nobody was in it), or LiveKit is down: nothing to close
        _forget_occupancy()
    return {"ok": True}
