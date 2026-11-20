"""Videos: klabnet's own YouTube.

A video is a `posts` row with kind='video' whose text is its description,
so reactions (PUT /api/posts/{id}/reactions) and comments (/api/posts/{id}/
replies) are the posts' own, and @mentions in a description count like the
Feed's. The `videos` row (db.py) is the rest, keyed by that post id.

An upload is kept as it came (source.<ext>) and queued for ffmpeg, one at a
time on one worker thread: an HLS ladder of fMP4 rungs (1080p, 720p, 480p,
360p, as many as the source is big enough for), keyframes every 4s and
aligned across rungs so a player can switch at any segment, two posters
and a storyboard (a grid of small frames for the seek bar). A job left
half-done by a restart starts again at startup (resume_jobs, from main.py).

Files live on the media share under videos/<file_id[:2]>/<file_id>/ and
are served only to people signed in, as immutable: file_id is new for each
upload. Under /api/posts/videos because Caddy only forwards a fixed list
of /api prefixes."""
import json
import math
import os
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ..auth import get_groups, get_username, is_admin
from ..config import MEDIA_DIR, VIDEO_DESC_MAX_CHARS, VIDEO_MAX_MB, VIDEO_PRESET, VIDEO_TITLE_MAX_CHARS
from ..db import get_db
from ..media import media_ready
from .posts import _reactions_and_reply_counts

router = APIRouter()
BASE = "/api/posts/videos"

_EXTS = {"mp4", "mov", "mkv", "webm", "avi", "m4v"}
_MAX_BYTES = VIDEO_MAX_MB * 1024 * 1024
_FILE_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,40}")
# Everything a video's folder serves; source.* is never among them.
_SERVED_RE = re.compile(r"master\.m3u8|v\d{1,2}/(?:index\.m3u8|init(?:_\d{1,2})?\.mp4|seg\d{1,6}\.m4s)|poster[A-Za-z0-9_-]{0,24}-[ls]\.jpg|storyboard\.jpg")
_TYPES = {"m3u8": "application/vnd.apple.mpegurl", "m4s": "video/iso.segment", "mp4": "video/mp4", "jpg": "image/jpeg"}
# The ladder: (rung, video bitrate). A rung is the picture's short side, so
# a portrait 1080x1920 phone video is "1080p" as it is everywhere else.
_LADDER = [(1080, 5000), (720, 2800), (480, 1200), (360, 700)]
SEGMENT = 4  # seconds, and the keyframe interval
_upload_slots = threading.BoundedSemaphore(1)
_jobs: "queue.Queue[int]" = queue.Queue()
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None
_VIEW_EVERY = 6 * 3600  # one view a person per 6h
DONE_AT = 0.95          # watched this far, a video is finished


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _signed_in(request: Request) -> str | None:
    u = get_username(request)
    return None if u == "anonymous" else u


def _folder(file_id: str) -> str:
    return os.path.join(MEDIA_DIR, "videos", file_id[:2], file_id)


def _remove_folder(file_id: str) -> None:
    if file_id and _FILE_ID_RE.fullmatch(file_id):
        shutil.rmtree(_folder(file_id), ignore_errors=True)


def _even(n: float) -> int:
    return max(2, int(round(n / 2)) * 2)


# ── Reading a file: ffprobe ──

def probe(path: str) -> dict | None:
    """The first video stream's shown size (after rotation), the length and
    whether there's sound; None when there's no video in it."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
            capture_output=True, timeout=120, check=True,
        ).stdout
        info = json.loads(out or b"{}")
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if not video or not video.get("width") or not video.get("height"):
        return None
    w, h = int(video["width"]), int(video["height"])
    rotation = 0
    try:
        rotation = int(float((video.get("tags") or {}).get("rotate", 0)))
    except ValueError:
        pass
    for sd in video.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rotation = int(float(sd["rotation"]))
            except (TypeError, ValueError):
                pass
    if abs(rotation) % 180 == 90:
        w, h = h, w
    duration = None
    for src in (info.get("format") or {}, video):
        try:
            d = float(src.get("duration") or 0)
        except (TypeError, ValueError):
            d = 0
        if d > 0:
            duration = d
            break
    return {"width": w, "height": h, "duration": duration,
            "audio": any(s.get("codec_type") == "audio" for s in streams)}


def _rungs(w: int, h: int) -> list[dict]:
    """The ladder for a w x h picture: each rung's size and bitrates."""
    short = min(w, h)
    picked = [(p, kbps) for p, kbps in _LADDER if p <= short]
    if not picked:
        picked = [(short, 500)]
    out = []
    for p, kbps in picked:
        scale = p / short
        out.append({"p": p, "w": _even(w * scale), "h": _even(h * scale), "kbps": kbps})
    return out


