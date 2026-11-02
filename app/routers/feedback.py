"""Bug reports and ideas, kept here.

They used to be filed as Gitea issues by a bot account. Now they're rows in
the database: anyone signed in can post one and see what's been posted
(so the same thing isn't reported twice), and the owner reviews them (with
Claude) and marks them done. An admin can mark one done or reply to it;
whoever posted it, or an admin, can delete it.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_groups, get_username, is_admin
from ..config import FEEDBACK_BODY_MAX_CHARS, FEEDBACK_TITLE_MAX_CHARS
from ..db import get_db

router = APIRouter()

KINDS = ("bug", "idea")
STATES = ("open", "done", "wontfix")


def _unauthenticated() -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": "not authenticated"})


def _shape(row, me: str, admin: bool) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "body": row["body"],
        "username": row["username"],
        "state": row["state"],
        "reply": row["reply"],
        "created": row["created"],
        "updated": row["updated"],
        "mine": row["username"] == me,
        "can_delete": row["username"] == me or admin,
    }


@router.get("/api/feedback")
def list_feedback(request: Request, state: str = ""):
    """Everything posted, open ones first, newest first within that.
    `state=open` (or done, wontfix) narrows it."""
    me = get_username(request)
    if me == "anonymous":
        return _unauthenticated()
    admin = is_admin(get_groups(request))
    with get_db() as db:
        if state in STATES:
            rows = db.execute("SELECT * FROM feedback WHERE state=? ORDER BY id DESC LIMIT 500", (state,)).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM feedback ORDER BY state='open' DESC, id DESC LIMIT 500"
            ).fetchall()
    return {"items": [_shape(r, me, admin) for r in rows], "admin": admin}


@router.post("/api/feedback")
async def create_feedback(request: Request):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid body"})

    kind = str(body.get("type") or body.get("kind") or "bug").strip().lower()
    if kind == "feature":
        kind = "idea"
    if kind not in KINDS:
        kind = "bug"
    title = str(body.get("title") or "").strip()[:FEEDBACK_TITLE_MAX_CHARS]
    detail = str(body.get("body") or "").strip()[:FEEDBACK_BODY_MAX_CHARS]
    if not title:
        return JSONResponse(status_code=400, content={"error": "title required"})
    # What makes a report actionable that the reporter can't be expected
    # to supply: where they were and what they're running.
    page = str(body.get("page") or "").strip()[:200]
    version = str(body.get("version") or "").strip()[:40]
    ua = (request.headers.get("user-agent") or "")[:300]

    with get_db() as db:
        cur = db.execute(
            "INSERT INTO feedback (username, kind, title, body, page, version, ua) VALUES (?,?,?,?,?,?,?)",
            (username, kind, title, detail, page, version, ua),
        )
        db.commit()
        row = db.execute("SELECT * FROM feedback WHERE id=?", (cur.lastrowid,)).fetchone()
    return {"ok": True, "item": _shape(row, username, is_admin(get_groups(request)))}


@router.patch("/api/feedback/{item_id}")
async def update_feedback(item_id: int, request: Request):
    """An admin marks one done (or won't fix, or open again) and may say
    something about it (`reply`)."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    if not is_admin(get_groups(request)):
        return JSONResponse(status_code=403, content={"error": "admins only"})
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid body"})
    sets, args = [], []
    if "state" in body:
        if body["state"] not in STATES:
            return JSONResponse(status_code=400, content={"error": "bad state"})
        sets.append("state=?"); args.append(body["state"])
    if "reply" in body:
        sets.append("reply=?"); args.append(str(body["reply"] or "").strip()[:FEEDBACK_BODY_MAX_CHARS])
    if not sets:
        return JSONResponse(status_code=400, content={"error": "nothing to change"})
    with get_db() as db:
        cur = db.execute(
            f"UPDATE feedback SET {', '.join(sets)}, updated=datetime('now') WHERE id=?", (*args, item_id)
        )
        db.commit()
        if not cur.rowcount:
            return JSONResponse(status_code=404, content={"error": "not found"})
        row = db.execute("SELECT * FROM feedback WHERE id=?", (item_id,)).fetchone()
    return {"ok": True, "item": _shape(row, username, True)}


@router.delete("/api/feedback/{item_id}")
def delete_feedback(item_id: int, request: Request):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    with get_db() as db:
        row = db.execute("SELECT username FROM feedback WHERE id=?", (item_id,)).fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": "not found"})
        if row["username"] != username and not is_admin(get_groups(request)):
            return JSONResponse(status_code=403, content={"error": "not yours"})
        db.execute("DELETE FROM feedback WHERE id=?", (item_id,))
        db.commit()
    return {"ok": True}
