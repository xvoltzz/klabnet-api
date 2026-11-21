"""Chat admin: klab's Matrix rooms, managed from klabnet (2026-11-21, the
owner: "matrix room management needs some HELP cuz i cant manage who sees
what for shit and i cant see what people create and thats a problem").

klabnet-api holds a Synapse server admin's access token (MATRIX_ADMIN_TOKEN)
and talks to Synapse for klabnet's admins (the Authentik admin group):

  GET    /api/posts/chat-admin                     {enabled}: is it set up, and are you an admin
  GET    /api/posts/chat-admin/rooms               every room: kind (channel, dm, space), name,
                                                   alias, creator, created, members, who can join,
                                                   listed or not, history, encryption
  GET    /api/posts/chat-admin/rooms/{id}          one room: the above, topic, and everyone in it
                                                   (joined, invited, banned) with their role
  PATCH  /api/posts/chat-admin/rooms/{id}          {join_rule?, listed?, history?, name?, topic?}
  POST   /api/posts/chat-admin/rooms/{id}/members  {user, action: invite|kick|ban|unban|role, role?, reason?}
  DELETE /api/posts/chat-admin/rooms/{id}          {purge?}: everyone is removed and it's gone

Changing a room is done as the token's user: if it isn't an admin of the
room, Synapse's make_room_admin makes it one (joining it if need be), and
if it joined only for this, it leaves again afterwards. Message contents
are never read. Under /api/posts/ because Caddy only forwards a fixed list
of /api prefixes."""
import asyncio
import time
import urllib.parse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_groups, get_username, is_admin
from ..config import MATRIX_ADMIN_TOKEN, MATRIX_URL
from ..db import get_db

router = APIRouter()
BASE = "/api/posts/chat-admin"
ROLES = {"admin": 100, "mod": 50, "member": 0}
JOIN_RULES = ("public", "invite", "knock")
HISTORY = ("shared", "joined", "invited", "world_readable")

_cache: dict = {"rooms": None, "at": 0.0}
_me: dict = {"id": None}


class SynapseError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def enabled() -> bool:
    return bool(MATRIX_ADMIN_TOKEN)


def _call(method: str, path: str, body=None, timeout: float = 20):
    try:
        r = httpx.request(method, MATRIX_URL + path, json=body, timeout=timeout,
                          headers={"Authorization": "Bearer " + MATRIX_ADMIN_TOKEN})
    except httpx.HTTPError as e:
        raise SynapseError(502, f"couldn't reach Matrix ({e.__class__.__name__})")
    if r.status_code >= 400:
        try:
            j = r.json()
            msg = str((j.get("error") if isinstance(j, dict) else "") or r.text)
        except ValueError:
            msg = r.text
        raise SynapseError(r.status_code, msg[:300] or f"Matrix said {r.status_code}")
    try:
        out = r.json() if r.content else {}
    except ValueError:
        raise SynapseError(502, "Matrix sent something that isn't JSON")
    return out if isinstance(out, dict) else {}


def _q(room_id: str) -> str:
    return urllib.parse.quote(room_id, safe="")


def _admin_id() -> str:
    if not _me["id"]:
        _me["id"] = _call("GET", "/_matrix/client/v3/account/whoami")["user_id"]
    return _me["id"]


def _int(v) -> int:
    # Power levels from old clients can be strings, or junk.
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _username(mxid: str) -> str:
    return (mxid or "").lstrip("@").split(":")[0]


def _guard(request: Request):
    if not enabled():
        return JSONResponse(status_code=404, content={"error": "chat admin isn't set up (MATRIX_ADMIN_TOKEN)"})
    if not is_admin(get_groups(request)):
        return JSONResponse(status_code=403, content={"error": "admins only"})
    return None


def _fail(e: SynapseError):
    return JSONResponse(status_code=e.status if 400 <= e.status < 600 else 502, content={"error": str(e)})