# ── Making the ladder: ffmpeg ──

def _set(post_id: int, **cols) -> None:
    keys = ", ".join(f"{k}=?" for k in cols)
    with get_db() as db:
        db.execute(f"UPDATE videos SET {keys}, updated=datetime('now') WHERE post_id=?", (*cols.values(), post_id))
        db.commit()


def _die_with_us() -> None:
    """In the child, before ffmpeg starts: die when this server does, so a
    restart's job never races one left over from before it (Linux only)."""
    try:
        import ctypes
        import signal
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:
        pass


def _run(cmd: list[str], post_id: int, duration: float | None, lo: float, hi: float) -> None:
    """ffmpeg, writing how far it's got into the row (lo..hi of the whole
    job) at most every 2s. Raises RuntimeError with its last words."""
    proc = subprocess.Popen(cmd + ["-progress", "pipe:1", "-nostats"], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, preexec_fn=_die_with_us)
    tail: list[str] = []

    def drain():
        for line in proc.stderr:
            tail.append(line.rstrip())
            del tail[:-20]
    t = threading.Thread(target=drain, daemon=True)
    t.start()
    last = 0.0
    for line in proc.stdout:
        key, _, value = line.strip().partition("=")
        if key == "out_time_us" and duration and value.isdigit():
            now = time.monotonic()
            if now - last >= 2:
                last = now
                done = min(1.0, int(value) / 1e6 / duration)
                _set(post_id, progress=round(lo + (hi - lo) * done, 3))
    proc.wait()
    t.join(timeout=5)
    if proc.returncode != 0:
        raise RuntimeError(" / ".join(l for l in tail[-3:] if l) or f"ffmpeg exited {proc.returncode}")


