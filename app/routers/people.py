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
from .listens import LISTEN_MIN
from .photos import _COLS as _PHOTO_COLS, _attach
from .posts import _reactions_and_reply_counts, _row_to_post

STAR = "⭐"
GOLD_AT = 5  # stars that make a post gold (the site's KLAB_GOLD_AT)

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


@router.get("/api/posts/people/{username}")
def person(username: str, request: Request):
    """One person's page: their numbers, latest posts and photos, their gold
    posts (GOLD_AT stars or more) and what they listen to."""
    me = get_username(request)
    if me == "anonymous":
        return JSONResponse(status_code=401, content={"error": "not authenticated"})
    with get_db() as db:
        user = db.execute("SELECT username, first_seen, last_seen FROM users WHERE username = ?", (username,)).fetchone()
        # Someone from before users was kept still has their posts.
        if user is None and db.execute("SELECT 1 FROM posts WHERE username = ? LIMIT 1", (username,)).fetchone():
            user = {"username": username, "first_seen": None, "last_seen": None}
        if user is None or username.endswith("-bot"):
            return JSONResponse(status_code=404, content={"error": "no one by that name"})
        cols = "id, username, text, image_mxc, song_json, created"
        rows = db.execute(
            f"SELECT {cols} FROM posts WHERE kind='' AND username = ? ORDER BY id DESC LIMIT 30", (username,)
        ).fetchall()
        gold_rows = db.execute(
            f"""SELECT {cols}, kind FROM posts WHERE username = ? AND id IN (
                    SELECT post_id FROM post_reactions WHERE emoji = ? GROUP BY post_id HAVING COUNT(*) >= ?)
                ORDER BY id DESC LIMIT 30""",
            (username, STAR, GOLD_AT),
        ).fetchall()
        ids = list({r["id"] for r in rows} | {r["id"] for r in gold_rows})
        reactions, replies = _reactions_and_reply_counts(db, ids, me)
        photo_rows = db.execute(
            f"""SELECT {_PHOTO_COLS} FROM posts WHERE kind='photo' AND username = ?
                AND EXISTS (SELECT 1 FROM photos ph WHERE ph.post_id = posts.id)
                ORDER BY id DESC LIMIT 24""",
            (username,),
        ).fetchall()
        photos = _attach(db, photo_rows, me)

        def n(sql, *args):
            return db.execute(sql, args).fetchone()[0] or 0

        counts = {
            "posts": n("SELECT COUNT(*) FROM posts WHERE kind='' AND username = ?", username),
            "photos": n("SELECT COUNT(*) FROM photos p JOIN posts ps ON ps.id = p.post_id WHERE ps.username = ?", username),
            "replies": n("SELECT COUNT(*) FROM post_replies WHERE username = ?", username),
            "stars": n("SELECT COUNT(*) FROM post_reactions r JOIN posts p ON p.id = r.post_id WHERE p.username = ? AND r.emoji = ?", username, STAR),
            "listened": round(n("SELECT SUM(seconds) FROM listens WHERE username = ?", username)),
            "plays": n("SELECT COUNT(*) FROM listens WHERE username = ? AND seconds >= ?", username, LISTEN_MIN),
        }
        artists = [
            {"name": r["artist"], "seconds": round(r["s"])}
            for r in db.execute(
                """SELECT artist, SUM(seconds) AS s FROM listens WHERE username = ? AND artist != ''
                   GROUP BY artist HAVING s >= ? ORDER BY s DESC LIMIT 6""", (username, LISTEN_MIN))
        ]
        songs = [
            {"songId": r["song_id"], "song": r["song"], "artist": r["artist"], "album": r["album"],
             "coverArt": r["cover_art"], "plays": r["n"]}
            for r in db.execute(
                """SELECT song_id, song, artist, album, cover_art, COUNT(*) AS n FROM listens
                   WHERE username = ? AND seconds >= ? GROUP BY song_id ORDER BY n DESC, MAX(last) DESC LIMIT 6""",
                (username, LISTEN_MIN))
        ]
        recent = [
            {"songId": r["song_id"], "song": r["song"], "artist": r["artist"], "album": r["album"],
             "coverArt": r["cover_art"], "at": r["last"]}
            for r in db.execute(
                """SELECT song_id, song, artist, album, cover_art, MAX(last) AS last FROM listens
                   WHERE username = ? AND seconds >= ? GROUP BY song_id ORDER BY last DESC LIMIT 8""",
                (username, LISTEN_MIN))
        ]
    gold = []
    for r in gold_rows:
        post = _row_to_post(r, reactions, replies)
        post["kind"] = r["kind"] or "post"
        gold.append(post)
    return {
        "username": user["username"],
        "firstSeen": user["first_seen"],
        "lastSeen": user["last_seen"],
        "counts": counts,
        "posts": [_row_to_post(r, reactions, replies) for r in rows],
        "photos": photos,
        "gold": gold,
        "listening": {"artists": artists, "songs": songs, "recent": recent},
    }

