from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_groups, get_username, is_admin
from ..config import POST_MAX_CHARS
from ..db import get_db

router = APIRouter()


def _unauthenticated() -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": "not authenticated"})


def _row_to_post(r) -> dict:
    return {
        "id": r["id"],
        "username": r["username"],
        "text": r["text"],
        "image_mxc": r["image_mxc"],
        "created": r["created"],
    }


@router.get("/api/posts")
def get_posts(request: Request, limit: int = 50, before_id: int | None = None):
    """Feed, newest-first. `before_id` pages further back for "load older"."""
    limit = max(1, min(limit, 100))
    with get_db() as db:
        if before_id is not None:
            rows = db.execute(
                "SELECT id, username, text, image_mxc, created FROM posts WHERE id < ? ORDER BY id DESC LIMIT ?",
                (before_id, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, username, text, image_mxc, created FROM posts ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return {"posts": [_row_to_post(r) for r in rows]}


@router.post("/api/posts")
async def create_post(request: Request):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    text = str(body.get("text", "")).strip()[:POST_MAX_CHARS]
    image_mxc = str(body.get("image_mxc", "")).strip()
    if not text and not image_mxc:
        return JSONResponse(status_code=400, content={"error": "post must have text or an image"})
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO posts(username, text, image_mxc) VALUES (?, ?, ?)",
            (username, text, image_mxc),
        )
        db.commit()
        row = db.execute(
            "SELECT id, username, text, image_mxc, created FROM posts WHERE id=?",
            (cur.lastrowid,),
        ).fetchone()
    return _row_to_post(row)


@router.delete("/api/posts/{post_id}")
def delete_post(post_id: int, request: Request):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    with get_db() as db:
        row = db.execute("SELECT username FROM posts WHERE id=?", (post_id,)).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "not found"})
        if row["username"] != username and not is_admin(get_groups(request)):
            return JSONResponse(status_code=403, content={"error": "not your post"})
        db.execute("DELETE FROM posts WHERE id=?", (post_id,))
        db.commit()
    return {"ok": True}
