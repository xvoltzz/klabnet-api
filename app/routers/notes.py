from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_groups, get_username, is_admin
from ..config import NOTE_MAX_CHARS
from ..db import get_db

router = APIRouter()


def _unauthenticated() -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": "not authenticated"})


@router.get("/api/notes")
def get_notes(request: Request):
    """Everyone's note, for every user — bulk fetch so the frontend can
    decorate presence cards without a request per person. Notes no longer
    expire (the owner: "show what the last note a person had was"): each
    is someone's latest until they write another."""
    with get_db() as db:
        rows = db.execute("SELECT username, text, created FROM notes").fetchall()
    return {"notes": [{"username": r["username"], "text": r["text"], "created": r["created"]} for r in rows]}


@router.put("/api/notes")
async def set_note(request: Request):
    """Sets (replacing any existing) the caller's own note. Empty/whitespace
    text clears it — same as DELETE, just via the one call a "post a note"
    composer already needs to make anyway."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    text = str(body.get("text", "")).strip()[:NOTE_MAX_CHARS]
    if not text:
        # Clearing keeps the last one showing (the owner): a new note
        # replaces it, nothing removes it.
        with get_db() as db:
            row = db.execute("SELECT text FROM notes WHERE username=?", (username,)).fetchone()
        return {"ok": True, "text": row["text"] if row else ""}
    with get_db() as db:
        db.execute(
            """INSERT INTO notes(username, text, created) VALUES (?, ?, datetime('now'))
               ON CONFLICT(username) DO UPDATE SET text = excluded.text, created = excluded.created""",
            (username, text),
        )
        db.commit()
    return {"ok": True, "text": text}


@router.delete("/api/notes")
def clear_note(request: Request, user: str = ""):
    """Your own: kept for older pages; a note stays until replaced (see
    set_note). With ?user=, an admin taking someone's note down (the owner,
    2026-11-21: admins can delete or modify anything); that one goes. A
    query, not a path, so it rides the /api/notes route Caddy forwards."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    target = user.strip().lower()
    if not target or target == username:
        return {"ok": True}
    if not is_admin(get_groups(request)):
        return JSONResponse(status_code=403, content={"error": "admin access required"})
    with get_db() as db:
        db.execute("DELETE FROM notes WHERE username=?", (target,))
        db.commit()
    return {"ok": True, "text": ""}

