"""Photos tab. A photo post is an ordinary `posts` row with kind='photo'
(the caption is its text), so the feed's reaction and reply endpoints work
on it unchanged. Its images live in `photos`, and the files on the media
share (see photos_store.py).

Everything sits under /api/posts/photos because Caddy only forwards an
explicit list of /api prefixes, and /api/posts* is already on it. This
router must be included before posts.router so these paths win over
/api/posts/{post_id}.

Flow: the composer uploads each file as soon as it's picked
(POST /uploads → id + EXIF to pre-fill), then creates the post from those
ids. Uploads never attached to a post are swept after a day.
"""

import json
import os

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ..auth import get_username
from ..config import PHOTO_MAX_UPLOAD_MB, PHOTOS_PER_POST, POST_MAX_CHARS
from ..db import get_db
from ..media import media_ready
from ..photos_store import (
    SIZES,
    PhotoError,
    delete_photo_files,
    photo_path,
    process_upload,
    sanitize_exif,
    valid_photo_id,
)
from .posts import _reactions_and_reply_counts, _row_to_post, _sanitize_song, _unauthenticated

router = APIRouter()

_MAX_BYTES = PHOTO_MAX_UPLOAD_MB * 1024 * 1024


def _sweep_abandoned_uploads(db) -> None:
    rows = db.execute(
        "SELECT id FROM photos WHERE post_id IS NULL AND created < datetime('now', '-1 day')"
    ).fetchall()
    if rows:
        ids = [r["id"] for r in rows]
        delete_photo_files(ids)
        db.executemany("DELETE FROM photos WHERE id=?", [(i,) for i in ids])
        db.commit()


def _photo_json(r) -> dict:
    try:
        exif = json.loads(r["exif_json"])
    except (ValueError, TypeError):
        exif = {}
    return {"id": r["id"], "w": r["width"], "h": r["height"], "exif": exif}


@router.post("/api/posts/photos/uploads")
def upload_photo(request: Request, file: UploadFile = File(...)):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    if not media_ready():
        return JSONResponse(status_code=503, content={"error": "photo storage is offline"})
    data = file.file.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        return JSONResponse(status_code=413, content={"error": f"photos can be up to {PHOTO_MAX_UPLOAD_MB}MB"})
    try:
        photo = process_upload(data)
    except PhotoError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    with get_db() as db:
        db.execute(
            "INSERT INTO photos(id, username, width, height, exif_json) VALUES (?, ?, ?, ?, ?)",
            (photo["id"], username, photo["width"], photo["height"], json.dumps(photo["exif"])),
        )
        db.commit()
        _sweep_abandoned_uploads(db)
    return {"id": photo["id"], "w": photo["width"], "h": photo["height"], "exif": photo["exif"]}


@router.delete("/api/posts/photos/uploads/{photo_id}")
def discard_upload(photo_id: str, request: Request):
    """Removing a picked photo from the composer before posting."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    with get_db() as db:
        row = db.execute(
            "SELECT 1 FROM photos WHERE id=? AND username=? AND post_id IS NULL", (photo_id, username)
        ).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "not found"})
        db.execute("DELETE FROM photos WHERE id=?", (photo_id,))
        db.commit()
    delete_photo_files([photo_id])
    return {"ok": True}


@router.get("/api/posts/photos/files/{photo_id}/{size}")
def get_photo_file(photo_id: str, size: str):
    if size not in SIZES or not valid_photo_id(photo_id):
        return JSONResponse(status_code=404, content={"error": "not found"})
    path = photo_path(photo_id, size)
    if not os.path.isfile(path):
        return JSONResponse(status_code=404, content={"error": "not found"})
    # A photo id's files never change, so the browser can keep them forever.
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.get("/api/posts/photos")
def list_photo_posts(request: Request, limit: int = 40, before_id: int | None = None):
    """Photo posts, newest first, each with its photos in order."""
    limit = max(1, min(limit, 100))
    username = get_username(request)
    cols = "id, username, text, image_mxc, song_json, created"
    with get_db() as db:
        if before_id is not None:
            rows = db.execute(
                f"SELECT {cols} FROM posts WHERE kind='photo' AND id < ? ORDER BY id DESC LIMIT ?",
                (before_id, limit),
            ).fetchall()
        else:
            rows = db.execute(
                f"SELECT {cols} FROM posts WHERE kind='photo' ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        ids = [r["id"] for r in rows]
        reactions, reply_counts = _reactions_and_reply_counts(db, ids, username)
        photos: dict[int, list] = {}
        if ids:
            marks = ",".join("?" for _ in ids)
            for p in db.execute(
                f"SELECT * FROM photos WHERE post_id IN ({marks}) ORDER BY post_id, position", ids
            ).fetchall():
                photos.setdefault(p["post_id"], []).append(_photo_json(p))
    posts = []
    for r in rows:
        post = _row_to_post(r, reactions, reply_counts)
        post["photos"] = photos.get(r["id"], [])
        if post["photos"]:
            posts.append(post)
    return {"posts": posts}


@router.post("/api/posts/photos")
async def create_photo_post(request: Request):
    """Body: {caption, photos: [{id, exif?}], song?}. `exif` is the
    composer's edited version; when present it replaces what was read from
    the file, so a film scan can say what film it was."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    items = body.get("photos")
    if not isinstance(items, list) or not items:
        return JSONResponse(status_code=400, content={"error": "a photo post needs at least one photo"})
    if len(items) > PHOTOS_PER_POST:
        return JSONResponse(status_code=400, content={"error": f"up to {PHOTOS_PER_POST} photos per post"})
    photo_ids = [str(p.get("id", "")) if isinstance(p, dict) else "" for p in items]
    if len(set(photo_ids)) != len(photo_ids):
        return JSONResponse(status_code=400, content={"error": "the same photo is in there twice"})
    caption = str(body.get("caption", "")).strip()[:POST_MAX_CHARS]
    song_json = _sanitize_song(body.get("song"))

    with get_db() as db:
        marks = ",".join("?" for _ in photo_ids)
        owned = db.execute(
            f"SELECT COUNT(*) FROM photos WHERE id IN ({marks}) AND username=? AND post_id IS NULL",
            (*photo_ids, username),
        ).fetchone()[0]
        if owned != len(photo_ids):
            return JSONResponse(status_code=400, content={"error": "some of those photos aren't available"})
        cur = db.execute(
            "INSERT INTO posts(username, text, song_json, kind) VALUES (?, ?, ?, 'photo')",
            (username, caption, song_json),
        )
        post_id = cur.lastrowid
        for position, (photo_id, item) in enumerate(zip(photo_ids, items)):
            if isinstance(item.get("exif"), dict):
                db.execute(
                    "UPDATE photos SET post_id=?, position=?, exif_json=? WHERE id=?",
                    (post_id, position, json.dumps(sanitize_exif(item["exif"])), photo_id),
                )
            else:
                db.execute(
                    "UPDATE photos SET post_id=?, position=? WHERE id=?", (post_id, position, photo_id)
                )
        db.commit()
        row = db.execute(
            "SELECT id, username, text, image_mxc, song_json, created FROM posts WHERE id=?", (post_id,)
        ).fetchone()
        photos = [
            _photo_json(p)
            for p in db.execute("SELECT * FROM photos WHERE post_id=? ORDER BY position", (post_id,)).fetchall()
        ]
    post = _row_to_post(row, {}, {})
    post["photos"] = photos
    return post
