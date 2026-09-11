import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_username
from ..config import BIO_MAX_CHARS
from ..db import get_db

router = APIRouter()

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _unauthenticated() -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": "not authenticated"})


def _row_to_profile(r) -> dict:
    return {"chat_color": r["chat_color"], "bio": r["bio"], "banner_mxc": r["banner_mxc"]}


@router.get("/api/profiles")
def get_profiles():
    """Every saved profile in one call — mirrors GET /api/notes's bulk-fetch
    shape so the frontend can decorate chat/feed/presence without a lookup
    per user. Profile data is meant to be public, unlike /api/prefs."""
    with get_db() as db:
        rows = db.execute("SELECT username, chat_color, bio, banner_mxc FROM profiles").fetchall()
    return {"profiles": {r["username"]: _row_to_profile(r) for r in rows}}


@router.put("/api/profiles/me")
async def update_my_profile(request: Request):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})

    with get_db() as db:
        row = db.execute(
            "SELECT chat_color, bio, banner_mxc FROM profiles WHERE username=?", (username,)
        ).fetchone()
        current = _row_to_profile(row) if row else {"chat_color": "", "bio": "", "banner_mxc": ""}

        if "chat_color" in body:
            chat_color = str(body.get("chat_color") or "").strip()
            # '' means "use the default hash-based color" — anything else must
            # be a strict 6-digit hex, since the frontend interpolates this
            # value directly into a `style="--name-color:...;"` attribute.
            if chat_color and not _HEX_COLOR_RE.match(chat_color):
                return JSONResponse(status_code=400, content={"error": "chat_color must be a hex color like #a1b2c3"})
            current["chat_color"] = chat_color
        if "bio" in body:
            current["bio"] = str(body.get("bio", "")).strip()[:BIO_MAX_CHARS]
        if "banner_mxc" in body:
            current["banner_mxc"] = str(body.get("banner_mxc", "")).strip()

        db.execute(
            """INSERT INTO profiles(username, chat_color, bio, banner_mxc, updated)
               VALUES (?, ?, ?, ?, datetime('now'))
               ON CONFLICT(username) DO UPDATE SET
                   chat_color = excluded.chat_color,
                   bio        = excluded.bio,
                   banner_mxc = excluded.banner_mxc,
                   updated    = excluded.updated""",
            (username, current["chat_color"], current["bio"], current["banner_mxc"]),
        )
        db.commit()
    return {"ok": True, **current}
