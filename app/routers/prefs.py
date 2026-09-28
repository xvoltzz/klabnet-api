import asyncio
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
    # Everyone without a username would share one "anonymous" row.
    if username == "anonymous":
        return JSONResponse(status_code=401, content={"error": "not authenticated"})
    raw = await request.body()
    # Served back on every page load: a few hundred KB is a lot of playlists.
    if len(raw) > 2_000_000:
        return JSONResponse(status_code=413, content={"error": "prefs too large"})
    try:
        body = json.loads(raw)
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


@router.patch("/api/prefs")
async def patch_prefs(request: Request):
    """Just the fields that changed, merged into what's stored, in one
    transaction; answers with the merged whole. One request, so it can be
    sent as a tab closes (a GET-then-PUT gets cut off half-way), and two
    devices saving at once no longer overwrite each other's fields."""
    username = get_username(request)
    if username == "anonymous":
        return JSONResponse(status_code=401, content={"error": "not authenticated"})
    raw = await request.body()
    if len(raw) > 2_000_000:
        return JSONResponse(status_code=413, content={"error": "prefs too large"})
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    changes = {k: v for k, v in body.items() if k in ALLOWED_PREF_KEYS}
    return await asyncio.to_thread(_merge_prefs, username, changes)


def _merge_prefs(username: str, changes: dict):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT data FROM prefs WHERE username=?", (username,)).fetchone()
        try:
            merged = json.loads(row["data"]) if row else {}
        except ValueError:
            merged = {}
        merged.update(changes)
        data = json.dumps(merged)
        if len(data) > 2_000_000:
            db.rollback()
            return JSONResponse(status_code=413, content={"error": "prefs too large"})
        db.execute(
            """INSERT INTO prefs(username,data,updated) VALUES(?,?,datetime('now'))
               ON CONFLICT(username) DO UPDATE SET data=excluded.data,updated=excluded.updated""",
            (username, data),
        )
        db.commit()
    return merged
