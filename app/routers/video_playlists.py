"""Video playlists: Music's playlists (routers/playlists.py) for videos. A
name, a description, a cover and an ordered list of videos.

Private to its owner until featured; a featured one is listed for everyone
(Videos -> Playlists, Browse's shelf) and anyone can watch it. Only the
owner (or an admin) changes it. Covers go through Music's playlist cover
pipeline (playlists._write_cover, served from its /covers route); with none
the site shows the first videos' posters as a 2x2.

Under /api/posts/videos/playlists (Caddy forwards /api/posts). Included
into videos.router ahead of its /{post_id} routes, so "playlists" is never
taken for a video id. videos.py is imported lazily: it imports this file.
"""
import secrets

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse

from ..auth import get_groups, get_username, is_admin
from ..config import PLAYLIST_COVER_MAX_MB, PLAYLIST_DESC_MAX_CHARS, PLAYLIST_NAME_MAX_CHARS, PLAYLISTS_PER_USER
from ..db import get_db
from ..media import media_ready

router = APIRouter()
BASE = "/api/posts/videos/playlists"
MAX_VIDEOS = 5000
_COVERS = "/api/music-requests/playlists/covers"


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _me(request: Request) -> str | None:
    u = get_username(request)
    return None if u == "anonymous" else u


def _visible(row, username: str) -> bool:
    return row is not None and (row["featured"] or row["username"] == username)


def _can_edit(row, request: Request, username: str) -> bool:
    return row["username"] == username or is_admin(get_groups(request))


async def _body(request: Request) -> dict | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _fields(body: dict) -> dict | JSONResponse:
    out = {}
    if "name" in body:
        name = " ".join(str(body["name"] or "").split())[:PLAYLIST_NAME_MAX_CHARS]
        if not name:
            return _error(400, "a playlist needs a name")
        out["name"] = name
    if "description" in body:
        desc = str(body["description"] or "").strip()
        if len(desc) > PLAYLIST_DESC_MAX_CHARS:
            return _error(400, f"a description can be up to {PLAYLIST_DESC_MAX_CHARS} characters")
        out["description"] = desc
    if "featured" in body:
        out["featured"] = 1 if body["featured"] else 0
    return out


def _ids(raw) -> list[int] | None:
    if not isinstance(raw, list):
        return None
    out, seen = [], set()
    for x in raw[:MAX_VIDEOS]:
        try:
            i = int(x)
        except (TypeError, ValueError, OverflowError):
            return None
        if not 0 < i < 2 ** 63:
            continue  # SQLite can't bind it (a 500 in _shown); no such video anyway
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _shown(db, ids: list[int], username: str) -> list[int]:
    """Of these video ids, the ones that exist and the viewer may see."""
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    ok = {r[0] for r in db.execute(
        f"""SELECT v.post_id FROM videos v JOIN posts p ON p.id = v.post_id
            WHERE v.post_id IN ({marks}) AND (v.status='ready' OR p.username=?)""", (*ids, username))}
    return [i for i in ids if i in ok]


def _summaries(db, rows, username: str, request: Request) -> list[dict]:
    from .videos import _urls
    admin = is_admin(get_groups(request))
    out = []
    for r in rows:
        items = db.execute(
            """SELECT v.post_id, v.file_id, v.status, v.poster, v.storyboard, v.original, v.duration, p.username
               FROM video_playlist_items i JOIN videos v ON v.post_id = i.video_id JOIN posts p ON p.id = v.post_id
               WHERE i.playlist_id=? AND (v.status='ready' OR p.username=?) ORDER BY i.pos""", (r["id"], username)).fetchall()
        posters = [u for u in (_urls(x)["posterSmall"] for x in items) if u][:4]
        out.append({
            "id": r["id"], "name": r["name"], "description": r["description"], "username": r["username"],
            "featured": bool(r["featured"]), "count": len(items),
            "duration": round(sum(x["duration"] or 0 for x in items)),
            "cover": f"{_COVERS}/{r['cover']}/l" if r["cover"] else None,
            "coverSmall": f"{_COVERS}/{r['cover']}/s" if r["cover"] else None,
            "posters": posters, "created": r["created"], "updated": r["updated"],
            "mine": r["username"] == username, "canEdit": r["username"] == username or admin,
        })
    return out


def _row(db, playlist_id: str):
    return db.execute("SELECT * FROM video_playlists WHERE id=?", (playlist_id,)).fetchone()


def _full(db, playlist_id: str, request: Request, username: str) -> dict:
    from .videos import _COLS, _json_many
    row = _row(db, playlist_id)
    out = _summaries(db, [row], username, request)[0]
    rows = db.execute(
        f"""SELECT {_COLS} FROM video_playlist_items i JOIN videos v ON v.post_id = i.video_id
            JOIN posts p ON p.id = v.post_id
            WHERE i.playlist_id=? AND (v.status='ready' OR p.username=?) ORDER BY i.pos""", (playlist_id, username)).fetchall()
    out["videos"] = _json_many(db, rows, request, username)
    return out


