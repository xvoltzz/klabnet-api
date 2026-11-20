"""Clips: short videos, mostly gameplay ("in videos id like a "Clips"
section… that can be for gameplay clips!!").

A clip is a video (routers/videos.py) with videos.kind = 'clip': the same
pipeline (the ladder keeps 60fps, the Original is kept), comments and
reactions, but kept out of Videos' own lists and given a place of its own.
It's at most CLIP_MAX_SECONDS long and has an optional game, free text
merged by case with the games already used.

  GET  /api/posts/videos/clips          ?game=&user=&q=&sort=new|top&window=week|month|all
  GET  /api/posts/videos/clips/games    the games used, with counts
  POST /api/posts/videos/{id}/clip      {start, end, title?, game?}: a clip
                                        made from a range of a video

Uploading one is Videos' upload with kind=clip (and game). A clip made from
a range keeps where it came from (clip_of, clip_start, clip_end) and starts
with no file: the worker cuts the range out of the video's upload (cut(),
re-encoded near-losslessly so the cut lands on the frame) before making the
ladder as for any upload.

Included into videos.router ahead of its /{post_id} routes, so "clips" is
never taken for a video id. videos.py is imported lazily: it imports this."""
import os
import secrets
import shutil
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..config import CLIP_MAX_SECONDS, VIDEO_DESC_MAX_CHARS, VIDEO_TITLE_MAX_CHARS
from ..db import get_db
from ..media import media_ready

router = APIRouter()
BASE = "/api/posts/videos/clips"
GAME_MAX_CHARS = 60
MIN_SECONDS = 1.0
_WINDOWS = {"week": 7 * 86400, "month": 30 * 86400, "all": 0}


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _me(request: Request) -> str | None:
    from .videos import _signed_in
    return _signed_in(request)


def game_name(db, raw) -> str:
    """A game as it's kept: trimmed, and spelt as the first clip with the
    same name (any case) spelt it, so "elden ring" joins "Elden Ring"."""
    name = " ".join(str(raw or "").split())[:GAME_MAX_CHARS]
    if not name:
        return ""
    r = db.execute("""SELECT game FROM videos WHERE kind='clip' AND game != '' AND lower(game) = lower(?)
                      ORDER BY post_id LIMIT 1""", (name,)).fetchone()
    return r["game"] if r else name


def _like(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@router.get(BASE)
def list_clips(request: Request, game: str = "", user: str = "", q: str = "", sort: str = "new",
               window: str = "week", limit: int = 30, offset: int = 0):
    """Newest first (by when each came out), or the top ones: the most
    watched and reacted to in the window (views and reactions in it; then
    of all time; then newest). Offset pages either way."""
    from .videos import _COLS, _json_many
    username = _me(request)
    if not username:
        return _error(401, "not authenticated")
    limit = max(1, min(limit, 100))
    where, args = ["v.kind='clip'", "v.unlisted=0", "(v.status='ready' OR p.username=?)"], [username]
    if game.strip():
        where.append("lower(v.game) = lower(?)"); args.append(" ".join(game.split()))
    if user.strip():
        where.append("p.username=?"); args.append(user.strip().lower())
    for word in q.split()[:8]:
        like = _like(word)
        where.append("(v.title LIKE ? ESCAPE '\\' OR p.text LIKE ? ESCAPE '\\' OR v.game LIKE ? ESCAPE '\\')"); args += [like, like, like]
    cond = " AND ".join(where)
    with get_db() as db:
        total = db.execute(f"SELECT COUNT(*) FROM videos v JOIN posts p ON p.id = v.post_id WHERE {cond}", args).fetchone()[0]
        if sort == "top":
            span = _WINDOWS.get(window, _WINDOWS["week"])
            since = time.time() - span if span else 0
            since_sql = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(since)) if span else ""
            rows = db.execute(f"""SELECT {_COLS},
                       (SELECT COUNT(*) FROM video_views w WHERE w.post_id = v.post_id AND w.at >= ?)
                     + (SELECT COUNT(*) FROM post_reactions x WHERE x.post_id = v.post_id AND x.created >= ?) AS score,
                       (SELECT COUNT(*) FROM post_reactions x WHERE x.post_id = v.post_id) AS reacts
                   FROM videos v JOIN posts p ON p.id = v.post_id WHERE {cond}
                   ORDER BY score DESC, views + reacts DESC, v.published DESC, v.post_id DESC LIMIT ? OFFSET ?""",
                              (since, since_sql, *args, limit, max(0, offset))).fetchall()
        else:
            rows = db.execute(f"""SELECT {_COLS} FROM videos v JOIN posts p ON p.id = v.post_id WHERE {cond}
                                  ORDER BY v.published DESC, v.post_id DESC LIMIT ? OFFSET ?""",
                              (*args, limit, max(0, offset))).fetchall()
        return {"clips": _json_many(db, rows, request, username), "total": total, "max": CLIP_MAX_SECONDS}


