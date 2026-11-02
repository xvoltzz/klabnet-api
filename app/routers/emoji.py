"""Custom emoji: a picture with a name, used as :name: in posts, chat
messages and reactions, everywhere klabnet shows text.

Anyone signed in can add one; whoever added it, or an admin, can delete
it. They're under /api/posts/emoji so they ride a prefix Caddy already
forwards. The pictures are files on the media share (emoji/<file>), a new
upload a new file, so they're served as immutable.

A still picture is shrunk to fit 128px and kept as a PNG. An animated GIF
or WebP is kept as it came (resizing every frame would lose its timing),
so long as it's small.
"""

import io
import os
import re
import secrets

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image

from ..auth import get_groups, get_username, is_admin
from ..config import CUSTOM_EMOJI_MAX, CUSTOM_EMOJI_MAX_KB, MEDIA_DIR
from ..db import get_db
from ..media import media_ready

router = APIRouter()

BASE = "/api/posts/emoji"
# :name: has to fit a reaction (EMOJI_MAX_CHARS, 32) with its colons.
NAME_RE = re.compile(r"[a-z0-9_]{2,30}")
_FILE_RE = re.compile(r"[A-Za-z0-9_-]{16}\.(png|gif|webp)")
EDGE = 128
_MAX_BYTES = CUSTOM_EMOJI_MAX_KB * 1024
_TYPES = {"png": "image/png", "gif": "image/gif", "webp": "image/webp"}


def _error(status: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": msg})


def _path(file: str) -> str:
    return os.path.join(MEDIA_DIR, "emoji", file)


def _shape(row, me: str, admin: bool) -> dict:
    return {
        "name": row["name"],
        "url": f"{BASE}/files/{row['file']}",
        "by": row["username"],
        "animated": bool(row["animated"]),
        "can_delete": row["username"] == me or admin,
    }


@router.get(BASE)
def list_emoji(request: Request):
    me = get_username(request)
    if me == "anonymous":
        return _error(401, "not authenticated")
    admin = is_admin(get_groups(request))
    with get_db() as db:
        rows = db.execute("SELECT * FROM custom_emoji ORDER BY name").fetchall()
    return {"emoji": [_shape(r, me, admin) for r in rows],
            "limits": {"kb": CUSTOM_EMOJI_MAX_KB, "count": CUSTOM_EMOJI_MAX, "storage": media_ready()}}


def _write(data: bytes) -> tuple[str, bool]:
    """The upload as an emoji's file. Returns (file, animated); raises
    ValueError with a message for the uploader."""
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > 4096 * 4096:
            raise ValueError("that image is too large")
        animated = getattr(img, "n_frames", 1) > 1
        fmt = (img.format or "").lower()
        img.load()
    except ValueError:
        raise
    except Exception:
        raise ValueError("that file isn't an image we can read")
    os.makedirs(os.path.join(MEDIA_DIR, "emoji"), exist_ok=True)
    stem = secrets.token_urlsafe(12)
    if animated:
        if fmt not in ("gif", "webp"):
            raise ValueError("an animated emoji has to be a GIF or WebP")
        if len(data) > 512 * 1024:
            raise ValueError("an animated emoji can be up to 512KB")
        file = f"{stem}.{fmt}"
        tmp = _path(file) + ".partial"
        with open(tmp, "wb") as f:
            f.write(data)
    else:
        img = img.convert("RGBA")
        img.thumbnail((EDGE, EDGE), Image.LANCZOS)
        img.info = {}
        file = f"{stem}.png"
        tmp = _path(file) + ".partial"
        img.save(tmp, "PNG", optimize=True)
    os.replace(tmp, _path(file))
    return file, animated


@router.post(BASE)
def add_emoji(request: Request, name: str = Form(...), file: UploadFile = File(...)):
    me = get_username(request)
    if me == "anonymous":
        return _error(401, "not authenticated")
    name = name.strip().strip(":").lower()
    if not NAME_RE.fullmatch(name):
        return _error(400, "a name is 2 to 30 letters, numbers or underscores")
    if not media_ready():
        return _error(503, "emoji storage is offline")
    with get_db() as db:
        if db.execute("SELECT 1 FROM custom_emoji WHERE name=?", (name,)).fetchone():
            return _error(409, f":{name}: is taken")
        if db.execute("SELECT COUNT(*) FROM custom_emoji").fetchone()[0] >= CUSTOM_EMOJI_MAX:
            return _error(409, f"there are {CUSTOM_EMOJI_MAX} emoji already, the most there can be")
    data = file.file.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        return _error(413, f"emoji can be up to {CUSTOM_EMOJI_MAX_KB}KB")
    try:
        stored, animated = _write(data)
    except ValueError as e:
        return _error(400, str(e))
    with get_db() as db:
        try:
            db.execute("INSERT INTO custom_emoji(name, file, username, animated) VALUES (?, ?, ?, ?)",
                       (name, stored, me, int(animated)))
            db.commit()
        except Exception:
            # Someone took the name while this was uploading.
            _remove(stored)
            return _error(409, f":{name}: is taken")
        row = db.execute("SELECT * FROM custom_emoji WHERE name=?", (name,)).fetchone()
    return _shape(row, me, is_admin(get_groups(request)))


def _remove(file: str) -> None:
    if _FILE_RE.fullmatch(file or ""):
        try:
            os.remove(_path(file))
        except OSError:
            pass


@router.delete(f"{BASE}/{{name}}")
def delete_emoji(name: str, request: Request):
    me = get_username(request)
    if me == "anonymous":
        return _error(401, "not authenticated")
    with get_db() as db:
        row = db.execute("SELECT * FROM custom_emoji WHERE name=?", (name,)).fetchone()
        if row is None:
            return _error(404, "not found")
        if row["username"] != me and not is_admin(get_groups(request)):
            return _error(403, "not yours")
        db.execute("DELETE FROM custom_emoji WHERE name=?", (name,))
        db.commit()
    _remove(row["file"])
    return {"ok": True}


@router.get(f"{BASE}/files/{{file}}")
def emoji_file(file: str):
    if not _FILE_RE.fullmatch(file):
        return _error(404, "not found")
    path = _path(file)
    if not os.path.isfile(path):
        return _error(404, "not found")
    return FileResponse(path, media_type=_TYPES[file.rsplit(".", 1)[1]],
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})