def _touch(db, playlist_id: str) -> None:
    db.execute("UPDATE video_playlists SET updated=datetime('now') WHERE id=?", (playlist_id,))


def _append(db, playlist_id: str, ids: list[int], username: str) -> int:
    have = {r[0] for r in db.execute("SELECT video_id FROM video_playlist_items WHERE playlist_id=?", (playlist_id,))}
    top = db.execute("SELECT COALESCE(MAX(pos), -1) FROM video_playlist_items WHERE playlist_id=?", (playlist_id,)).fetchone()[0]
    added = 0
    for i in ids:
        if i in have or len(have) >= MAX_VIDEOS:
            continue
        top += 1
        have.add(i)
        db.execute("INSERT INTO video_playlist_items(playlist_id, pos, video_id, added_by) VALUES (?, ?, ?, ?)",
                   (playlist_id, top, i, username))
        added += 1
    return added


def create_playlist_row(db, username: str, name: str, description: str = "", featured: bool = False,
                        videos: list[int] | None = None, source_id: str = "") -> str:
    """A new playlist (also the importer's way in): its id."""
    pid = secrets.token_urlsafe(9)
    db.execute("""INSERT INTO video_playlists(id, username, name, description, featured, source_id)
                  VALUES (?, ?, ?, ?, ?, ?)""", (pid, username, name, description, 1 if featured else 0, source_id))
    if videos:
        _append(db, pid, videos, username)
    return pid


@router.get(BASE)
def list_playlists(request: Request):
    """Yours, then everyone else's featured ones, the latest changed first."""
    me = _me(request)
    if not me:
        return _error(401, "not authenticated")
    with get_db() as db:
        rows = db.execute("""SELECT * FROM video_playlists WHERE username=? OR featured=1
                             ORDER BY (username=?) DESC, updated DESC LIMIT 500""", (me, me)).fetchall()
        return {"playlists": _summaries(db, rows, me, request)}


@router.post(BASE)
async def new_playlist(request: Request):
    me = _me(request)
    if not me:
        return _error(401, "not authenticated")
    body = await _body(request)
    if body is None:
        return _error(400, "invalid JSON")
    fields = _fields({"name": body.get("name", ""), **{k: body[k] for k in ("description", "featured") if k in body}})
    if isinstance(fields, JSONResponse):
        return fields
    ids = _ids(body.get("videos", []))
    if ids is None:
        return _error(400, "videos is a list of video ids")
    with get_db() as db:
        if db.execute("SELECT COUNT(*) FROM video_playlists WHERE username=?", (me,)).fetchone()[0] >= PLAYLISTS_PER_USER:
            return _error(409, f"you can have up to {PLAYLISTS_PER_USER} playlists")
        pid = create_playlist_row(db, me, fields["name"], fields.get("description", ""), bool(fields.get("featured")),
                                  _shown(db, ids, me))
        db.commit()
        return _full(db, pid, request, me)


@router.get(f"{BASE}/{{playlist_id}}")
def get_playlist(playlist_id: str, request: Request):
    me = _me(request)
    if not me:
        return _error(401, "not authenticated")
    with get_db() as db:
        if not _visible(_row(db, playlist_id), me):
            return _error(404, "not found")
        return _full(db, playlist_id, request, me)


@router.patch(f"{BASE}/{{playlist_id}}")
async def edit_playlist(playlist_id: str, request: Request):
    """name, description, featured, and videos: the whole order (what's
    left out is taken out)."""
    me = _me(request)
    if not me:
        return _error(401, "not authenticated")
    body = await _body(request)
    if body is None:
        return _error(400, "invalid JSON")
    fields = _fields(body)
    if isinstance(fields, JSONResponse):
        return fields
    with get_db() as db:
        row = _row(db, playlist_id)
        if not _visible(row, me):
            return _error(404, "not found")
        if not _can_edit(row, request, me):
            return _error(403, "not your playlist")
        if fields:
            keys = ", ".join(f"{k}=?" for k in fields)
            db.execute(f"UPDATE video_playlists SET {keys} WHERE id=?", (*fields.values(), playlist_id))
        if "videos" in body:
            ids = _ids(body["videos"])
            if ids is None:
                return _error(400, "videos is a list of video ids")
            # Videos the editor can't see (someone's still processing) stay, at the end.
            keep = _shown(db, ids, me)
            hidden = [r[0] for r in db.execute(
                """SELECT i.video_id FROM video_playlist_items i JOIN videos v ON v.post_id = i.video_id
                   JOIN posts p ON p.id = v.post_id WHERE i.playlist_id=? AND v.status!='ready' AND p.username!=?
                   ORDER BY i.pos""", (playlist_id, me))]
            db.execute("DELETE FROM video_playlist_items WHERE playlist_id=?", (playlist_id,))
            _append(db, playlist_id, keep + [h for h in hidden if h not in keep], row["username"])
        _touch(db, playlist_id)
        db.commit()
        return _full(db, playlist_id, request, me)


