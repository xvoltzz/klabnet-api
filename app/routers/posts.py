import json
import re
import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_groups, get_username, is_admin
from ..config import EMOJI_MAX_CHARS, FEED_POST_MAX_CHARS, POST_MAX_CHARS, SONG_FIELD_MAX_CHARS, SONG_LYRIC_MAX_CHARS
from ..db import get_db
from ..photos_store import delete_photo_files

router = APIRouter()

# A Matrix media id, and nothing else: every viewer's browser loads it.
_MXC_RE = re.compile(r"mxc://[A-Za-z0-9.:-]{1,255}/[A-Za-z0-9_-]{1,255}")


def _unauthenticated() -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": "not authenticated"})


# Subsonic ids end up in stream/cover URLs on every viewer's client, so only
# plain id-shaped strings get stored — anything else could smuggle extra
# query params or paths into those URLs.
_SUBSONIC_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _clean_id(value) -> str | None:
    """'' for a missing id, the id if it's well-formed, None if it's junk."""
    s = str(value if value is not None else "").strip()
    if not s:
        return ""
    return s if _SUBSONIC_ID_RE.fullmatch(s) else None


def _sanitize_song(raw) -> str:
    """Client-provided "share to feed" song attachment -> a JSON string to
    store, or '' if there's nothing usable. Not verified against the actual
    music library (same trust model as image_mxc) — this only caps field
    lengths and drops anything without at least a title, the one field the
    feed card can't render without. A malformed songId drops the whole
    song; a malformed coverArt just drops that field. albumId/artistId
    aren't stored at all."""
    if not isinstance(raw, dict):
        return ""
    title = str(raw.get("title", "")).strip()[:SONG_FIELD_MAX_CHARS]
    if not title:
        return ""
    song_id = _clean_id(raw.get("songId"))
    if song_id is None:
        return ""
    song = {
        "songId": song_id,
        "title": title,
        "artist": str(raw.get("artist", "")).strip()[:SONG_FIELD_MAX_CHARS],
        "album": str(raw.get("album", "")).strip()[:SONG_FIELD_MAX_CHARS],
        "coverArt": _clean_id(raw.get("coverArt")) or "",
        "lyric": str(raw.get("lyric") or "").strip()[:SONG_LYRIC_MAX_CHARS],
    }
    # Photo posts play a clip starting here (seconds into the track).
    start = raw.get("start")
    if isinstance(start, (int, float)) and 0 < start < 3600:
        song["start"] = round(float(start), 1)
    return json.dumps(song)


def _reactions_and_reply_counts(db, post_ids: list[int], username: str) -> tuple[dict, dict]:
    """One query each for reaction aggregates and reply counts across a page
    of posts, instead of N+1 per-post lookups."""
    if not post_ids:
        return {}, {}
    placeholders = ",".join("?" for _ in post_ids)
    reactions: dict[int, dict[str, dict]] = {}
    for row in db.execute(
        f"""SELECT post_id, emoji, COUNT(*) AS cnt,
                   SUM(CASE WHEN username = ? THEN 1 ELSE 0 END) AS mine
            FROM post_reactions WHERE post_id IN ({placeholders})
            GROUP BY post_id, emoji""",
        (username, *post_ids),
    ).fetchall():
        reactions.setdefault(row["post_id"], {})[row["emoji"]] = {
            "count": row["cnt"],
            "mine": bool(row["mine"]),
        }
    reply_counts: dict[int, int] = {}
    for row in db.execute(
        f"""SELECT post_id, COUNT(*) AS cnt FROM post_replies
            WHERE post_id IN ({placeholders}) GROUP BY post_id""",
        post_ids,
    ).fetchall():
        reply_counts[row["post_id"]] = row["cnt"]
    return reactions, reply_counts


def _sanitize_video(raw, db) -> str | JSONResponse:
    """A shared video ({id, start?}) -> the JSON to store, '' for none. The
    video must exist; start is where the card plays from."""
    if raw in (None, "", {}):
        return ""
    if not isinstance(raw, dict):
        return JSONResponse(status_code=400, content={"error": "that isn't a video"})
    try:
        vid = int(raw.get("id"))
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "that isn't a video"})
    if db.execute("SELECT 1 FROM videos WHERE post_id=?", (vid,)).fetchone() is None:
        return JSONResponse(status_code=404, content={"error": "no such video"})
    video = {"id": vid}
    start = raw.get("start")
    if isinstance(start, (int, float)) and 0 < start < 86400:
        video["start"] = round(float(start), 1)
    return json.dumps(video)


def _attach_videos(db, posts: list[dict]) -> list[dict]:
    """Each post's shared video, as its card shows it (None once deleted)."""
    from .videos import card
    for p in posts:
        raw = p.pop("_video", "")
        if not raw:
            continue
        try:
            v = json.loads(raw)
        except ValueError:
            continue
        c = card(db, int(v.get("id", 0)))
        p["video"] = {**c, "start": v.get("start")} if c else None
    return posts


