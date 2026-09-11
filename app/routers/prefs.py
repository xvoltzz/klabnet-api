import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_username
from ..db import get_db

router = APIRouter()

# Whitelisted so an unrecognized/renamed frontend key never silently
# accumulates in the stored blob forever.
ALLOWED_PREF_KEYS = {
    "tile_order", "hidden_tiles", "section_order", "notes", "login_song",
    "favorites", "play_history", "playlists", "settings", "theme", "perf",
    "volume", "tile_groups", "bg_dark", "bg_light",
}


@router.get("/api/prefs")
def get_prefs(request: Request):
    username = get_username(request)
    with get_db() as db:
        row = db.execute("SELECT data FROM prefs WHERE username=?", (username,)).fetchone()
    return json.loads(row["data"]) if row else {}


@router.put("/api/prefs")
async def put_prefs(request: Request):
    username = get_username(request)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    clean = {k: v for k, v in body.items() if k in ALLOWED_PREF_KEYS}
    with get_db() as db:
        db.execute(
            """INSERT INTO prefs(username,data,updated) VALUES(?,?,datetime('now'))
               ON CONFLICT(username) DO UPDATE SET data=excluded.data,updated=excluded.updated""",
            (username, json.dumps(clean)),
        )
        db.commit()
    return {"ok": True, "username": username}
