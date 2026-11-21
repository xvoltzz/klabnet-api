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
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ..auth import get_groups, get_username, is_admin
from ..config import (CLIP_MAX_SECONDS, MEDIA_DIR, VIDEO_AUDIO_KBPS, VIDEO_CRF, VIDEO_DESC_MAX_CHARS, VIDEO_MAX_MB, VIDEO_PRESET,
                      VIDEO_TITLE_MAX_CHARS)
from .. import encoder
from ..db import get_db
from ..media import media_ready
from .posts import _reactions_and_reply_counts

router = APIRouter()
BASE = "/api/posts/videos"
# Playlists first: their paths must win over /{post_id} (routers/video_playlists.py).
from .video_playlists import router as _playlists_router  # noqa: E402
router.include_router(_playlists_router)
# Clips' too (/clips, /clips/games; routers/clips.py).
from .clips import router as _clips_router  # noqa: E402
router.include_router(_clips_router)

_EXTS = {"mp4", "mov", "mkv", "webm", "avi", "m4v"}
_MAX_BYTES = VIDEO_MAX_MB * 1024 * 1024
_FILE_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,40}")
# Everything a video's folder serves; source.* and thumb.* never are.
_SERVED_RE = re.compile(r"master\.m3u8|v\d{1,2}/(?:index\.m3u8|init(?:_\d{1,2})?\.mp4|seg\d{1,6}\.m4s)|poster[A-Za-z0-9_-]{0,24}-[ls]\.jpg|storyboard\.jpg|original\.mp4")
_TYPES = {"m3u8": "application/vnd.apple.mpegurl", "m4s": "video/iso.segment", "mp4": "video/mp4", "jpg": "image/jpeg"}
# The ladder: (rung, ceiling kbps at <=30fps, at >30fps). A rung is the
# picture's short side, so a portrait 1080x1920 phone video is "1080p" as
# it is everywhere else. x264 aims for a look (VIDEO_CRF), not a bitrate:
# simple pictures stay small, and busy ones get bits up to the ceiling.
_LADDER = [(2160, 30000, 45000), (1440, 16000, 24000), (1080, 10000, 15000),
           (720, 5000, 7500), (480, 2500, 3500), (360, 1200, 1800)]
SEGMENT = 4  # seconds, and the keyframe interval (time-based, so any frame rate)
MAX_FPS = 60
# What a browser plays straight from an mp4 (the Original quality).
_DIRECT_VIDEO = {"h264", "hevc", "av1", "vp9"}
_DIRECT_AUDIO = {"aac", "opus", "mp3"}
_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
_upload_slots = threading.BoundedSemaphore(1)
_jobs: "queue.Queue[int]" = queue.Queue()
_queued: set[int] = set()  # in _jobs or being made now
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None
_POLL = 20  # seconds: the worker looks for videos queued by another process (the importer)
_VIEW_EVERY = 6 * 3600  # one view a person per 6h
DONE_AT = 0.95          # watched this far, a video is finished
_BACKDATED = "-2 days"  # published this long before it arrived: imported, not news


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


# ── When it came out ──

