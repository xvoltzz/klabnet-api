"""Stars on songs and albums. Posts and photos are starred with a ⭐
reaction (posts.py) and chat messages with a ⭐ Matrix reaction; songs and
albums live in Navidrome, so their stars are kept here.

Under /api/posts/ so it rides the proxy route the feed already has.
Registered before posts.router, though nothing there would match anyway.
"""

import re
import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_username
from ..db import get_db

router = APIRouter()

KINDS = {"song", "album"}
# Navidrome ids, the same shape posts.py accepts for a shared song.
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# A long album page or a page of the Songs tab, in one request.
MAX_IDS = 300


def _unauthenticated() -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": "not authenticated"})


def _counts(db, kind: str, ids: list[str], username: str) -> dict:
    """{id: {"count", "mine"}} for the ids that have any stars at all."""
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = db.execute(
        f"""SELECT item_id, COUNT(*) AS count, MAX(username = ?) AS mine
            FROM stars WHERE kind = ? AND item_id IN ({placeholders})
            GROUP BY item_id""",
        (username, kind, *ids),
    ).fetchall()
    return {r["item_id"]: {"count": r["count"], "mine": bool(r["mine"])} for r in rows}


@router.get("/api/posts/stars")
def get_stars(request: Request, kind: str = "", ids: str = ""):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    if kind not in KINDS:
        return JSONResponse(status_code=400, content={"error": "unknown kind"})
    wanted = [i for i in dict.fromkeys(ids.split(",")) if _ID_RE.fullmatch(i)][:MAX_IDS]
    with get_db() as db:
        return {"stars": _counts(db, kind, wanted, username)}


@router.put("/api/posts/stars")
async def toggle_star(request: Request):
    """Stars the item for the caller, or takes their star back."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    kind, item_id = str(body.get("kind", "")), str(body.get("id", ""))
    if kind not in KINDS or not _ID_RE.fullmatch(item_id):
        return JSONResponse(status_code=400, content={"error": "kind and id required"})
    with get_db() as db:
        mine = db.execute(
            "SELECT 1 FROM stars WHERE kind=? AND item_id=? AND username=?", (kind, item_id, username)
        ).fetchone()
        if mine:
            db.execute("DELETE FROM stars WHERE kind=? AND item_id=? AND username=?", (kind, item_id, username))
        else:
            try:
                db.execute("INSERT INTO stars(kind, item_id, username) VALUES (?, ?, ?)", (kind, item_id, username))
            except sqlite3.IntegrityError:
                pass  # a racing duplicate: starred either way
        db.commit()
        entry = _counts(db, kind, [item_id], username).get(item_id, {"count": 0, "mine": False})
    return entry
