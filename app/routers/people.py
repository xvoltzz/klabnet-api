"""Everyone klabnet knows, with how much they've been around: for Home's
bubbles, which are sized by it. Posts and replies written, and time
listened (from listens, routers/listens.py). Under /api/posts/ because
Caddy only forwards a fixed list of /api prefixes (as /api/posts/changes).
Cached for a minute: every open Home asks."""
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_username
from ..db import get_db

router = APIRouter()

_TTL = 60
_cache: dict = {"at": 0.0, "body": None}


@router.get("/api/posts/people")
def people(request: Request):
    if get_username(request) == "anonymous":
        return JSONResponse(status_code=401, content={"error": "not authenticated"})
    now = time.time()
    if _cache["body"] is None or now - _cache["at"] > _TTL:
        with get_db() as db:
            users = db.execute("SELECT username, first_seen, last_seen FROM users").fetchall()
            posts = {r["username"]: r["n"] for r in db.execute("SELECT username, COUNT(*) AS n FROM posts GROUP BY username")}
            replies = {r["username"]: r["n"] for r in db.execute("SELECT username, COUNT(*) AS n FROM post_replies GROUP BY username")}
            listened = {r["username"]: r["s"] for r in db.execute("SELECT username, SUM(seconds) AS s FROM listens GROUP BY username")}
        _cache["body"] = {"people": [
            {
                "username": u["username"],
                "firstSeen": u["first_seen"],
                "lastSeen": u["last_seen"],
                "posts": posts.get(u["username"], 0),
                "replies": replies.get(u["username"], 0),
                "listened": round(listened.get(u["username"]) or 0),
            }
            for u in users
            # Service accounts aren't people.
            if not u["username"].endswith("-bot")
        ]}
        _cache["at"] = now
    return _cache["body"]