def parse_published(raw) -> str | None:
    """A published date as the site or the importer sends it (2019-03-03,
    2019-03-03T14:05[:00][Z|+01:00], yt-dlp's 20190303, or unix seconds) as
    the database keeps times ('YYYY-MM-DD HH:MM:SS', UTC); None when blank.
    A bare date is noon UTC, so it's that day almost anywhere. Raises
    ValueError with a message for the uploader."""
    s = str(raw if raw is not None else "").strip()
    if not s:
        return None
    try:
        if re.fullmatch(r"\d{8}", s):
            dt = datetime.strptime(s + "12", "%Y%m%d%H").replace(tzinfo=timezone.utc)
        elif re.fullmatch(r"\d{9,11}(?:\.\d+)?", s):
            dt = datetime.fromtimestamp(float(s), timezone.utc)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            dt = datetime.fromisoformat(s + "T12:00:00+00:00")
        else:
            dt = datetime.fromisoformat(s.replace(" ", "T").replace("z", "Z").replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise ValueError("that isn't a date")
    dt = dt.astimezone(timezone.utc)
    if dt > datetime.now(timezone.utc) + timedelta(minutes=1):
        raise ValueError("a video can't come out in the future")
    if dt.year < 1970:
        raise ValueError("that date's too long ago")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ── Reading a file: ffprobe ──

def _rate(s) -> float:
    try:
        n, _, d = str(s or "0").partition("/")
        return float(n) / float(d or 1) if float(d or 1) else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def probe(path: str) -> dict | None:
    """The first video stream's shown size (after rotation), the length,
    frame rate, codecs, colour and the sound; None when there's no video."""
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
    fmt = info.get("format") or {}
    duration = None
    for src in (fmt, video):
        try:
            d = float(src.get("duration") or 0)
        except (TypeError, ValueError):
            d = 0
        if d > 0:
            duration = d
            break
    # The average rate is the honest one for variable-rate phone video
    # (r_frame_rate is the container's tick, often 90000 or 120).
    avg, real = _rate(video.get("avg_frame_rate")), _rate(video.get("r_frame_rate"))
    fps = avg if 1 <= avg <= 1000 else (real if 1 <= real <= 1000 else 30.0)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    try:
        abr = int(audio.get("bit_rate") or 0) if audio else 0
    except (TypeError, ValueError):
        abr = 0
    try:
        bitrate = int(fmt.get("bit_rate") or 0)
    except (TypeError, ValueError):
        bitrate = 0
    return {"width": w, "height": h, "duration": duration, "fps": round(fps, 3),
            "fps_raw": video.get("avg_frame_rate") or "", "fps_nominal": round(real, 3),
            "fps_nominal_raw": video.get("r_frame_rate") or "", "vfr": bool(avg and real and abs(avg - real) > 0.5),
            "vcodec": video.get("codec_name") or "", "pix_fmt": video.get("pix_fmt") or "",
            "hdr": (video.get("color_transfer") or "") in _HDR_TRANSFERS,
            "bitrate": bitrate, "audio": audio is not None,
            "acodec": (audio or {}).get("codec_name") or "", "achannels": int((audio or {}).get("channels") or 0),
            "abitrate": abr}


def _out_fps(info: dict) -> tuple[str, float]:
    """The ladder's frame rate: the source's own (as ffmpeg's rational, so
    29.97 stays 30000/1001), capped at 60."""
    fps = info["fps"]
    if info.get("vfr") and 1 <= info.get("fps_nominal", 0) <= MAX_FPS + 0.5:
        # A variable-rate phone video: its nominal rate (30, 60), so motion
        # keeps its timing; slow stretches repeat frames.
        return info["fps_nominal_raw"], info["fps_nominal"]
    if fps > MAX_FPS + 0.5:
        return str(MAX_FPS), float(MAX_FPS)
    raw = info.get("fps_raw") or ""
    if re.fullmatch(r"\d{1,7}/\d{1,7}", raw) and abs(_rate(raw) - fps) < 0.01:
        return raw, fps
    return f"{fps:.3f}", fps


def _rungs(w: int, h: int, fps: float = 30.0) -> list[dict]:
    """The ladder for a w x h picture: each rung's size, frame rate and
    bitrate ceiling."""
    short = min(w, h)
    high = fps > 30.5
    picked = [(p, hi if high else lo) for p, lo, hi in _LADDER if p <= short]
    if not picked:
        picked = [(_even(short), 800)]
    out = []
    for p, kbps in picked:
        scale = p / short
        out.append({"p": p, "w": _even(w * scale), "h": _even(h * scale), "kbps": kbps, "fps": round(fps, 3)})
    return out


# ── Making the ladder: ffmpeg ──

def _set(post_id: int, **cols) -> None:
    keys = ", ".join(f"{k}=?" for k in cols)
    with get_db() as db:
        db.execute(f"UPDATE videos SET {keys}, updated=datetime('now') WHERE post_id=?", (*cols.values(), post_id))
        db.commit()


def _die_with_us() -> None:
    """In the child, before ffmpeg starts: die when this server does, so a
    restart's job never races one left over from before it (Linux only).
    And a step back in the CPU queue, so a long encode doesn't starve the
    API itself (an upload arriving while another video is being made)."""
    try:
        os.nice(10)
    except OSError:
        pass
    try:
        import ctypes
        import signal
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:
        pass


def _run(cmd: list[str], duration: float | None, lo: float, hi: float, report=None) -> None:
    """ffmpeg, telling report() how far it's got (lo..hi of the whole job)
    at most every 2s. Raises RuntimeError with its last words."""
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
        if report and key == "out_time_us" and duration and value.isdigit():
            now = time.monotonic()
            if now - last >= 2:
                last = now
                report(round(lo + (hi - lo) * min(1.0, int(value) / 1e6 / duration), 3))
    proc.wait()
    t.join(timeout=5)
    if proc.returncode != 0:
        raise RuntimeError(" / ".join(l for l in tail[-3:] if l) or f"ffmpeg exited {proc.returncode}")



def _encode(make, duration: float | None, lo: float, hi: float, report=None, what: str = "a video",
            clean=None) -> str:
    """An encode on the chosen encoder (app/encoder.py): make(enc) is its
    ffmpeg. On the GPU, one job at a time; a GPU failure is logged and the
    same encode run again with x264 (after clean(), which clears what the
    failed run left), so no video fails because of the GPU. Returns the
    encoder that made it."""
    if encoder.choice() == "nvenc":
        try:
            with encoder.slot:
                _run(make("nvenc"), duration, lo, hi, report)
            return "nvenc"
        except RuntimeError as e:
            print(f"videos: NVENC failed for {what} ({e}); making it again with x264")
            if clean:
                clean()
    _run(make("x264"), duration, lo, hi, report)
    return "x264"


_zscale: bool | None = None


def _has_zscale() -> bool:
    global _zscale
    if _zscale is None:
        try:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=30).stdout
            _zscale = bool(re.search(r"\szscale\s", out)) and bool(re.search(r"\stonemap\s", out))
        except (subprocess.SubprocessError, OSError):
            _zscale = False
    return _zscale