def _row_to_post(r, reactions: dict, reply_counts: dict) -> dict:
    song = None
    if r["song_json"]:
        try:
            song = json.loads(r["song_json"])
        except (ValueError, TypeError):
            song = None
    return {
        "id": r["id"],
        "username": r["username"],
        "text": r["text"],
        "image_mxc": r["image_mxc"],
        "song": song,
        "created": r["created"],
        "edited": (r["edited"] if "edited" in r.keys() else "") or None,
        "reactions": reactions.get(r["id"], {}),
        "reply_count": reply_counts.get(r["id"], 0),
        "video": None,
        # Filled in by _attach_videos, which needs the database.
        "_video": r["video_json"] if "video_json" in r.keys() else "",
    }


@router.get("/api/posts")
def get_posts(request: Request, limit: int = 50, before_id: int | None = None):
    """Feed, newest-first. `before_id` pages further back for "load older"."""
    limit = max(1, min(limit, 100))
    username = get_username(request)
    with get_db() as db:
        if before_id is not None:
            rows = db.execute(
                "SELECT id, username, text, image_mxc, song_json, video_json, created, edited FROM posts WHERE kind='' AND id < ? ORDER BY id DESC LIMIT ?",
                (before_id, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, username, text, image_mxc, song_json, video_json, created, edited FROM posts WHERE kind='' ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        reactions, reply_counts = _reactions_and_reply_counts(db, [r["id"] for r in rows], username)
        return {"posts": _attach_videos(db, [_row_to_post(r, reactions, reply_counts) for r in rows])}


@router.post("/api/posts")
async def create_post(request: Request):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    text = str(body.get("text", "")).strip()
    # Refused rather than cut: a clipped markdown post can end mid-table or
    # mid-code-block, and the writer would never know what got dropped.
    if len(text) > FEED_POST_MAX_CHARS:
        return JSONResponse(status_code=400, content={"error": f"post is too long (max {FEED_POST_MAX_CHARS:,} characters)"})
    image_mxc = str(body.get("image_mxc", "")).strip()
    if image_mxc and not _MXC_RE.fullmatch(image_mxc):
        return JSONResponse(status_code=400, content={"error": "that image isn't a Matrix upload"})
    song_json = _sanitize_song(body.get("song"))
    with get_db() as db:
        video_json = _sanitize_video(body.get("video"), db)
        if isinstance(video_json, JSONResponse):
            return video_json
        if not text and not image_mxc and not song_json and not video_json:
            return JSONResponse(status_code=400, content={"error": "post must have text, an image, a song or a video"})
        cur = db.execute(
            "INSERT INTO posts(username, text, image_mxc, song_json, video_json) VALUES (?, ?, ?, ?, ?)",
            (username, text, image_mxc, song_json, video_json),
        )
        db.commit()
        row = db.execute(
            "SELECT id, username, text, image_mxc, song_json, video_json, created, edited FROM posts WHERE id=?",
            (cur.lastrowid,),
        ).fetchone()
        return _attach_videos(db, [_row_to_post(row, {}, {})])[0]


@router.patch("/api/posts/{post_id}")
async def edit_post(post_id: int, request: Request):
    """Change a feed post's text: its writer, or an admin (the owner,
    2026-11-21: admins can delete or modify anything). The song, picture
    and video stay."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    text = str(body.get("text", "")).strip()
    if len(text) > FEED_POST_MAX_CHARS:
        return JSONResponse(status_code=400, content={"error": f"post is too long (max {FEED_POST_MAX_CHARS:,} characters)"})
    with get_db() as db:
        row = db.execute(
            "SELECT username, kind, image_mxc, song_json, video_json FROM posts WHERE id=?", (post_id,)
        ).fetchone()
        if row is None or row["kind"] != "":
            return JSONResponse(status_code=404, content={"error": "not found"})
        if row["username"] != username and not is_admin(get_groups(request)):
            return JSONResponse(status_code=403, content={"error": "not your post"})
        if not text and not row["image_mxc"] and not row["song_json"] and not row["video_json"]:
            return JSONResponse(status_code=400, content={"error": "a post with nothing else in it needs some text"})
        db.execute("UPDATE posts SET text=?, edited=datetime('now') WHERE id=?", (text, post_id))
        db.commit()
        out = db.execute(
            "SELECT id, username, text, image_mxc, song_json, video_json, created, edited FROM posts WHERE id=?",
            (post_id,),
        ).fetchone()
        reactions, reply_counts = _reactions_and_reply_counts(db, [post_id], username)
        return _attach_videos(db, [_row_to_post(out, reactions, reply_counts)])[0]


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
        # No FK cascade in this DB (see db.py) — clean up manually.
        photo_ids = [r["id"] for r in db.execute("SELECT id FROM photos WHERE post_id=?", (post_id,)).fetchall()]
        from .videos import delete_files_for
        video_file = delete_files_for(post_id, db)
        db.execute("DELETE FROM photos WHERE post_id=?", (post_id,))
        db.execute("DELETE FROM post_tags WHERE post_id=?", (post_id,))
        db.execute("DELETE FROM post_reactions WHERE post_id=?", (post_id,))
        db.execute("DELETE FROM post_replies WHERE post_id=?", (post_id,))
        db.execute("DELETE FROM posts WHERE id=?", (post_id,))
        db.commit()
    delete_photo_files(photo_ids)
    if video_file:
        from .videos import _remove_folder
        _remove_folder(video_file)
    return {"ok": True}


@router.put("/api/posts/{post_id}/reactions")
async def toggle_reaction(post_id: int, request: Request):
    """Adds the caller's reaction, or removes it if they'd already reacted
    with that same emoji — one call for a click that toggles a pill. Unlike
    /api/notes's PUT (a plain upsert-or-clear), this one actually flips
    state based on what's already there, so a racing duplicate insert is
    treated as a no-op rather than an error."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    emoji = str(body.get("emoji", "")).strip()[:EMOJI_MAX_CHARS]
    if not emoji:
        return JSONResponse(status_code=400, content={"error": "emoji required"})
    with get_db() as db:
        if db.execute("SELECT 1 FROM posts WHERE id=?", (post_id,)).fetchone() is None:
            return JSONResponse(status_code=404, content={"error": "not found"})
        existing = db.execute(
            "SELECT 1 FROM post_reactions WHERE post_id=? AND username=? AND emoji=?",
            (post_id, username, emoji),
        ).fetchone()
        # Any string counts as an emoji: a script could pile thousands of
        # "reactions" on a post, and every feed load returns them all.
        if not existing and db.execute(
            "SELECT COUNT(*) FROM post_reactions WHERE post_id=? AND username=?", (post_id, username)
        ).fetchone()[0] >= 12:
            return JSONResponse(status_code=400, content={"error": "that's a lot of reactions"})
        if existing:
            db.execute(
                "DELETE FROM post_reactions WHERE post_id=? AND username=? AND emoji=?",
                (post_id, username, emoji),
            )
        else:
            try:
                db.execute(
                    "INSERT INTO post_reactions(post_id, username, emoji) VALUES (?, ?, ?)",
                    (post_id, username, emoji),
                )
            except sqlite3.IntegrityError:
                # Lost a race with another request for the same toggle — the
                # reaction already exists either way, so this is a no-op.
                pass
        db.commit()
        reactions, _ = _reactions_and_reply_counts(db, [post_id], username)
    return {"reactions": reactions.get(post_id, {})}


@router.get("/api/posts/{post_id}/replies")
def get_replies(post_id: int, request: Request):
    with get_db() as db:
        if db.execute("SELECT 1 FROM posts WHERE id=?", (post_id,)).fetchone() is None:
            return JSONResponse(status_code=404, content={"error": "not found"})
        rows = db.execute(
            "SELECT id, post_id, username, text, created FROM post_replies WHERE post_id=? ORDER BY id ASC",
            (post_id,),
        ).fetchall()
    return {"replies": [dict(r) for r in rows]}


@router.post("/api/posts/{post_id}/replies")
async def create_reply(post_id: int, request: Request):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    text = str(body.get("text", "")).strip()[:POST_MAX_CHARS]
    if not text:
        return JSONResponse(status_code=400, content={"error": "reply text required"})
    with get_db() as db:
        if db.execute("SELECT 1 FROM posts WHERE id=?", (post_id,)).fetchone() is None:
            return JSONResponse(status_code=404, content={"error": "not found"})
        cur = db.execute(
            "INSERT INTO post_replies(post_id, username, text) VALUES (?, ?, ?)",
            (post_id, username, text),
        )
        db.commit()
        row = db.execute(
            "SELECT id, post_id, username, text, created FROM post_replies WHERE id=?",
            (cur.lastrowid,),
        ).fetchone()
    return dict(row)


@router.delete("/api/posts/{post_id}/replies/{reply_id}")
def delete_reply(post_id: int, reply_id: int, request: Request):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    with get_db() as db:
        row = db.execute(
            "SELECT username FROM post_replies WHERE id=? AND post_id=?", (reply_id, post_id)
        ).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "not found"})
        if row["username"] != username and not is_admin(get_groups(request)):
            return JSONResponse(status_code=403, content={"error": "not your reply"})
        db.execute("DELETE FROM post_replies WHERE id=?", (reply_id,))
        db.commit()
    return {"ok": True}