@router.get(f"{BASE}/games")
def list_games(request: Request, user: str = ""):
    """The games clips are of, most clipped first: [{name, count}], and how
    many clips there are in all (and without a game)."""
    username = _me(request)
    if not username:
        return _error(401, "not authenticated")
    where, args = ["v.kind='clip'", "v.unlisted=0", "(v.status='ready' OR p.username=?)"], [username]
    if user.strip():
        where.append("p.username=?"); args.append(user.strip().lower())
    cond = " AND ".join(where)
    with get_db() as db:
        # SQLite: with MIN(), the bare column is the first clip's spelling.
        rows = db.execute(f"""SELECT v.game AS name, MIN(v.post_id) AS first, COUNT(*) AS n
                              FROM videos v JOIN posts p ON p.id = v.post_id WHERE {cond} AND v.game != ''
                              GROUP BY lower(v.game) ORDER BY n DESC, lower(v.game)""", args).fetchall()
        total = db.execute(f"SELECT COUNT(*) FROM videos v JOIN posts p ON p.id = v.post_id WHERE {cond}", args).fetchone()[0]
    return {"games": [{"name": r["name"], "count": r["n"]} for r in rows], "total": total,
            "none": total - sum(r["n"] for r in rows), "max": CLIP_MAX_SECONDS}


# ── Make a clip from a range of a video ──

@router.post("/api/posts/videos/{post_id}/clip")
async def make_clip(post_id: int, request: Request):
    """{start, end, title?, game?, description?}: a new clip of yours, the
    range [start, end) of that video (anyone's that you can watch), made
    from its upload through the ladder. It links back to the video."""
    from . import videos as V
    username = _me(request)
    if not username:
        return _error(401, "not authenticated")
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
        start, end = float(body.get("start")), float(body.get("end"))
    except Exception:
        return _error(400, "start and end (seconds) required")
    with get_db() as db:
        src = db.execute("SELECT v.*, p.username FROM videos v JOIN posts p ON p.id = v.post_id WHERE v.post_id=?", (post_id,)).fetchone()
    if src is None or (src["unlisted"] and src["username"] != username):
        return _error(404, "not found")
    if src["status"] != "ready":
        return _error(409, "the video's still being made")
    length = src["duration"] or 0
    if not (start >= 0 and end > start):
        return _error(400, "the clip has to end after it starts")
    end = min(end, length) if length else end
    if end - start < MIN_SECONDS:
        return _error(400, "a clip has to be at least a second long")
    if end - start > CLIP_MAX_SECONDS + 0.05:
        return _error(400, f"a clip can be up to {V._clock(CLIP_MAX_SECONDS)} long")
    if not os.path.isfile(os.path.join(V._folder(src["file_id"]), f"source.{src['ext']}")):
        return _error(409, "that video's upload isn't kept, so it can't be clipped")
    if not media_ready():
        return _error(503, "storage is offline")
    if not shutil.which("ffmpeg"):
        return _error(503, "this server can't make videos yet (no ffmpeg)")
    description = str(body.get("description") or "").strip()
    if len(description) > VIDEO_DESC_MAX_CHARS:
        return _error(400, f"a description can be up to {VIDEO_DESC_MAX_CHARS:,} characters")
    title = " ".join(str(body.get("title") or "").split())[:VIDEO_TITLE_MAX_CHARS] or (src["title"] or "Untitled")[:VIDEO_TITLE_MAX_CHARS]
    file_id = secrets.token_urlsafe(12)
    os.makedirs(V._folder(file_id), exist_ok=True)
    with get_db() as db:
        game = game_name(db, body.get("game") if "game" in body else src["game"])
        cur = db.execute("INSERT INTO posts(username, text, kind) VALUES (?, ?, 'video')", (username, description))
        clip_id = cur.lastrowid
        db.execute("""INSERT INTO videos(post_id, title, file_id, ext, duration, width, height, kind, game,
                                         clip_of, clip_start, clip_end, published)
                      VALUES (?, ?, ?, 'mkv', ?, ?, ?, 'clip', ?, ?, ?, ?, datetime('now'))""",
                   (clip_id, title, file_id, round(end - start, 3), src["width"], src["height"], game,
                    post_id, round(start, 3), round(end, 3)))
        db.commit()
    V._enqueue(clip_id)
    with get_db() as db:
        return V._one(db, clip_id, request, username)


def cut(row, report=None) -> None:
    """The worker's first step for a clip made from a range: the range of
    the video it's of, cut from that video's upload into this clip's folder
    as source.mkv. Re-encoded (x264 CRF 12, the source's frame rate and
    picture) so it starts on the very frame asked for; the ladder and the
    Original are then made from it. Raises when the video's gone."""
    from . import videos as V
    with get_db() as db:
        src = db.execute("SELECT file_id, ext FROM videos WHERE post_id=?", (row["clip_of"],)).fetchone()
    if src is None:
        raise RuntimeError("the video it was clipped from is gone")
    path = os.path.join(V._folder(src["file_id"]), f"source.{src['ext']}")
    if not os.path.isfile(path):
        raise RuntimeError("the video it was clipped from has no upload kept")
    info = V.probe(path)
    if info is None:
        raise RuntimeError("couldn't read the video it was clipped from")
    start, length = float(row["clip_start"]), float(row["clip_end"]) - float(row["clip_start"])
    out = os.path.join(V._folder(row["file_id"]), f"source.{row['ext']}")
    tmp = out + ".partial"
    cmd = ["ffmpeg", "-hide_banner", "-y", "-ss", f"{start:.3f}", "-i", path, "-t", f"{length:.3f}",
           "-map", "0:v:0", "-c:v", "libx264", "-preset", "fast", "-crf", "12"]
    if info["audio"]:
        cmd[cmd.index("-c:v"):cmd.index("-c:v")] = ["-map", "0:a:0"]
        cmd += ["-c:a", "aac", "-b:a", "320k"]
    cmd += ["-map_metadata", "-1", "-f", "matroska", tmp]
    try:
        V._run(cmd, length, 0.0, 1.0, report)
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