def _ladder_cmd(src: str, out: str, rungs: list[dict], info: dict, enc: str = "x264") -> list[str]:
    """ffmpeg for the ladder, with x264 (the CPU) or NVENC (the GPU:
    app/encoder.py). Same rungs, frame rate and keyframes either way."""
    n = len(rungs)
    rate, fps = _out_fps(info)
    gpu_all = enc == "nvenc" and encoder.gpu_ladder(info)
    # One frame rate for every rung (the source's, or 60 above that; a
    # variable-rate phone video comes out constant at its average), and
    # HDR brought to SDR once, before the split, so it isn't washed out.
    pre = [f"fps={rate}"]
    if info.get("hdr"):
        if _has_zscale():
            pre += ["zscale=t=linear:npl=100", "format=gbrpf32le", "zscale=p=bt709",
                    "tonemap=tonemap=hable:desat=0", "zscale=t=bt709:m=bt709:r=tv"]
        else:
            print("videos: an HDR source, and this ffmpeg has no zscale: the ladder will look washed out")
    chains = [f"[0:v]{','.join(pre)},split={n}" + "".join(f"[s{i}]" for i in range(n))]
    if gpu_all:
        # Decoded on the GPU and scaled there: the frames never leave it.
        chains += [f"[s{i}]scale_cuda={r['w']}:{r['h']}:interp_algo=lanczos:format=yuv420p[v{i}]" for i, r in enumerate(rungs)]
        head = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-extra_hw_frames", "8"]
    else:
        chains += [f"[s{i}]scale={r['w']}:{r['h']}:flags=lanczos,format=yuv420p[v{i}]" for i, r in enumerate(rungs)]
        # NVENC with CPU filters: NVDEC still decodes what it can (frames
        # come back to the CPU for zscale and scale).
        head = ["-hwaccel", "cuda"] if enc == "nvenc" and encoder.gpu_decodes(info) else []
    cmd = ["ffmpeg", "-hide_banner", "-y", *head, "-i", src, "-filter_complex", ";".join(chains)]
    audio = info["audio"]
    for i in range(n):
        cmd += ["-map", f"[v{i}]"]
        if audio:
            cmd += ["-map", "0:a:0"]
    if enc == "nvenc":
        cmd += encoder.nvenc_ladder_args(fps, SEGMENT)
    else:
        cmd += ["-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", str(VIDEO_CRF), "-profile:v", "high",
                "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT})", "-sc_threshold", "0"]
    if info.get("hdr"):
        cmd += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    for i, r in enumerate(rungs):
        cmd += [f"-maxrate:v:{i}", f"{r['kbps']}k", f"-bufsize:v:{i}", f"{r['kbps'] * 2}k"]
    if audio:
        # Already AAC stereo at a sane rate: kept as it is.
        if info["acodec"] == "aac" and info["achannels"] in (1, 2) and 0 < info["abitrate"] <= 256000:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", f"{VIDEO_AUDIO_KBPS}k", "-ac", "2", "-ar", "48000"]
    stream_map = " ".join(f"v:{i},a:{i}" if audio else f"v:{i}" for i in range(n))
    cmd += ["-f", "hls", "-hls_time", str(SEGMENT), "-hls_playlist_type", "vod",
            "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
            "-hls_flags", "independent_segments",
            "-hls_segment_filename", os.path.join(out, "v%v", "seg%d.m4s"),
            "-master_pl_name", "master.m3u8", "-var_stream_map", stream_map,
            os.path.join(out, "v%v", "index.m3u8")]
    return cmd


def _fix_master(out: str, rungs: list[dict]) -> None:
    """ffmpeg's master playlist leaves FRAME-RATE out (and RESOLUTION, with
    some versions): put them in, so players can say 1080p60."""
    path = os.path.join(out, "master.m3u8")
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return
    fixed, k = [], 0
    for line in lines:
        if line.startswith("#EXT-X-STREAM-INF:") and k < len(rungs):
            r = rungs[k]
            k += 1
            if "RESOLUTION=" not in line:
                line += f",RESOLUTION={r['w']}x{r['h']}"
            if "FRAME-RATE=" not in line:
                line += f",FRAME-RATE={r['fps']:.3f}"
        fixed.append(line)
    tmp = path + ".partial"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(fixed) + "\n")
    os.replace(tmp, path)


def _poster_files(img, folder: str, stem: str) -> None:
    """A picture as the two poster files (1280 and 480 wide)."""
    from PIL import Image
    for size, width in (("l", 1280), ("s", 480)):
        im = img.copy()
        if im.width > width:
            im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
        im.save(os.path.join(folder, f"{stem}-{size}.jpg"), "JPEG", quality=85, optimize=True, progressive=True)