def _ladder_cmd(src: str, out: str, rungs: list[dict], audio: bool) -> list[str]:
    n = len(rungs)
    split = f"[0:v]split={n}" + "".join(f"[s{i}]" for i in range(n))
    chains = [split] + [f"[s{i}]scale={r['w']}:{r['h']}:flags=lanczos,format=yuv420p[v{i}]" for i, r in enumerate(rungs)]
    cmd = ["ffmpeg", "-hide_banner", "-y", "-i", src, "-filter_complex", ";".join(chains)]
    for i, r in enumerate(rungs):
        cmd += ["-map", f"[v{i}]"]
        if audio:
            cmd += ["-map", "0:a:0"]
    cmd += ["-c:v", "libx264", "-preset", VIDEO_PRESET, "-profile:v", "high",
            "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT})", "-sc_threshold", "0"]
    for i, r in enumerate(rungs):
        cmd += [f"-b:v:{i}", f"{r['kbps']}k", f"-maxrate:v:{i}", f"{int(r['kbps'] * 1.5)}k",
                f"-bufsize:v:{i}", f"{r['kbps'] * 2}k"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000"]
    stream_map = " ".join(f"v:{i},a:{i}" if audio else f"v:{i}" for i in range(n))
    cmd += ["-f", "hls", "-hls_time", str(SEGMENT), "-hls_playlist_type", "vod",
            "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
            "-hls_flags", "independent_segments",
            "-hls_segment_filename", os.path.join(out, "v%v", "seg%d.m4s"),
            "-master_pl_name", "master.m3u8", "-var_stream_map", stream_map,
            os.path.join(out, "v%v", "index.m3u8")]
    return cmd


def _stills(src: str, out: str, post_id: int, duration: float | None, w: int, h: int) -> dict:
    """The posters (1280 and 480 wide, at 10% in) and the storyboard."""
    at = (duration or 0) * 0.1
    for name, width in (("poster-l.jpg", 1280), ("poster-s.jpg", 480)):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.2f}", "-i", src,
                        "-frames:v", "1", "-vf", f"scale={min(width, _even(w))}:-2", "-q:v", "3",
                        os.path.join(out, name)], check=True, timeout=300)
    if not duration:
        return {}
    interval = max(2.0, duration / 100)
    count = max(1, min(100, int(duration // interval)))
    cols = min(10, count)
    rows = math.ceil(count / cols)
    tw, th = 160, _even(160 * h / w)
    _run(["ffmpeg", "-hide_banner", "-y", "-i", src, "-vf",
          f"fps=1/{interval:.4f},scale={tw}:{th},tile={cols}x{rows}", "-frames:v", "1", "-q:v", "5",
          os.path.join(out, "storyboard.jpg")], post_id, duration, 0.9, 1.0)
    return {"cols": cols, "rows": rows, "w": tw, "h": th, "interval": round(interval, 3), "count": count}


def _transcode(post_id: int) -> None:
    with get_db() as db:
        row = db.execute("SELECT * FROM videos WHERE post_id=?", (post_id,)).fetchone()
    if row is None or row["status"] != "processing":
        return
    folder = _folder(row["file_id"])
    src = os.path.join(folder, f"source.{row['ext']}")
    try:
        info = probe(src)
        if info is None:
            raise RuntimeError("couldn't find a video in that file")
        rungs = _rungs(info["width"], info["height"])
        # A restart mid-job leaves half a ladder: start it clean.
        for name in os.listdir(folder):
            if name != f"source.{row['ext']}":
                p = os.path.join(folder, name)
                if os.path.isdir(p):
                    shutil.rmtree(p)
                else:
                    os.remove(p)
        for i in range(len(rungs)):
            os.makedirs(os.path.join(folder, f"v{i}"), exist_ok=True)
        _set(post_id, progress=0, width=info["width"], height=info["height"], duration=info["duration"])
        _run(_ladder_cmd(src, folder, rungs, info["audio"]), post_id, info["duration"], 0.0, 0.9)
        board = _stills(src, folder, post_id, info["duration"], info["width"], info["height"])
        _set(post_id, status="ready", progress=1, error="", rungs=json.dumps(rungs),
             storyboard=json.dumps(board) if board else "", poster=row["poster"] or "poster")
    except Exception as e:  # noqa: BLE001 — anything at all fails the video, not the worker
        msg = str(e) or e.__class__.__name__
        print(f"video {post_id}: {msg}")
        if _exists(post_id):
            _set(post_id, status="failed", error=msg[:300])


def _exists(post_id: int) -> bool:
    with get_db() as db:
        return db.execute("SELECT 1 FROM videos WHERE post_id=?", (post_id,)).fetchone() is not None


def _work() -> None:
    while True:
        post_id = _jobs.get()
        try:
            _transcode(post_id)
        finally:
            _jobs.task_done()


def _enqueue(post_id: int) -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, name="video-transcode", daemon=True)
            _worker.start()
    _jobs.put(post_id)


def resume_jobs() -> None:
    """At startup: videos a restart interrupted (or that were still queued)."""
    if not shutil.which("ffmpeg"):
        print("videos: ffmpeg isn't installed; uploads will wait")
        return
    with get_db() as db:
        ids = [r[0] for r in db.execute("SELECT post_id FROM videos WHERE status='processing' ORDER BY post_id")]
    for post_id in ids:
        _enqueue(post_id)


# ── The JSON ──

_COLS = """v.*, p.username, p.text, p.created,
           (SELECT COUNT(*) FROM video_views w WHERE w.post_id = v.post_id) AS views"""


def _can_edit(owner: str, request: Request, username: str) -> bool:
    return username == owner or is_admin(get_groups(request))


def _urls(r) -> dict:
    base = f"{BASE}/{r['post_id']}/files/{r['file_id']}"
    ready = r["status"] == "ready"
    stem = r["poster"] or "poster"
    board = None
    if ready and r["storyboard"]:
        try:
            board = {"url": f"{base}/storyboard.jpg", **json.loads(r["storyboard"])}
        except ValueError:
            board = None
    return {
        "hls": f"{base}/master.m3u8" if ready else None,
        "poster": f"{base}/{stem}-l.jpg" if ready else None,
        "posterSmall": f"{base}/{stem}-s.jpg" if ready else None,
        "storyboard": board,
    }


def _json_many(db, rows, request: Request, username: str) -> list[dict]:
    ids = [r["post_id"] for r in rows]
    reactions, replies = _reactions_and_reply_counts(db, ids, username)
    watched = {}
    if ids:
        marks = ",".join("?" * len(ids))
        watched = {w["post_id"]: w["position"] for w in db.execute(
            f"SELECT post_id, position FROM video_progress WHERE username=? AND post_id IN ({marks})", (username, *ids))}
    admin = is_admin(get_groups(request))
    return [{
        "id": r["post_id"], "title": r["title"], "description": r["text"], "username": r["username"],
        "created": r["created"], "duration": r["duration"], "width": r["width"], "height": r["height"],
        "status": r["status"], "progress": r["progress"], "error": r["error"] or None,
        **_urls(r),
        "views": r["views"], "reactions": reactions.get(r["post_id"], {}), "reply_count": replies.get(r["post_id"], 0),
        "watched": watched.get(r["post_id"]),
        "canEdit": admin or r["username"] == username,
        **({"unlisted": True} if r["unlisted"] else {}),
    } for r in rows]


def _one(db, post_id: int, request: Request, username: str) -> dict | None:
    r = db.execute(f"SELECT {_COLS} FROM videos v JOIN posts p ON p.id = v.post_id WHERE v.post_id=?", (post_id,)).fetchone()
    return _json_many(db, [r], request, username)[0] if r else None


def card(db, post_id: int) -> dict | None:
    """A video as a feed post shows it (routers/posts.py), or None if it's gone."""
    r = db.execute("""SELECT v.post_id, v.title, v.file_id, v.status, v.poster, v.storyboard, v.duration, p.username
                      FROM videos v JOIN posts p ON p.id = v.post_id WHERE v.post_id=?""", (post_id,)).fetchone()
    if r is None:
        return None
    u = _urls(r)
    return {"id": r["post_id"], "title": r["title"], "poster": u["poster"], "posterSmall": u["posterSmall"],
            "duration": r["duration"], "username": r["username"], "status": r["status"]}


# ── Routes ──

def _save_upload(file: UploadFile) -> tuple[str, str, int] | JSONResponse:
    ext = (os.path.splitext(file.filename or "")[1][1:] or "").lower()
    if ext not in _EXTS:
        return _error(400, "a video is an mp4, mov, mkv, webm, avi or m4v file")
    file_id = secrets.token_urlsafe(12)
    folder = _folder(file_id)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"source.{ext}")
    tmp = path + ".partial"
    size = 0
    try:
        with _upload_slots, open(tmp, "wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_BYTES:
                    raise OverflowError
                out.write(chunk)
        os.replace(tmp, path)
    except OverflowError:
        _remove_folder(file_id)
        return _error(413, f"a video can be up to {VIDEO_MAX_MB}MB")
    except Exception:
        _remove_folder(file_id)
        raise
    return file_id, ext, size


def create(request: Request, username: str, file: UploadFile, title: str, description: str, unlisted: bool = False) -> int | JSONResponse:
    """An upload made into a video and queued for ffmpeg: its post id. An
    unlisted one (uploaded straight into a TV channel, routers/tv.py) is
    left out of Videos' lists."""
    if not media_ready():
        return _error(503, "storage is offline")
    if not shutil.which("ffmpeg"):
        return _error(503, "this server can't make videos yet (no ffmpeg)")
    if len(description.strip()) > VIDEO_DESC_MAX_CHARS:
        return _error(400, f"a description can be up to {VIDEO_DESC_MAX_CHARS:,} characters")
    title = " ".join(title.split())[:VIDEO_TITLE_MAX_CHARS] or os.path.splitext(file.filename or "")[0][:VIDEO_TITLE_MAX_CHARS].strip() or "Untitled"
    saved = _save_upload(file)
    if isinstance(saved, JSONResponse):
        return saved
    file_id, ext, size = saved
    info = probe(os.path.join(_folder(file_id), f"source.{ext}"))
    if info is None:
        _remove_folder(file_id)
        return _error(400, "couldn't find a video in that file")
    with get_db() as db:
        cur = db.execute("INSERT INTO posts(username, text, kind) VALUES (?, ?, 'video')", (username, description.strip()))
        post_id = cur.lastrowid
        db.execute("""INSERT INTO videos(post_id, title, file_id, ext, duration, width, height, size, unlisted)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (post_id, title, file_id, ext, info["duration"], info["width"], info["height"], size, 1 if unlisted else 0))
        db.commit()
    _enqueue(post_id)
    return post_id


@router.post(BASE)
def upload(request: Request, file: UploadFile = File(...), title: str = Form(""), description: str = Form("")):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    post_id = create(request, username, file, title, description)
    if isinstance(post_id, JSONResponse):
        return post_id
    with get_db() as db:
        return _one(db, post_id, request, username)


@router.get(BASE)
def list_videos(request: Request, limit: int = 24, before_id: int | None = None, offset: int = 0,
                user: str = "", sort: str = "new", q: str = ""):
    """Newest first (before_id pages back), or the most watched (offset
    pages). Other people's only once they're ready; yours always, so you
    can see yours being made."""
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    limit = max(1, min(limit, 100))
    where, args = ["(v.status='ready' OR p.username=?)", "v.unlisted=0"], [username]
    if user:
        where.append("p.username=?"); args.append(user.strip().lower())
    for word in q.split()[:8]:
        like = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(v.title LIKE ? ESCAPE '\\' OR p.text LIKE ? ESCAPE '\\')"); args += [like, like]
    cond = " AND ".join(where)
    with get_db() as db:
        total = db.execute(f"SELECT COUNT(*) FROM videos v JOIN posts p ON p.id = v.post_id WHERE {cond}", args).fetchone()[0]
        if sort == "popular":
            rows = db.execute(f"""SELECT {_COLS} FROM videos v JOIN posts p ON p.id = v.post_id WHERE {cond}
                                  ORDER BY views DESC, v.post_id DESC LIMIT ? OFFSET ?""", (*args, limit, max(0, offset))).fetchall()
        else:
            page = cond + (" AND v.post_id < ?" if before_id is not None else "")
            rows = db.execute(f"""SELECT {_COLS} FROM videos v JOIN posts p ON p.id = v.post_id WHERE {page}
                                  ORDER BY v.post_id DESC LIMIT ?""",
                              (*args, *([before_id] if before_id is not None else []), limit)).fetchall()
        return {"videos": _json_many(db, rows, request, username), "total": total}


@router.get(f"{BASE}/continue")
def continue_watching(request: Request):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    with get_db() as db:
        rows = db.execute(f"""SELECT {_COLS} FROM video_progress g JOIN videos v ON v.post_id = g.post_id
                              JOIN posts p ON p.id = v.post_id
                              WHERE g.username=? AND v.status='ready' AND v.unlisted=0 AND g.position > 5
                                AND (v.duration IS NULL OR g.position < v.duration * ?)
                              ORDER BY g.updated DESC LIMIT 20""", (username, DONE_AT)).fetchall()
        return {"videos": _json_many(db, rows, request, username)}


@router.get(f"{BASE}/{{post_id}}")
def get_video(post_id: int, request: Request):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    with get_db() as db:
        out = _one(db, post_id, request, username)
    return out or _error(404, "not found")


def _editable(post_id: int, request: Request, username: str):
    with get_db() as db:
        row = db.execute("SELECT v.*, p.username FROM videos v JOIN posts p ON p.id = v.post_id WHERE v.post_id=?", (post_id,)).fetchone()
    if row is None:
        return None, _error(404, "not found")
    if not _can_edit(row["username"], request, username):
        return None, _error(403, "not your video")
    return row, None


@router.patch(f"{BASE}/{{post_id}}")
async def edit_video(post_id: int, request: Request):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    row, err = _editable(post_id, request, username)
    if err:
        return err
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
    except Exception:
        return _error(400, "invalid JSON")
    with get_db() as db:
        if "title" in body:
            title = " ".join(str(body["title"] or "").split())[:VIDEO_TITLE_MAX_CHARS]
            if not title:
                return _error(400, "a video needs a title")
            db.execute("UPDATE videos SET title=?, updated=datetime('now') WHERE post_id=?", (title, post_id))
        if "description" in body:
            desc = str(body["description"] or "").strip()
            if len(desc) > VIDEO_DESC_MAX_CHARS:
                return _error(400, f"a description can be up to {VIDEO_DESC_MAX_CHARS:,} characters")
            db.execute("UPDATE posts SET text=? WHERE id=?", (desc, post_id))
        db.commit()
        return _one(db, post_id, request, username)


@router.put(f"{BASE}/{{post_id}}/poster")
def upload_poster(post_id: int, request: Request, file: UploadFile = File(...)):
    """A thumbnail of the owner's choosing, in place of the frame at 10%."""
    from PIL import Image, ImageOps
    import io
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    row, err = _editable(post_id, request, username)
    if err:
        return err
    if row["status"] != "ready":
        return _error(409, "the video's still being made")
    if not media_ready():
        return _error(503, "storage is offline")
    data = file.file.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024:
        return _error(413, "that image is too big")
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    except Exception:
        return _error(400, "that isn't an image")
    stem = "poster" + secrets.token_hex(4)
    folder = _folder(row["file_id"])
    for size, width in (("l", 1280), ("s", 480)):
        im = img.copy()
        if im.width > width:
            im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
        im.save(os.path.join(folder, f"{stem}-{size}.jpg"), "JPEG", quality=85, optimize=True, progressive=True)
    old = row["poster"]
    _set(post_id, poster=stem)
    if old and old != "poster":
        for size in ("l", "s"):
            try:
                os.remove(os.path.join(folder, f"{old}-{size}.jpg"))
            except OSError:
                pass
    with get_db() as db:
        return _one(db, post_id, request, username)


def delete_files_for(post_id: int, db) -> str | None:
    """Drops a video's rows (not the post's own) and hands back its file id,
    for routers/posts.py's delete and this one's."""
    r = db.execute("SELECT file_id FROM videos WHERE post_id=?", (post_id,)).fetchone()
    db.execute("DELETE FROM videos WHERE post_id=?", (post_id,))
    db.execute("DELETE FROM video_progress WHERE post_id=?", (post_id,))
    db.execute("DELETE FROM video_views WHERE post_id=?", (post_id,))
    db.execute("DELETE FROM tv_items WHERE video_id=?", (post_id,))
    return r["file_id"] if r else None


@router.delete(f"{BASE}/{{post_id}}")
def delete_video(post_id: int, request: Request):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    row, err = _editable(post_id, request, username)
    if err:
        return err
    with get_db() as db:
        file_id = delete_files_for(post_id, db)
        for table in ("post_tags", "post_reactions", "post_replies"):
            db.execute(f"DELETE FROM {table} WHERE post_id=?", (post_id,))
        db.execute("DELETE FROM posts WHERE id=?", (post_id,))
        db.commit()
    # A job still running on it fails once its folder is gone, finds no
    # row, and leaves it be.
    _remove_folder(file_id)
    return {"ok": True}


@router.post(f"{BASE}/{{post_id}}/view")
def count_view(post_id: int, request: Request):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    now = time.time()
    with get_db() as db:
        if db.execute("SELECT 1 FROM videos WHERE post_id=?", (post_id,)).fetchone() is None:
            return _error(404, "not found")
        db.execute("""INSERT INTO video_views(post_id, username, at) VALUES (?, ?, ?)
                      ON CONFLICT(post_id, username) DO UPDATE SET at=excluded.at WHERE video_views.at < ?""",
                   (post_id, username, now, now - _VIEW_EVERY))
        db.commit()
        views = db.execute("SELECT COUNT(*) FROM video_views WHERE post_id=?", (post_id,)).fetchone()[0]
    return {"views": views}


@router.put(f"{BASE}/{{post_id}}/progress")
async def save_progress(post_id: int, request: Request):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    try:
        body = await request.json()
        position = float(body.get("position"))
        if not math.isfinite(position) or position < 0:
            raise ValueError
    except Exception:
        return _error(400, "position (seconds) required")
    with get_db() as db:
        r = db.execute("SELECT duration FROM videos WHERE post_id=?", (post_id,)).fetchone()
        if r is None:
            return _error(404, "not found")
        if r["duration"] and position >= r["duration"] * DONE_AT:
            db.execute("DELETE FROM video_progress WHERE username=? AND post_id=?", (username, post_id))
        else:
            db.execute("""INSERT INTO video_progress(username, post_id, position) VALUES (?, ?, ?)
                          ON CONFLICT(username, post_id) DO UPDATE SET position=excluded.position, updated=datetime('now')""",
                       (username, post_id, round(position, 1)))
        db.commit()
    return {"ok": True}


@router.get(f"{BASE}/{{post_id}}/files/{{file_id}}/{{path:path}}")
def video_file(post_id: int, file_id: str, path: str, request: Request):
    if not _signed_in(request):
        return _error(401, "not authenticated")
    if not _FILE_ID_RE.fullmatch(file_id) or not _SERVED_RE.fullmatch(path):
        return _error(404, "not found")
    with get_db() as db:
        r = db.execute("SELECT file_id FROM videos WHERE post_id=?", (post_id,)).fetchone()
    if r is None or r["file_id"] != file_id:
        return _error(404, "not found")
    full = os.path.join(_folder(file_id), *path.split("/"))
    if not os.path.isfile(full):
        return _error(404, "not found")
    return FileResponse(full, media_type=_TYPES[path.rsplit(".", 1)[1]],
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})