@router.delete(f"{BASE}/{{playlist_id}}")
def delete_playlist(playlist_id: str, request: Request):
    from .playlists import _delete_cover_files
    me = _me(request)
    if not me:
        return _error(401, "not authenticated")
    with get_db() as db:
        row = _row(db, playlist_id)
        if not _visible(row, me):
            return _error(404, "not found")
        if not _can_edit(row, request, me):
            return _error(403, "not your playlist")
        db.execute("DELETE FROM video_playlist_items WHERE playlist_id=?", (playlist_id,))
        db.execute("DELETE FROM video_playlists WHERE id=?", (playlist_id,))
        db.commit()
    _delete_cover_files(row["cover"])
    return {"ok": True}


@router.post(f"{BASE}/{{playlist_id}}/videos")
async def add_videos(playlist_id: str, request: Request):
    me = _me(request)
    if not me:
        return _error(401, "not authenticated")
    body = await _body(request)
    ids = _ids((body or {}).get("videos"))
    if not ids:
        return _error(400, "videos is a list of video ids")
    with get_db() as db:
        row = _row(db, playlist_id)
        if not _visible(row, me):
            return _error(404, "not found")
        if not _can_edit(row, request, me):
            return _error(403, "not your playlist")
        added = _append(db, playlist_id, _shown(db, ids, me), me)
        _touch(db, playlist_id)
        db.commit()
        out = _summaries(db, [_row(db, playlist_id)], me, request)[0]
        out["added"] = added
        return out


@router.delete(f"{BASE}/{{playlist_id}}/videos/{{video_id}}")
def remove_video(playlist_id: str, video_id: int, request: Request):
    me = _me(request)
    if not me:
        return _error(401, "not authenticated")
    with get_db() as db:
        row = _row(db, playlist_id)
        if not _visible(row, me):
            return _error(404, "not found")
        if not _can_edit(row, request, me):
            return _error(403, "not your playlist")
        db.execute("DELETE FROM video_playlist_items WHERE playlist_id=? AND video_id=?", (playlist_id, video_id))
        _touch(db, playlist_id)
        db.commit()
        return _summaries(db, [_row(db, playlist_id)], me, request)[0]


@router.post(f"{BASE}/{{playlist_id}}/cover")
def upload_cover(playlist_id: str, request: Request, file: UploadFile = File(...)):
    from .playlists import _cover_slots, _delete_cover_files, _write_cover
    me = _me(request)
    if not me:
        return _error(401, "not authenticated")
    if not media_ready():
        return _error(503, "cover storage is offline")
    with get_db() as db:
        row = _row(db, playlist_id)
        if not _visible(row, me):
            return _error(404, "not found")
        if not _can_edit(row, request, me):
            return _error(403, "not your playlist")
    cap = PLAYLIST_COVER_MAX_MB * 1024 * 1024
    data = file.file.read(cap + 1)
    if len(data) > cap:
        return _error(413, f"covers can be up to {PLAYLIST_COVER_MAX_MB}MB")
    try:
        with _cover_slots:
            cover_id = _write_cover(data)
    except ValueError as e:
        return _error(400, str(e))
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT cover FROM video_playlists WHERE id=?", (playlist_id,)).fetchone()
        if row is None:
            db.rollback()
            _delete_cover_files(cover_id)
            return _error(404, "not found")
        db.execute("UPDATE video_playlists SET cover=?, updated=datetime('now') WHERE id=?", (cover_id, playlist_id))
        db.commit()
        _delete_cover_files(row["cover"])
        return _summaries(db, [_row(db, playlist_id)], me, request)[0]


@router.delete(f"{BASE}/{{playlist_id}}/cover")
def remove_cover(playlist_id: str, request: Request):
    from .playlists import _delete_cover_files
    me = _me(request)
    if not me:
        return _error(401, "not authenticated")
    with get_db() as db:
        row = _row(db, playlist_id)
        if not _visible(row, me):
            return _error(404, "not found")
        if not _can_edit(row, request, me):
            return _error(403, "not your playlist")
        db.execute("UPDATE video_playlists SET cover='', updated=datetime('now') WHERE id=?", (playlist_id,))
        db.commit()
        _delete_cover_files(row["cover"])
        return _summaries(db, [_row(db, playlist_id)], me, request)[0]