def _stills(src: str, out: str, duration: float | None, w: int, h: int, report=None) -> dict:
    """The posters (1280 and 480 wide, at 10% in, or from thumb.* when the
    video came with one: the importer's YouTube thumbnail) and the
    storyboard."""
    at = (duration or 0) * 0.1
    for name, width in (("poster-l.jpg", 1280), ("poster-s.jpg", 480)):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.2f}", "-i", src,
                        "-frames:v", "1", "-vf", f"scale={min(width, _even(w))}:-2", "-q:v", "3",
                        os.path.join(out, name)], check=True, timeout=300)
    thumb = next((n for n in os.listdir(out) if n.startswith("thumb.")), None)
    if thumb:
        try:
            from PIL import Image, ImageOps
            _poster_files(ImageOps.exif_transpose(Image.open(os.path.join(out, thumb))).convert("RGB"), out, "poster")
        except Exception as e:  # noqa: BLE001 — the frame at 10% will do
            print(f"videos: couldn't use {thumb} as the poster: {e}")
    if not duration:
        return {}
    interval = max(2.0, duration / 100)
    count = max(1, min(100, int(duration // interval)))
    cols = min(10, count)
    rows = math.ceil(count / cols)
    tw, th = 160, _even(160 * h / w)
    _run(["ffmpeg", "-hide_banner", "-y", "-i", src, "-vf",
          f"fps=1/{interval:.4f},scale={tw}:{th},tile={cols}x{rows}", "-frames:v", "1", "-q:v", "5",
          os.path.join(out, "storyboard.jpg")], duration, 0.9, 1.0, report)
    return {"cols": cols, "rows": rows, "w": tw, "h": th, "interval": round(interval, 3), "count": count}


def make_original(src: str, out: str, info: dict) -> dict | None:
    """The Original quality: the upload remuxed into an mp4 a browser plays
    straight (original.mp4, moov first), with no re-encode, when its
    codecs are ones browsers know; None otherwise (the ladder's top rung is
    then the best on offer, rather than transcoding a huge lossless file).
    HDR stays HDR."""
    if info["vcodec"] not in _DIRECT_VIDEO or (info["audio"] and info["acodec"] not in _DIRECT_AUDIO):
        return None
    dest = os.path.join(out, "original.mp4")
    tmp = dest + ".partial"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-map", "0:v:0"]
    if info["audio"]:
        cmd += ["-map", "0:a:0"]
    cmd += ["-c", "copy", "-movflags", "+faststart", "-map_metadata", "-1"]
    if info["vcodec"] == "hevc":
        cmd += ["-tag:v", "hvc1"]  # Safari's name for it
    cmd += ["-f", "mp4", tmp]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=3600, preexec_fn=_die_with_us)
        os.replace(tmp, dest)
    except (subprocess.SubprocessError, OSError) as e:
        print(f"videos: no Original for {src}: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    size = os.path.getsize(dest)
    rate = info["bitrate"] or (round(size * 8 / info["duration"]) if info["duration"] else 0)
    return {"width": info["width"], "height": info["height"], "fps": info["fps"], "codec": info["vcodec"],
            "bitrate": rate, "size": size, "hdr": bool(info.get("hdr"))}


def _keep(name: str, ext: str) -> bool:
    """What a fresh build of a folder leaves: the upload and its thumbnail."""
    return name == f"source.{ext}" or name.startswith("thumb.")


def _clear_ladder(folder: str, n: int) -> None:
    """What a failed ladder run left: empty rung folders and no master."""
    for i in range(n):
        p = os.path.join(folder, f"v{i}")
        shutil.rmtree(p, ignore_errors=True)
        os.makedirs(p, exist_ok=True)
    try:
        os.remove(os.path.join(folder, "master.m3u8"))
    except OSError:
        pass


def build(post_id: int, folder: str, ext: str, report=None) -> dict:
    """The whole job in a folder holding source.<ext> (and maybe thumb.*):
    the ladder, the posters, the storyboard and the Original. Returns the
    video row's new columns; raises on failure."""
    src = os.path.join(folder, f"source.{ext}")
    info = probe(src)
    if info is None:
        raise RuntimeError("couldn't find a video in that file")
    rungs = _rungs(info["width"], info["height"], _out_fps(info)[1])
    # A restart mid-job leaves half a ladder: start it clean.
    for name in os.listdir(folder):
        if not _keep(name, ext):
            p = os.path.join(folder, name)
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
    for i in range(len(rungs)):
        os.makedirs(os.path.join(folder, f"v{i}"), exist_ok=True)
    if report:
        report(0.0)
    _encode(lambda enc: _ladder_cmd(src, folder, rungs, info, enc), info["duration"], 0.0, 0.85, report,
            f"the ladder of {os.path.basename(folder)}", lambda: _clear_ladder(folder, len(rungs)))
    _fix_master(folder, rungs)
    original = make_original(src, folder, info)
    board = _stills(src, folder, info["duration"], info["width"], info["height"], report)
    return {"width": info["width"], "height": info["height"], "duration": info["duration"],
            "rungs": json.dumps(rungs), "storyboard": json.dumps(board) if board else "",
            "original": json.dumps(original) if original else ""}


def _transcode(post_id: int) -> None:
    with get_db() as db:
        row = db.execute("SELECT * FROM videos WHERE post_id=?", (post_id,)).fetchone()
    if row is None or row["status"] != "processing":
        return
    try:
        report = lambda f: _set(post_id, progress=f)
        # A clip made from a range of another video: that range is cut from
        # its upload first (routers/clips.py), then made like any upload.
        if row["clip_of"] and not os.path.isfile(os.path.join(_folder(row["file_id"]), f"source.{row['ext']}")):
            from .clips import cut
            cut(row, lambda f: _set(post_id, progress=round(f * 0.2, 3)))
            report = lambda f: _set(post_id, progress=round(0.2 + f * 0.8, 3))
        cols = build(post_id, _folder(row["file_id"]), row["ext"], report)
        _set(post_id, status="ready", progress=1, error="", poster=row["poster"] or "poster", **cols)
    except Exception as e:  # noqa: BLE001 — anything at all fails the video, not the worker
        msg = str(e) or e.__class__.__name__
        print(f"video {post_id}: {msg}")
        if _exists(post_id):
            _set(post_id, status="failed", error=msg[:300])


def rebuild(post_id: int, log=print) -> bool:
    """Makes a ready video's ladder again from its kept upload (a better
    encoder, a new rung), in a new folder under a new file id while the old
    one keeps playing, then swaps the row over and drops the old folder.
    File URLs are immutable, so a new folder is the only safe swap."""
    with get_db() as db:
        row = db.execute("SELECT * FROM videos WHERE post_id=?", (post_id,)).fetchone()
    if row is None:
        log(f"video {post_id}: not found")
        return False
    if "edit_live" in row.keys() and row["edit_live"]:
        # An edited video is made from its edit (routers/studio.py), never
        # from the upload alone.
        from .studio import make
        _set(post_id, render="queued")
        return make(post_id, log)
    old = _folder(row["file_id"])
    src = os.path.join(old, f"source.{row['ext']}")
    if not os.path.isfile(src):
        log(f"video {post_id}: its upload isn't there any more ({src})")
        return False
    new_id = secrets.token_urlsafe(12)
    new = _folder(new_id)
    os.makedirs(new, exist_ok=True)
    try:
        # The upload, its thumbnail and a custom poster come along (hard
        # links where the share allows, so nothing big is copied).
        stem = row["poster"] or "poster"
        for name in os.listdir(old):
            if _keep(name, row["ext"]) or (stem != "poster" and name.startswith(stem + "-")):
                try:
                    os.link(os.path.join(old, name), os.path.join(new, name))
                except OSError:
                    shutil.copy2(os.path.join(old, name), os.path.join(new, name))
        started = time.monotonic()
        cols = build(post_id, new, row["ext"])
        # build() cleared everything but the upload: the custom poster again.
        if stem != "poster":
            for size in ("l", "s"):
                p = os.path.join(old, f"{stem}-{size}.jpg")
                if os.path.isfile(p):
                    shutil.copy2(p, os.path.join(new, f"{stem}-{size}.jpg"))
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(new, ignore_errors=True)
        log(f"video {post_id}: {e}")
        return False
    with get_db() as db:
        cur = db.execute("SELECT file_id FROM videos WHERE post_id=?", (post_id,)).fetchone()
        if cur is None or cur["file_id"] != row["file_id"]:
            db.rollback()
            shutil.rmtree(new, ignore_errors=True)
            log(f"video {post_id}: changed while it was being made; left as it was")
            return False
        keys = ", ".join(f"{k}=?" for k in cols)
        db.execute(f"UPDATE videos SET file_id=?, status='ready', progress=1, error='', {keys}, updated=datetime('now') WHERE post_id=?",
                   (new_id, *cols.values(), post_id))
        db.commit()
    _remove_folder(row["file_id"])
    rungs = ", ".join(f"{r['p']}p{round(r['fps']) if r['fps'] > 30.5 else ''}" for r in json.loads(cols["rungs"]))
    log(f"video {post_id}: rebuilt in {time.monotonic() - started:.0f}s ({rungs}{', Original' if cols['original'] else ''})")
    return True


def _exists(post_id: int) -> bool:
    with get_db() as db:
        return db.execute("SELECT 1 FROM videos WHERE post_id=?", (post_id,)).fetchone() is not None


def _waiting() -> list[int]:
    with get_db() as db:
        return [r[0] for r in db.execute("SELECT post_id FROM videos WHERE status='processing' ORDER BY post_id")]


def _work() -> None:
    while True:
        try:
            post_id = _jobs.get(timeout=_POLL)
        except queue.Empty:
            # Videos queued by another process (the importer writes rows and
            # leaves the making to this one).
            try:
                for pid in _waiting():
                    _enqueue(pid)
            except Exception as e:  # noqa: BLE001
                print(f"videos: couldn't look for queued videos: {e}")
            try:
                sweep_uploads()
            except Exception as e:  # noqa: BLE001
                print(f"videos: couldn't sweep old uploads: {e}")
            continue
        try:
            _transcode(post_id)
        finally:
            with _worker_lock:
                _queued.discard(post_id)
            _jobs.task_done()


def _start_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, name="video-transcode", daemon=True)
            _worker.start()


def _enqueue(post_id: int) -> None:
    _start_worker()
    with _worker_lock:
        if post_id in _queued:
            return
        _queued.add(post_id)
    _jobs.put(post_id)


def resume_jobs() -> None:
    """At startup: videos a restart interrupted (or that were still queued),
    and the worker that keeps an eye out for more."""
    if not shutil.which("ffmpeg"):
        print("videos: ffmpeg isn't installed; uploads will wait")
        return
    for post_id in _waiting():
        _enqueue(post_id)
    _start_worker()


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
    original = None
    # Rows that don't select it (other routers' own queries) just have none.
    if ready and "original" in r.keys() and r["original"]:
        try:
            original = {"url": f"{base}/original.mp4", **json.loads(r["original"])}
        except ValueError:
            original = None
    return {
        "hls": f"{base}/master.m3u8" if ready else None,
        "original": original,
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
    clipped = _clipped_from(db, rows)
    return [{
        "id": r["post_id"], "title": r["title"], "description": r["text"], "username": r["username"],
        "created": r["created"], "published": r["published"] or r["created"], "duration": r["duration"], "width": r["width"], "height": r["height"],
        "status": r["status"], "progress": r["progress"], "error": r["error"] or None,
        **_urls(r),
        "views": r["views"], "reactions": reactions.get(r["post_id"], {}), "reply_count": replies.get(r["post_id"], 0),
        "watched": watched.get(r["post_id"]),
        "canEdit": admin or r["username"] == username,
        **({"unlisted": True} if r["unlisted"] else {}),
        **({"kind": "clip", "game": r["game"] or "", "clipOf": clipped.get(r["post_id"])} if r["kind"] == "clip" else {}),
    } for r in rows]


def _clipped_from(db, rows) -> dict:
    """A clip made from a range of a video: that video ({id, title, start,
    end}, title None once it's gone), by the clip's id."""
    made = [r for r in rows if r["kind"] == "clip" and r["clip_of"]]
    if not made:
        return {}
    ids = sorted({r["clip_of"] for r in made})
    marks = ",".join("?" * len(ids))
    titles = {t["post_id"]: t["title"] for t in db.execute(f"SELECT post_id, title FROM videos WHERE post_id IN ({marks})", ids)}
    return {r["post_id"]: {"id": r["clip_of"], "title": titles.get(r["clip_of"]), "start": r["clip_start"], "end": r["clip_end"]}
            for r in made}


def _one(db, post_id: int, request: Request, username: str) -> dict | None:
    r = db.execute(f"SELECT {_COLS} FROM videos v JOIN posts p ON p.id = v.post_id WHERE v.post_id=?", (post_id,)).fetchone()
    return _json_many(db, [r], request, username)[0] if r else None


def card(db, post_id: int) -> dict | None:
    """A video as a feed post shows it (routers/posts.py), or None if it's gone."""
    r = db.execute("""SELECT v.post_id, v.title, v.file_id, v.status, v.poster, v.storyboard, v.original, v.duration,
                             v.published, p.username
                      FROM videos v JOIN posts p ON p.id = v.post_id WHERE v.post_id=?""", (post_id,)).fetchone()
    if r is None:
        return None
    u = _urls(r)
    return {"id": r["post_id"], "title": r["title"], "poster": u["poster"], "posterSmall": u["posterSmall"],
            "duration": r["duration"], "username": r["username"], "status": r["status"], "published": r["published"]}


# ── Routes ──

def _clock(s: float) -> str:
    s = int(round(s or 0))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


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


def create(request: Request | None, username: str, file, title: str, description: str, unlisted: bool = False,
           published: str | None = None, source_id: str = "", thumb: str | None = None,
           queue_it: bool = True, kind: str = "", game: str = "", saved: tuple[str, str, int] | None = None) -> int | JSONResponse:
    """An upload made into a video and queued for ffmpeg: its post id. An
    unlisted one (uploaded straight into a TV channel, routers/tv.py) is
    left out of Videos' lists. file is an UploadFile, or anything with
    .filename and .file (the importer's own files). published: when it
    came out (parse_published; blank is now). source_id: the YouTube id it
    was imported from. thumb: a picture file to make the poster from. With
    queue_it False (the importer, another process) the row is left
    processing for this server's worker to find. kind 'clip': a clip
    (routers/clips.py), up to CLIP_MAX_SECONDS long, with its game.
    saved: (file_id, ext, size) of an upload already in its folder as
    source.<ext> (a chunked upload, finish_upload); file is then only its
    name. Anything that fails removes that folder."""
    def refuse(status: int, message: str) -> JSONResponse:
        if saved:
            _remove_folder(saved[0])
        return _error(status, message)
    try:
        published = parse_published(published)
    except ValueError as e:
        return refuse(400, str(e))
    if not media_ready():
        return refuse(503, "storage is offline")
    if not shutil.which("ffmpeg"):
        return refuse(503, "this server can't make videos yet (no ffmpeg)")
    if len(description.strip()) > VIDEO_DESC_MAX_CHARS:
        return refuse(400, f"a description can be up to {VIDEO_DESC_MAX_CHARS:,} characters")
    title = " ".join(title.split())[:VIDEO_TITLE_MAX_CHARS] or os.path.splitext(file.filename or "")[0][:VIDEO_TITLE_MAX_CHARS].strip() or "Untitled"
    if saved is None:
        saved = _save_upload(file)
        if isinstance(saved, JSONResponse):
            return saved
    file_id, ext, size = saved
    info = probe(os.path.join(_folder(file_id), f"source.{ext}"))
    if info is None:
        _remove_folder(file_id)
        return _error(400, "couldn't find a video in that file")
    kind = "clip" if kind == "clip" else ""
    if kind and (info["duration"] or 0) > CLIP_MAX_SECONDS + 0.5:
        _remove_folder(file_id)
        return _error(400, f"a clip can be up to {_clock(CLIP_MAX_SECONDS)} long, and this is {_clock(info['duration'])}. Upload it as a video instead")
    if thumb:
        t_ext = os.path.splitext(thumb)[1][1:].lower()
        if t_ext in ("jpg", "jpeg", "png", "webp"):
            try:
                shutil.copyfile(thumb, os.path.join(_folder(file_id), f"thumb.{t_ext}"))
            except OSError as e:
                print(f"videos: couldn't keep the thumbnail {thumb}: {e}")
    try:
        with get_db() as db:
            from .clips import game_name
            game = game_name(db, game) if kind else ""
            cur = db.execute("INSERT INTO posts(username, text, kind) VALUES (?, ?, 'video')", (username, description.strip()))
            post_id = cur.lastrowid
            db.execute("""INSERT INTO videos(post_id, title, file_id, ext, duration, width, height, size, unlisted, published, source_id, kind, game)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')), ?, ?, ?)""",
                       (post_id, title, file_id, ext, info["duration"], info["width"], info["height"], size, 1 if unlisted else 0,
                        published, source_id, kind, game))
            db.commit()
    except Exception:
        # A row that couldn't be written (the database busy past its
        # timeout): no video, so no folder either.
        _remove_folder(file_id)
        raise
    if queue_it:
        _enqueue(post_id)
    return post_id


@router.post(BASE)
def upload(request: Request, file: UploadFile = File(...), title: str = Form(""), description: str = Form(""),
           published: str = Form(""), kind: str = Form(""), game: str = Form("")):
    """kind=clip (and a game) uploads a clip (routers/clips.py)."""
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    post_id = create(request, username, file, title, description, published=published, kind=kind, game=game)
    if isinstance(post_id, JSONResponse):
        return post_id
    with get_db() as db:
        return _one(db, post_id, request, username)


# ── Chunked uploads ──
# A big file goes up in parts (PUT, up to _PART_MAX each, in order) into a
# session on the media share, then finish_upload makes it a video like the
# single POST above. Nothing passes through the server's temporary folder
# (the single POST's multipart body is spooled whole into /tmp before the
# route even runs: a 13GB file filled the container's disk and was then
# refused at VIDEO_MAX_MB anyway), each request is small enough for any
# proxy's body limit, and a part that fails is sent again from where the
# server says it got to, so a dropped link or a restart only costs a part.
# Sessions not touched for _UPLOAD_TTL are swept (sweep_uploads).
_UPLOADS = os.path.join(MEDIA_DIR, "videos", ".uploads")  # never a 2-letter file-id folder
_UPLOAD_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,40}")
_PART_MAX = 96 * 1024 * 1024
_UPLOAD_TTL = 2 * 86400
_UPLOADS_PER_USER = 8
_upload_locks: dict[str, threading.Lock] = {}
_upload_locks_lock = threading.Lock()
_swept = 0.0


def _session_dir(sid: str) -> str:
    return os.path.join(_UPLOADS, sid)


def _session(sid: str, username: str):
    """(meta, data path) of the caller's own session, or an error response."""
    if not _UPLOAD_ID_RE.fullmatch(sid or ""):
        return None, None, _error(404, "that upload isn't there any more")
    try:
        with open(os.path.join(_session_dir(sid), "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None, None, _error(404, "that upload isn't there any more")
    if meta.get("user") != username:
        return None, None, _error(404, "that upload isn't there any more")
    return meta, os.path.join(_session_dir(sid), "data"), None


def _received(data: str) -> int:
    try:
        return os.path.getsize(data)
    except OSError:
        return 0


def _lock_for(sid: str) -> threading.Lock:
    with _upload_locks_lock:
        return _upload_locks.setdefault(sid, threading.Lock())


def sweep_uploads(force: bool = False) -> None:
    """Drops chunked uploads nobody has added to for _UPLOAD_TTL (at most
    hourly; the worker calls it while idle, and at startup)."""
    global _swept
    now = time.time()
    if not force and now - _swept < 3600:
        return
    _swept = now
    try:
        names = os.listdir(_UPLOADS)
    except OSError:
        return
    for sid in names:
        d = _session_dir(sid)
        try:
            last = max(os.path.getmtime(os.path.join(d, n)) for n in os.listdir(d)) if os.listdir(d) else os.path.getmtime(d)
        except (OSError, ValueError):
            continue
        if now - last > _UPLOAD_TTL:
            shutil.rmtree(d, ignore_errors=True)
            with _upload_locks_lock:
                _upload_locks.pop(sid, None)


def _fields(body: dict) -> dict:
    return {k: str(body.get(k) or "") for k in ("title", "description", "published", "kind", "game")}


@router.post(f"{BASE}/uploads")
def start_upload(request: Request, body: dict):
    """{name, size, title?, description?, published?, kind?, game?}: a new
    chunked upload, checked before a byte is sent. {id, received: 0, part}."""
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    name = str(body.get("name") or "")
    ext = (os.path.splitext(name)[1][1:] or "").lower()
    if ext not in _EXTS:
        return _error(400, "a video is an mp4, mov, mkv, webm, avi or m4v file")
    try:
        size = int(body.get("size"))
    except (TypeError, ValueError):
        return _error(400, "size (bytes) required")
    if size <= 0:
        return _error(400, "that file is empty")
    if size > _MAX_BYTES:
        limit = f"{VIDEO_MAX_MB / 1024:.0f}GB" if VIDEO_MAX_MB >= 1024 else f"{VIDEO_MAX_MB}MB"
        return _error(413, f"a video can be up to {limit} here, and this is {size / 1024 ** 3:.1f}GB")
    fields = _fields(body)
    try:
        parse_published(fields["published"])
    except ValueError as e:
        return _error(400, str(e))
    if len(fields["description"].strip()) > VIDEO_DESC_MAX_CHARS:
        return _error(400, f"a description can be up to {VIDEO_DESC_MAX_CHARS:,} characters")
    if not media_ready():
        return _error(503, "storage is offline")
    if not shutil.which("ffmpeg"):
        return _error(503, "this server can't make videos yet (no ffmpeg)")
    sweep_uploads()
    os.makedirs(_UPLOADS, exist_ok=True)
    # Room for it, and for the ladder it becomes (about a third again).
    free = shutil.disk_usage(_UPLOADS).free
    if free < size * 1.4 + 512 * 1024 * 1024:
        return _error(507, f"the server's disk is too full for this ({free / 1024 ** 3:.1f}GB free)")
    mine = 0
    for other in os.listdir(_UPLOADS):
        try:
            with open(os.path.join(_session_dir(other), "meta.json"), encoding="utf-8") as f:
                mine += json.load(f).get("user") == username
        except (OSError, ValueError):
            pass
    if mine >= _UPLOADS_PER_USER:
        return _error(429, "you've too many uploads going at once; finish or stop one first")
    sid = secrets.token_urlsafe(18)
    d = _session_dir(sid)
    os.makedirs(d)
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"user": username, "name": name[:255], "ext": ext, "size": size, "created": time.time(), **fields}, f)
    open(os.path.join(d, "data"), "wb").close()
    return {"id": sid, "received": 0, "size": size, "part": _PART_MAX}


@router.get(f"{BASE}/uploads/{{sid}}")
def upload_status(sid: str, request: Request):
    """How much of it the server has: where to carry on from."""
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    meta, data, err = _session(sid, username)
    if err:
        return err
    return {"id": sid, "received": _received(data), "size": meta["size"], "part": _PART_MAX}


@router.put(f"{BASE}/uploads/{{sid}}")
async def upload_part(sid: str, request: Request, offset: int = -1):
    """A part, as the raw body, written at ?offset= (which must be what the
    server has, or less: a part sent again after its answer was lost)."""
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import ClientDisconnect
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    meta, data, err = _session(sid, username)
    if err:
        return err
    try:
        length = int(request.headers.get("content-length") or -1)
    except ValueError:
        length = -1
    if length > _PART_MAX:
        return _error(413, f"a part can be up to {_PART_MAX // 1048576}MB")
    lock = _lock_for(sid)
    if not lock.acquire(blocking=False):
        return _error(409, "that upload is already being sent")
    try:
        have = _received(data)
        if offset < 0 or offset > have:
            return JSONResponse(status_code=409, content={"error": "out of step", "received": have})
        f = await run_in_threadpool(open, data, "r+b")
        try:
            if offset < have:
                await run_in_threadpool(f.truncate, offset)
            f.seek(offset)
            got, buf = 0, []
            pending = 0
            try:
                async for chunk in request.stream():
                    got += len(chunk)
                    if got > _PART_MAX or offset + got > meta["size"]:
                        await run_in_threadpool(f.truncate, offset)
                        return _error(413, "that's more than the file's size" if got <= _PART_MAX else
                                      f"a part can be up to {_PART_MAX // 1048576}MB")
                    buf.append(chunk)
                    pending += len(chunk)
                    if pending >= 1048576:
                        await run_in_threadpool(f.write, b"".join(buf))
                        buf, pending = [], 0
                if buf:
                    await run_in_threadpool(f.write, b"".join(buf))
            except ClientDisconnect:
                # What arrived is kept; the page asks where it got to.
                pass
            await run_in_threadpool(f.flush)
        finally:
            await run_in_threadpool(f.close)
        return {"received": _received(data), "size": meta["size"]}
    except OSError as e:
        return _error(507 if getattr(e, "errno", 0) == 28 else 500,
                      "the server's disk is full" if getattr(e, "errno", 0) == 28 else f"couldn't save that part: {e}")
    finally:
        lock.release()


def take_upload(sid: str, username: str):
    """A finished chunked upload moved into a new video folder as
    source.<ext>: ((file_id, ext, size), its meta), or (None, an error).
    For finish_upload and routers/tv.py's upload into a channel."""
    meta, data, err = _session(sid, username)
    if err:
        return None, err
    lock = _lock_for(sid)
    if not lock.acquire(blocking=False):
        return None, _error(409, "that upload is still being sent")
    try:
        have = _received(data)
        if have != meta["size"]:
            return None, JSONResponse(status_code=409, content={"error": "it isn't all there yet", "received": have})
        file_id = secrets.token_urlsafe(12)
        folder = _folder(file_id)
        os.makedirs(folder, exist_ok=True)
        try:
            os.replace(data, os.path.join(folder, f"source.{meta['ext']}"))  # the same share: a rename
        except OSError:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        shutil.rmtree(_session_dir(sid), ignore_errors=True)
        return (file_id, meta["ext"], have), meta
    finally:
        lock.release()
        with _upload_locks_lock:
            _upload_locks.pop(sid, None)


@router.post(f"{BASE}/uploads/{{sid}}/done")
def finish_upload(sid: str, request: Request, body: dict | None = None):
    """All of it's there: made into a video (the single POST's answer).
    body may carry the fields again (title… as at the start), which win."""
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    saved, meta = take_upload(sid, username)
    if saved is None:
        return meta
    body = body or {}
    fields = {k: (str(body[k] or "") if body.get(k) is not None else str(meta.get(k) or ""))
              for k in ("title", "description", "published", "kind", "game")}
    post_id = create(request, username, SimpleNamespace(filename=meta.get("name") or f"video.{saved[1]}"),
                     fields["title"], fields["description"], published=fields["published"],
                     kind=fields["kind"], game=fields["game"], saved=saved)
    if isinstance(post_id, JSONResponse):
        return post_id
    with get_db() as db:
        return _one(db, post_id, request, username)


@router.delete(f"{BASE}/uploads/{{sid}}")
def cancel_upload(sid: str, request: Request):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    meta, data, err = _session(sid, username)
    if err:
        return {"ok": True}
    shutil.rmtree(_session_dir(sid), ignore_errors=True)
    with _upload_locks_lock:
        _upload_locks.pop(sid, None)
    return {"ok": True}


@router.get(BASE)
def list_videos(request: Request, limit: int = 24, before_id: int | None = None, offset: int = 0,
                user: str = "", sort: str = "new", q: str = "", kind: str = ""):
    """Newest first by when each came out (published; before_id pages back
    from that video), oldest first (sort=old, before_id pages on), or the
    most watched (offset pages). Other people's only once they're ready;
    yours always, so you can see yours being made. Videos only: kind=clip
    is clips instead (search's filter), kind=all both."""
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    limit = max(1, min(limit, 100))
    where, args = ["(v.status='ready' OR p.username=?)", "v.unlisted=0"], [username]
    if kind != "all":
        where.append("v.kind=?"); args.append("clip" if kind == "clip" else "")
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
            old = sort == "old"
            page, more = cond, []
            if before_id is not None:
                at = db.execute("SELECT published FROM videos WHERE post_id=?", (before_id,)).fetchone()
                if at is not None:
                    page += f" AND (v.published, v.post_id) {'>' if old else '<'} (?, ?)"
                    more = [at["published"], before_id]
                elif not old:
                    page += " AND v.post_id < ?"
                    more = [before_id]
            way = "ASC" if old else "DESC"
            rows = db.execute(f"""SELECT {_COLS} FROM videos v JOIN posts p ON p.id = v.post_id WHERE {page}
                                  ORDER BY v.published {way}, v.post_id {way} LIMIT ? OFFSET ?""",
                              (*args, *more, limit, max(0, offset) if before_id is None else 0)).fetchall()
        return {"videos": _json_many(db, rows, request, username), "total": total}


@router.get(f"{BASE}/continue")
def continue_watching(request: Request):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    with get_db() as db:
        rows = db.execute(f"""SELECT {_COLS} FROM video_progress g JOIN videos v ON v.post_id = g.post_id
                              JOIN posts p ON p.id = v.post_id
                              WHERE g.username=? AND v.status='ready' AND v.unlisted=0 AND v.kind='' AND g.position > 5
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
        if "published" in body:
            try:
                published = parse_published(body["published"])
            except ValueError as e:
                return _error(400, str(e))
            if published:
                db.execute("UPDATE videos SET published=?, updated=datetime('now') WHERE post_id=?", (published, post_id))
        if "game" in body and row["kind"] == "clip":
            from .clips import game_name
            db.execute("UPDATE videos SET game=?, updated=datetime('now') WHERE post_id=?", (game_name(db, body["game"]), post_id))
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
    _poster_files(img, folder, stem)
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
    db.execute("DELETE FROM video_playlist_items WHERE video_id=?", (post_id,))
    from .studio import delete_for
    delete_for(post_id, db)
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