# ── When each room was made, and by whom ──
# m.room.create never changes, so it's asked for once a room and kept.
def _ensure_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS matrix_rooms (
        room_id TEXT PRIMARY KEY, created_ts INTEGER NOT NULL DEFAULT 0, creator TEXT NOT NULL DEFAULT '')""")


def _created(room_ids: list[str]) -> dict:
    with get_db() as db:
        _ensure_table(db)
        db.commit()
        have = {r["room_id"]: (r["created_ts"], r["creator"]) for r in db.execute("SELECT * FROM matrix_rooms").fetchall()}
    # Asked of Synapse with no transaction open: an INSERT between the calls
    # held SQLite's write lock for every room's round trip, stalling every
    # other write in the API.
    new = {}
    for rid in room_ids:
        if rid in have or rid in new:
            continue
        ts, creator = 0, ""
        try:
            for ev in _call("GET", f"/_synapse/admin/v1/rooms/{_q(rid)}/state", timeout=10).get("state", []):
                if ev.get("type") == "m.room.create":
                    ts = int(ev.get("origin_server_ts") or 0)
                    creator = ev.get("sender") or (ev.get("content") or {}).get("creator", "")
                    break
        except SynapseError:
            continue
        new[rid] = (ts, creator)
    if new:
        with get_db() as db:
            db.executemany("INSERT OR REPLACE INTO matrix_rooms(room_id, created_ts, creator) VALUES (?, ?, ?)",
                           [(rid, ts, c) for rid, (ts, c) in new.items()])
            db.commit()
        have.update(new)
    return have


def _kind(r: dict) -> str:
    if r.get("room_type") == "m.space":
        return "space"
    if not r.get("name") and not r.get("canonical_alias") and (r.get("joined_members") or 0) <= 2:
        return "dm"
    return "channel"


def rooms(fresh: bool = False) -> list[dict]:
    """Every room on the server, newest first (cached 30s)."""
    if not fresh and _cache["rooms"] is not None and time.time() - _cache["at"] < 30:
        return _cache["rooms"]
    out, start = [], 0
    while True:
        page = _call("GET", f"/_synapse/admin/v1/rooms?limit=500&from={start}")
        out.extend(page.get("rooms", []))
        nxt = page.get("next_batch")
        if nxt is None or not page.get("rooms"):
            break
        start = nxt
    made = _created([r["room_id"] for r in out])
    me = _admin_id()
    rows = []
    for r in out:
        ts, creator = made.get(r["room_id"], (0, r.get("creator") or ""))
        rows.append({
            "id": r["room_id"], "kind": _kind(r), "name": r.get("name") or "", "alias": r.get("canonical_alias") or "",
            "creator": _username(creator or r.get("creator") or ""), "created": ts // 1000 if ts else 0,
            "members": r.get("joined_members") or 0, "joinRule": r.get("join_rules") or "invite",
            "listed": bool(r.get("public")), "history": r.get("history_visibility") or "shared",
            "encrypted": bool(r.get("encryption")), "guests": r.get("guest_access") == "can_join",
            "version": r.get("version") or "", "admin": _username(me),
        })
    rows.sort(key=lambda x: x["created"], reverse=True)
    _cache.update(rooms=rows, at=time.time())
    return rows


def created_since(since: float) -> list[dict]:
    """For the recap: channels and spaces made after `since` (DMs aren't news)."""
    if not enabled():
        return []
    try:
        return [{"id": r["id"], "name": r["name"] or r["alias"], "who": r["creator"], "at": r["created"], "kind": r["kind"],
                 "joinRule": r["joinRule"]} for r in rooms() if r["created"] > since and r["kind"] != "dm"][:10]
    except SynapseError:
        return []


@router.get(BASE)
def status(request: Request):
    return {"enabled": enabled() and is_admin(get_groups(request))}


@router.get(BASE + "/rooms")
def list_rooms(request: Request, fresh: int = 0):
    if (g := _guard(request)):
        return g
    try:
        return {"rooms": rooms(bool(fresh))}
    except SynapseError as e:
        return _fail(e)


def _state(room_id: str) -> list[dict]:
    return _call("GET", f"/_synapse/admin/v1/rooms/{_q(room_id)}/state").get("state", [])


@router.get(BASE + "/rooms/{room_id}")
def room(room_id: str, request: Request):
    if (g := _guard(request)):
        return g
    try:
        base = _call("GET", f"/_synapse/admin/v1/rooms/{_q(room_id)}")
        state = _state(room_id)
        me = _admin_id()
    except SynapseError as e:
        return _fail(e)
    pl, topic, members, owners = {}, "", [], set()
    for ev in state:
        t, c = ev.get("type"), ev.get("content") or {}
        # Room version 12 on: whoever made the room (and any additional
        # creators) outranks every level and isn't in power_levels at all.
        if t == "m.room.create" and str(c.get("room_version") or "1").isdigit() and int(c.get("room_version") or 1) >= 12:
            owners = {ev.get("sender")} | set(c.get("additional_creators") or [])
        if t == "m.room.power_levels":
            pl = c
        elif t == "m.room.topic":
            topic = c.get("topic") or ""
        elif t == "m.room.member":
            members.append({"id": ev.get("state_key"), "membership": c.get("membership"), "name": c.get("displayname") or "",
                            "since": int(ev.get("origin_server_ts") or 0) // 1000, "by": _username(ev.get("sender") or "")})
    users = pl.get("users") or {}
    default = _int(pl.get("users_default"))
    for m in members:
        lvl = _int(users.get(m["id"], default))
        m["level"] = 1000 if m["id"] in owners else lvl
        m["role"] = "owner" if m["id"] in owners else "admin" if lvl >= 100 else "mod" if lvl >= 50 else "member"
        m["user"] = _username(m["id"])
        m["isAdminBot"] = m["id"] == me
    order = {"join": 0, "invite": 1, "knock": 2, "ban": 3, "leave": 4}
    members = [m for m in members if m["membership"] in ("join", "invite", "knock", "ban")]
    members.sort(key=lambda m: (order.get(m["membership"], 9), -m["level"], m["user"]))
    made = _created([room_id]).get(room_id, (0, ""))
    r = {
        "id": room_id, "kind": _kind(base), "name": base.get("name") or "", "alias": base.get("canonical_alias") or "",
        "topic": topic, "creator": _username(made[1] or base.get("creator") or ""), "created": made[0] // 1000 if made[0] else 0,
        "members": base.get("joined_members") or 0, "joinRule": base.get("join_rules") or "invite",
        "listed": bool(base.get("public")), "history": base.get("history_visibility") or "shared",
        "encrypted": bool(base.get("encryption")), "version": base.get("room_version") or base.get("version") or "",
        "people": members, "admin": _username(me),
    }
    return r


# ── Acting in a room as the token's user ──
class _AsAdmin:
    """Makes the token's user a room admin for the length of a change (Synapse's
    make_room_admin, which joins it if need be), and leaves again after if it
    joined only for this."""

    def __init__(self, room_id: str):
        self.room_id = room_id
        self.joined_here = False

    def __enter__(self):
        me = _admin_id()
        state = _state(self.room_id)
        member = next((ev for ev in state if ev.get("type") == "m.room.member" and ev.get("state_key") == me), None)
        joined = bool(member and (member.get("content") or {}).get("membership") == "join")
        pl = next((ev.get("content") or {} for ev in state if ev.get("type") == "m.room.power_levels"), {})
        level = _int((pl.get("users") or {}).get(me, pl.get("users_default")))
        if not joined or level < 100:
            _call("POST", f"/_synapse/admin/v1/rooms/{_q(self.room_id)}/make_room_admin", {"user_id": me})
            self.joined_here = not joined
            # In an invite-only room make_room_admin only invites: accept it.
            if not joined:
                _call("POST", f"/_matrix/client/v3/join/{_q(self.room_id)}", {})
        return self

    def __exit__(self, *exc):
        if self.joined_here:
            try:
                _call("POST", f"/_matrix/client/v3/rooms/{_q(self.room_id)}/leave", {})
            except SynapseError:
                pass
        _cache["rooms"] = None
        return False


def _put_state(room_id: str, etype: str, content: dict, key: str = ""):
    _call("PUT", f"/_matrix/client/v3/rooms/{_q(room_id)}/state/{etype}/{urllib.parse.quote(key, safe='')}", content)


@router.patch(BASE + "/rooms/{room_id}")
async def change_room(room_id: str, request: Request):
    if (g := _guard(request)):
        return g
    try:
        body = await request.json()
        assert isinstance(body, dict)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    if "join_rule" in body and body["join_rule"] not in JOIN_RULES:
        return JSONResponse(status_code=400, content={"error": "join_rule is public, invite or knock"})
    if "history" in body and body["history"] not in HISTORY:
        return JSONResponse(status_code=400, content={"error": "history is shared, joined, invited or world_readable"})
    # Synapse is asked with blocking calls (several, up to 20s each): off the
    # event loop, or the whole API waits on them.
    return await asyncio.to_thread(_change_room, room_id, body, request)


def _change_room(room_id: str, body: dict, request: Request):
    try:
        with _AsAdmin(room_id):
            # The directory first: Synapse's room_list_publication_rules can
            # refuse it, and then nothing else should have changed.
            if "listed" in body:
                try:
                    _call("PUT", f"/_matrix/client/v3/directory/list/room/{_q(room_id)}",
                          {"visibility": "public" if body["listed"] else "private"})
                except SynapseError as e:
                    if e.status == 403:
                        raise SynapseError(403, "Synapse won't list rooms in Browse channels: its room_list_publication_rules setting doesn't allow it")
                    raise
            if "join_rule" in body:
                _put_state(room_id, "m.room.join_rules", {"join_rule": body["join_rule"]})
            if "history" in body:
                _put_state(room_id, "m.room.history_visibility", {"history_visibility": body["history"]})
            if "name" in body:
                _put_state(room_id, "m.room.name", {"name": str(body["name"] or "").strip()[:100]})
            if "topic" in body:
                _put_state(room_id, "m.room.topic", {"topic": str(body["topic"] or "").strip()[:1000]})
    except SynapseError as e:
        return _fail(e)
    return room(room_id, request)


@router.post(BASE + "/rooms/{room_id}/members")
async def member(room_id: str, request: Request):
    if (g := _guard(request)):
        return g
    try:
        body = await request.json()
        assert isinstance(body, dict)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    action = body.get("action")
    user = str(body.get("user") or "").strip()
    if not user:
        return JSONResponse(status_code=400, content={"error": "who?"})
    if not user.startswith("@"):
        # A klabnet username: on this server.
        try:
            server = (await asyncio.to_thread(_admin_id)).split(":", 1)[1]
        except SynapseError as e:
            return _fail(e)
        user = f"@{user.lower()}:{server}"
    reason = str(body.get("reason") or "").strip()[:300]
    return await asyncio.to_thread(_member, room_id, body, action, user, reason, get_username(request), request)


def _member(room_id: str, body: dict, action, user: str, reason: str, me: str, request: Request):
    try:
        with _AsAdmin(room_id):
            q = _q(room_id)
            if action in ("invite", "kick", "ban", "unban"):
                payload = {"user_id": user}
                if reason or action in ("kick", "ban"):
                    payload["reason"] = reason or f"by {me} (klabnet admin)"
                _call("POST", f"/_matrix/client/v3/rooms/{q}/{action}", payload)
            elif action == "role":
                role = body.get("role")
                if role not in ROLES:
                    return JSONResponse(status_code=400, content={"error": "role is admin, mod or member"})
                pl = next((ev.get("content") or {} for ev in _state(room_id) if ev.get("type") == "m.room.power_levels"), {})
                users = dict(pl.get("users") or {})
                if ROLES[role] == _int(pl.get("users_default")):
                    users.pop(user, None)
                else:
                    users[user] = ROLES[role]
                _put_state(room_id, "m.room.power_levels", {**pl, "users": users})
            else:
                return JSONResponse(status_code=400, content={"error": "action is invite, kick, ban, unban or role"})
    except SynapseError as e:
        return _fail(e)
    return room(room_id, request)


@router.delete(BASE + "/rooms/{room_id}")
async def delete_room(room_id: str, request: Request):
    """Everyone is removed and the room is shut (Synapse's v2 delete, which
    runs in the background). purge: its history is wiped from the server too."""
    if (g := _guard(request)):
        return g
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    return await asyncio.to_thread(_delete_room, room_id, body)


def _delete_room(room_id: str, body: dict):
    try:
        out = _call("DELETE", f"/_synapse/admin/v2/rooms/{_q(room_id)}",
                    {"purge": bool(body.get("purge", True)), "block": False,
                     "message": "This room was closed by a klabnet admin."}, timeout=30)
    except SynapseError as e:
        return _fail(e)
    _cache["rooms"] = None
    with get_db() as db:
        _ensure_table(db)
        db.execute("DELETE FROM matrix_rooms WHERE room_id=?", (room_id,))
        db.commit()
    return {"ok": True, "deleteId": out.get("delete_id")}


@router.post(BASE + "/rooms/{room_id}/claim")
def claim(room_id: str, request: Request):
    """Make the token's user an admin of the room and stay in it (with the
    owner's own token as MATRIX_ADMIN_TOKEN, that's the owner: full rights
    in every room they claim)."""
    if (g := _guard(request)):
        return g
    try:
        me = _admin_id()
        state = _state(room_id)
        member = next((ev for ev in state if ev.get("type") == "m.room.member" and ev.get("state_key") == me), None)
        if not (member and (member.get("content") or {}).get("membership") == "join"):
            _call("POST", f"/_synapse/admin/v1/rooms/{_q(room_id)}/make_room_admin", {"user_id": me})
            member = next((ev for ev in _state(room_id) if ev.get("type") == "m.room.member" and ev.get("state_key") == me), None)
            if not (member and (member.get("content") or {}).get("membership") == "join"):
                _call("POST", f"/_matrix/client/v3/join/{_q(room_id)}", {})
        else:
            _call("POST", f"/_synapse/admin/v1/rooms/{_q(room_id)}/make_room_admin", {"user_id": me})
    except SynapseError as e:
        return _fail(e)
    _cache["rooms"] = None
    return room(room_id, request)
