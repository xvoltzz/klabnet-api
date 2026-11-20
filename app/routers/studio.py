"""The video studio: a light editor, annotations, cards (polls among them)
and end screens, for a video's owner (or an admin). klabnet-web's
js/43-studio.js is the other end.

The editor is non-destructive. An edit is a list (videos.edit, JSON) in the
time of the kept upload, never of an earlier render:

    {"keep": [[a, b], …], "mute": [[a, b], …], "muteAll": false,
     "gain": 0.0, "crop": null | "16:9" | "9:16" | "1:1", "rotate": 0 | 90 | 180 | 270}

keep is what stays, in order (trimming the ends and cutting out the middle
both come down to it). Saving queues a render from source.<ext>: one pass
through ffmpeg's trim/atrim + concat (and volume, crop, rotate) into a
high-quality intermediate at the source's size and frame rate (x264 CRF 14;
an HDR source stays HDR in HEVC 10-bit when this ffmpeg has libx265), which
becomes the video's Original ("Original (edited)") and from which the usual
ladder, posters and storyboard are made. It's built in a new folder under a
new file id while the old one keeps playing, then swapped in (videos.rebuild's
way). The unedited ladder (and the raw remux) ride along in the new folder's
base/, so the studio can always preview the whole upload. Reverting is a
render with no edit: videos.build() from the upload, as at first.

Annotations, cards and the end screen are in the edited timeline; the site
moves them when the cuts change (and drops what falls in a removed part).
Their GETs are for anyone signed in, with the video; PUT replaces all of a
kind (owner/admin). Under /api/posts/videos because Caddy only forwards a
fixed list of /api prefixes."""
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

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from ..config import VIDEO_PRESET
from ..db import get_db
from ..media import media_ready
from . import videos as V

router = APIRouter()
BASE = V.BASE

_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,24}")
_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?")
_STUDIO_FILES = re.compile(
    r"studio-wave\.png|base/(?:master\.m3u8|v\d{1,2}/(?:index\.m3u8|init(?:_\d{1,2})?\.mp4|seg\d{1,6}\.m4s)"
    r"|storyboard\.jpg|original\.mp4|studio-wave\.png)")
_TYPES = {"m3u8": "application/vnd.apple.mpegurl", "m4s": "video/iso.segment", "mp4": "video/mp4",
          "jpg": "image/jpeg", "png": "image/png"}
INTERMEDIATE_CRF = 14
MAX_KEEPS = 200
ANN_TYPES = ("note", "bubble", "title", "spotlight", "label")
CARD_KINDS = ("video", "playlist", "channel", "tv", "link", "poll")
END_KINDS = ("video", "latest", "best", "playlist", "channel", "tv", "link")
LINK_KINDS = ("time", "video", "playlist", "channel", "tv", "url")


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _f(v, lo: float, hi: float, default: float | None = None) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(x):
        return default
    return min(hi, max(lo, x))


def _row(db, post_id: int):
    return db.execute("SELECT v.*, p.username FROM videos v JOIN posts p ON p.id = v.post_id WHERE v.post_id=?",
                      (post_id,)).fetchone()


def _owner(post_id: int, request: Request):
    """(username, row) for the video's owner or an admin, or an error."""
    username = V._signed_in(request)
    if not username:
        return None, None, _error(401, "not authenticated")
    with get_db() as db:
        row = _row(db, post_id)
    if row is None:
        return None, None, _error(404, "not found")
    if not V._can_edit(row["username"], request, username):
        return None, None, _error(403, "not your video")
    return username, row, None


async def _body(request: Request):
    try:
        body = await request.json()
    except Exception:
        return None
    return body


def _loads(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


# ── The edit list ──

def _ranges(raw, duration: float, limit: int) -> list[list[float]]:
    out = []
    for r in (raw or [])[:limit * 2]:
        if not isinstance(r, (list, tuple)) or len(r) != 2:
            continue
        a, b = _f(r[0], 0, duration), _f(r[1], 0, duration)
        if a is None or b is None or b - a < 0.05:
            continue
        out.append([round(a, 3), round(b, 3)])
    out.sort()
    merged: list[list[float]] = []
    for a, b in out:
        if merged and a <= merged[-1][1] + 0.01:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged[:limit]


def clean_edit(raw, duration: float) -> dict | None:
    """The edit as stored, or None when it changes nothing. Raises
    ValueError with what's wrong."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or not duration:
        raise ValueError("an edit is an object")
    keep = _ranges(raw.get("keep") if raw.get("keep") is not None else [[0, duration]], duration, MAX_KEEPS)
    keep = [k for k in keep if k[1] - k[0] >= 0.1]
    if not keep:
        raise ValueError("that cuts out the whole video")
    if sum(b - a for a, b in keep) < 1:
        raise ValueError("an edited video has to be at least a second long")
    mute = _ranges(raw.get("mute"), duration, MAX_KEEPS)
    gain = round(_f(raw.get("gain"), -20, 20, 0) or 0, 1)
    crop = raw.get("crop") if raw.get("crop") in ("16:9", "9:16", "1:1") else None
    try:
        rotate = int(raw.get("rotate") or 0) % 360
    except (TypeError, ValueError):
        rotate = 0
    if rotate not in (0, 90, 180, 270):
        rotate = 0
    edit = {"keep": keep, "mute": mute, "muteAll": bool(raw.get("muteAll")), "gain": gain, "crop": crop, "rotate": rotate}
    whole = len(keep) == 1 and keep[0][0] <= 0.05 and keep[0][1] >= duration - 0.05
    if whole and not mute and not edit["muteAll"] and not gain and not crop and not rotate:
        return None
    return edit


def edited_length(edit: dict | None, duration: float) -> float:
    if not edit:
        return duration
    return sum(b - a for a, b in edit["keep"])


def _even(n: float) -> int:
    return max(2, int(n) // 2 * 2)


def _color_info(path: str) -> dict:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=color_transfer,color_primaries,color_space", "-of", "json", path],
                             capture_output=True, timeout=60, check=True).stdout
        s = (json.loads(out or b"{}").get("streams") or [{}])[0]
    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        s = {}
    return {"trc": s.get("color_transfer") or "", "prim": s.get("color_primaries") or "", "space": s.get("color_space") or ""}


_x265: bool | None = None


def _has_x265() -> bool:
    global _x265
    if _x265 is None:
        try:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30).stdout
            _x265 = bool(re.search(r"\slibx265\s", out))
        except (subprocess.SubprocessError, OSError):
            _x265 = False
    return _x265


def intermediate_cmd(src: str, dest: str, edit: dict, info: dict) -> tuple[list[str], bool]:
    """ffmpeg for the edit, from the upload to one high-quality mp4 (the
    Original (edited)), and whether it kept HDR."""
    rate, _ = V._out_fps(info)
    keeps = edit["keep"]
    n = len(keeps)
    w, h = info["width"], info["height"]
    pre = []
    rot = edit.get("rotate") or 0
    if rot == 90:
        pre.append("transpose=1")
    elif rot == 270:
        pre.append("transpose=2")
    elif rot == 180:
        pre += ["hflip", "vflip"]
    if rot in (90, 270):
        w, h = h, w
    crop = edit.get("crop")
    if crop:
        cw, ch = {"16:9": (16, 9), "9:16": (9, 16), "1:1": (1, 1)}[crop]
        r = cw / ch
        if w / h > r:
            tw, th = _even(h * r), _even(h)
        else:
            tw, th = _even(w), _even(w / r)
        pre.append(f"crop={tw}:{th}")
        w, h = tw, th
    hdr = bool(info.get("hdr"))
    keep_hdr = hdr and _has_x265()
    post = [f"fps={rate}"]
    if hdr and not keep_hdr and V._has_zscale():
        post += ["zscale=t=linear:npl=100", "format=gbrpf32le", "zscale=p=bt709",
                 "tonemap=tonemap=hable:desat=0", "zscale=t=bt709:m=bt709:r=tv"]
    post.append("format=yuv420p10le" if keep_hdr else "format=yuv420p")
    chains = [f"[0:v]{','.join(pre + ['null'])},split={n}" + "".join(f"[vs{i}]" for i in range(n))]
    chains += [f"[vs{i}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[vk{i}]" for i, (a, b) in enumerate(keeps)]
    audio = info["audio"] and not edit.get("muteAll")
    if audio:
        apre = ["aresample=48000"]
        if edit.get("mute"):
            enable = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in edit["mute"])
            apre.append(f"volume=volume=0:enable='{enable}'")
        if edit.get("gain"):
            apre.append(f"volume={edit['gain']:+.1f}dB")
        chains.append(f"[0:a:0]{','.join(apre)},asplit={n}" + "".join(f"[as{i}]" for i in range(n)))
        chains += [f"[as{i}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[ak{i}]" for i, (a, b) in enumerate(keeps)]
        chains.append("".join(f"[vk{i}][ak{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
    else:
        chains.append("".join(f"[vk{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vc]")
    chains.append(f"[vc]{','.join(post)}[vout]")
    cmd = ["ffmpeg", "-hide_banner", "-y", "-i", src, "-filter_complex", ";".join(chains), "-map", "[vout]"]
    if audio:
        cmd += ["-map", "[ac]"]
    if keep_hdr:
        c = _color_info(src)
        cmd += ["-c:v", "libx265", "-preset", VIDEO_PRESET, "-crf", "16", "-tag:v", "hvc1",
                "-color_primaries", c["prim"] or "bt2020", "-color_trc", c["trc"] or "smpte2084",
                "-colorspace", c["space"] or "bt2020nc", "-x265-params", "repeat-headers=1:log-level=error"]
    else:
        cmd += ["-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", str(INTERMEDIATE_CRF), "-profile:v", "high"]
        if hdr:
            cmd += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "256k", "-ac", "2", "-ar", "48000"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", "-map_metadata", "-1", "-f", "mp4", dest]
    return cmd, keep_hdr


def render_edit(folder: str, ext: str, edit: dict, report=None) -> dict:
    """The edit made in a folder holding source.<ext>: original.mp4 (the
    high-quality render), the ladder from it, posters and a storyboard.
    Returns the video row's new columns; raises on failure."""
    src = os.path.join(folder, f"source.{ext}")
    info = V.probe(src)
    if info is None:
        raise RuntimeError("couldn't find a video in the upload")
    length = edited_length(edit, info["duration"] or 0)
    for name in os.listdir(folder):
        if not V._keep(name, ext) and not name.startswith("poster") and name != "base":
            p = os.path.join(folder, name)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    inter = os.path.join(folder, "original.mp4")
    tmp = inter + ".partial.mp4"
    cmd, kept_hdr = intermediate_cmd(src, tmp, edit, info)
    if report:
        report(0.0)
    V._run(cmd, length, 0.0, 0.45, report)
    os.replace(tmp, inter)
    made = V.probe(inter)
    if made is None:
        raise RuntimeError("the render came out empty")
    rungs = V._rungs(made["width"], made["height"], V._out_fps(made)[1])
    for i in range(len(rungs)):
        os.makedirs(os.path.join(folder, f"v{i}"), exist_ok=True)
    V._run(V._ladder_cmd(inter, folder, rungs, made), made["duration"], 0.45, 0.9, report)
    V._fix_master(folder, rungs)
    board = V._stills(inter, folder, made["duration"], made["width"], made["height"], report)
    size = os.path.getsize(inter)
    original = {"width": made["width"], "height": made["height"], "fps": made["fps"],
                "codec": made["vcodec"], "bitrate": made["bitrate"] or (round(size * 8 / made["duration"]) if made["duration"] else 0),
                "size": size, "hdr": kept_hdr, "edited": True}
    return {"width": made["width"], "height": made["height"], "duration": made["duration"],
            "rungs": json.dumps(rungs), "storyboard": json.dumps(board) if board else "",
            "original": json.dumps(original)}


def _link_tree(src: str, dest: str) -> None:
    """Hard links (copies where the share won't) of a folder's files."""
    for root, _dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        out = dest if rel == "." else os.path.join(dest, rel)
        os.makedirs(out, exist_ok=True)
        for name in files:
            if name.endswith(".partial") or ".partial." in name:
                continue
            a, b = os.path.join(root, name), os.path.join(out, name)
            try:
                os.link(a, b)
            except OSError:
                shutil.copy2(a, b)


def _carry_base(row, old: str, new: str) -> dict:
    """The unedited ladder into new/base/ (from the old folder's own files
    when it wasn't edited, else from its base/), and what it is."""
    base = os.path.join(new, "base")
    os.makedirs(base, exist_ok=True)
    if row["edit_live"]:
        if os.path.isdir(os.path.join(old, "base")):
            _link_tree(os.path.join(old, "base"), base)
        return _loads(row["base"], {}) or {}
    for name in os.listdir(old):
        p = os.path.join(old, name)
        if re.fullmatch(r"v\d{1,2}", name) and os.path.isdir(p):
            _link_tree(p, os.path.join(base, name))
        elif name in ("master.m3u8", "storyboard.jpg", "original.mp4", "studio-wave.png"):
            try:
                os.link(p, os.path.join(base, name))
            except OSError:
                shutil.copy2(p, os.path.join(base, name))
    return {"duration": row["duration"], "width": row["width"], "height": row["height"],
            "rungs": _loads(row["rungs"], []), "storyboard": _loads(row["storyboard"], None),
            "original": _loads(row["original"], None)}


def _set(post_id: int, **cols) -> None:
    keys = ", ".join(f"{k}=?" for k in cols)
    with get_db() as db:
        db.execute(f"UPDATE videos SET {keys} WHERE post_id=?", (*cols.values(), post_id))
        db.commit()


def make(post_id: int, log=print) -> bool:
    """Renders the video's wanted edit (videos.edit; none: the upload as it
    came) in a new folder, then swaps it in. The old one plays meanwhile."""
    with get_db() as db:
        row = db.execute("SELECT * FROM videos WHERE post_id=?", (post_id,)).fetchone()
    if row is None or row["status"] != "ready":
        return False
    want = row["edit"] or ""
    if want == (row["edit_live"] or "") and not row["render"]:
        return False  # nothing to make (a change undone before it started)
    old = V._folder(row["file_id"])
    ext = row["ext"]
    if not os.path.isfile(os.path.join(old, f"source.{ext}")):
        _set(post_id, render="failed", render_error="the upload isn't there any more")
        return False
    _set(post_id, render="rendering", render_progress=0, render_error="")
    new_id = secrets.token_urlsafe(12)
    new = V._folder(new_id)
    os.makedirs(new, exist_ok=True)
    stem = row["poster"] or "poster"
    last = [0.0]

    def report(f):
        if f - last[0] >= 0.01 or f >= 1:
            last[0] = f
            _set(post_id, render_progress=f)
    started = time.monotonic()
    try:
        for name in os.listdir(old):
            if V._keep(name, ext) or (stem != "poster" and name.startswith(stem + "-")):
                try:
                    os.link(os.path.join(old, name), os.path.join(new, name))
                except OSError:
                    shutil.copy2(os.path.join(old, name), os.path.join(new, name))
        edit = _loads(want)
        cols = render_edit(new, ext, edit, report) if edit else V.build(post_id, new, ext, report)
        if stem != "poster":
            for size in ("l", "s"):
                p = os.path.join(old, f"{stem}-{size}.jpg")
                if os.path.isfile(p) and not os.path.isfile(os.path.join(new, f"{stem}-{size}.jpg")):
                    shutil.copy2(p, os.path.join(new, f"{stem}-{size}.jpg"))
        base = _carry_base(row, old, new) if edit else None
    except Exception as e:  # noqa: BLE001 — a failed render leaves the video as it was
        shutil.rmtree(new, ignore_errors=True)
        msg = str(e) or e.__class__.__name__
        log(f"video {post_id}: the edit didn't render: {msg}")
        _set(post_id, render="failed", render_error=msg[:300])
        return False
    with get_db() as db:
        cur = db.execute("SELECT file_id, edit FROM videos WHERE post_id=?", (post_id,)).fetchone()
        if cur is None or cur["file_id"] != row["file_id"]:
            db.rollback()
            shutil.rmtree(new, ignore_errors=True)
            log(f"video {post_id}: changed while its edit was rendering; left as it was")
            return False
        again = (cur["edit"] or "") != want
        keys = ", ".join(f"{k}=?" for k in cols)
        db.execute(f"""UPDATE videos SET file_id=?, {keys}, edit_live=?, base=?, render=?, render_progress=0,
                       render_error='', updated=datetime('now') WHERE post_id=?""",
                   (new_id, *cols.values(), want, json.dumps(base) if base else "", "queued" if again else "", post_id))
        # Where people stopped, past the new end, is finished.
        if cols.get("duration"):
            db.execute("DELETE FROM video_progress WHERE post_id=? AND position >= ?", (post_id, cols["duration"] * V.DONE_AT))
        db.commit()
    V._remove_folder(row["file_id"])
    log(f"video {post_id}: {'edit' if want else 'original'} rendered in {time.monotonic() - started:.0f}s")
    return True


# ── One render at a time, on its own thread ──

_q: "queue.Queue[int]" = queue.Queue()
_queued: set[int] = set()
_lock = threading.Lock()
_worker: threading.Thread | None = None


def _work() -> None:
    while True:
        post_id = _q.get()
        try:
            make(post_id)
        except Exception as e:  # noqa: BLE001
            print(f"studio: video {post_id}: {e}")
        finally:
            with _lock:
                _queued.discard(post_id)
            _q.task_done()
            # Changed again while it rendered: once more, with the new edit.
            try:
                with get_db() as db:
                    r = db.execute("SELECT render FROM videos WHERE post_id=?", (post_id,)).fetchone()
                if r is not None and r["render"] == "queued":
                    enqueue(post_id)
            except Exception:  # noqa: BLE001
                pass


def enqueue(post_id: int) -> None:
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, name="video-studio", daemon=True)
            _worker.start()
        if post_id in _queued:
            return
        _queued.add(post_id)
    _q.put(post_id)


def resume_renders() -> None:
    """At startup: renders a restart cut short (main.py)."""
    if not shutil.which("ffmpeg"):
        return
    with get_db() as db:
        ids = [r[0] for r in db.execute("SELECT post_id FROM videos WHERE render IN ('queued', 'rendering')")]
    for i in ids:
        enqueue(i)


# ── The waveform (the studio's, made the first time it's asked for) ──

_waving: set[int] = set()


def _wave_path(row) -> str:
    folder = V._folder(row["file_id"])
    return os.path.join(folder, "base", "studio-wave.png") if row["edit_live"] else os.path.join(folder, "studio-wave.png")


def _ensure_wave(row, audio: bool = True) -> bool:
    """True when it's there; otherwise starts making it (if there's sound)."""
    path = _wave_path(row)
    if os.path.isfile(path):
        return True
    if not audio:
        return False
    post_id = row["post_id"]
    with _lock:
        if post_id in _waving:
            return False
        _waving.add(post_id)
    src = os.path.join(V._folder(row["file_id"]), f"source.{row['ext']}")

    def go():
        try:
            tmp = path + ".partial.png"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-filter_complex",
                            "aformat=channel_layouts=mono,showwavespic=s=3000x96:colors=white:scale=sqrt",
                            "-frames:v", "1", tmp], check=True, timeout=600, capture_output=True, preexec_fn=V._die_with_us)
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001 — no waveform, just the frames
            print(f"studio: no waveform for video {post_id}: {e}")
        finally:
            with _lock:
                _waving.discard(post_id)
    threading.Thread(target=go, name="studio-wave", daemon=True).start()
    return False


# ── Annotations, cards, the end screen: cleaning what comes in ──

def _text(v, n: int) -> str:
    return str(v or "").strip()[:n]


def _color(v) -> str:
    v = str(v or "").strip()
    return v if _COLOR_RE.fullmatch(v) else ""


def clean_link(raw) -> dict | None:
    if not isinstance(raw, dict) or raw.get("kind") not in LINK_KINDS:
        return None
    k = raw["kind"]
    if k == "time":
        t = _f(raw.get("t"), 0, 86400)
        return {"kind": k, "t": round(t, 2)} if t is not None else None
    if k == "video":
        try:
            vid = int(raw.get("id"))
        except (TypeError, ValueError):
            return None
        t = _f(raw.get("t"), 0, 86400, 0) or 0
        return {"kind": k, "id": vid, **({"t": round(t, 2)} if t else {})}
    if k == "playlist":
        pid = _text(raw.get("id"), 40)
        return {"kind": k, "id": pid} if re.fullmatch(r"[\w-]{4,40}", pid) else None
    if k == "channel":
        user = _text(raw.get("user"), 64).lower()
        return {"kind": k, "user": user} if re.fullmatch(r"[\w.@-]{1,64}", user) else None
    if k == "tv":
        cid = _text(raw.get("id"), 40)
        return {"kind": k, "id": cid} if re.fullmatch(r"[\w-]{1,40}", cid) else None
    url = _text(raw.get("url"), 2000)
    if not re.fullmatch(r"https?://[^\s<>\"']+", url):
        return None
    return {"kind": "url", "url": url, **({"title": _text(raw.get("title"), 80)} if raw.get("title") else {})}


def _new_id(raw, taken: set) -> str:
    i = str(raw or "")
    if not _ID_RE.fullmatch(i) or i in taken:
        i = secrets.token_hex(4)
    taken.add(i)
    return i


def clean_annotations(raw, duration: float) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("annotations are a list")
    out, taken = [], set()
    for a in raw[:100]:
        if not isinstance(a, dict) or a.get("type") not in ANN_TYPES:
            continue
        start = _f(a.get("start"), 0, duration or 86400, 0)
        end = _f(a.get("end"), 0, (duration or 86400) + 0.5, start + 5)
        if end - start < 0.2:
            end = min((duration or 86400) + 0.5, start + 0.2)
        w = _f(a.get("w"), 2, 100, 30)
        h = _f(a.get("h"), 2, 100, 15)
        x = _f(a.get("x"), 0, 100 - w, 10)
        y = _f(a.get("y"), 0, 100 - h, 10)
        st = a.get("style") if isinstance(a.get("style"), dict) else {}
        style = {"bg": _color(st.get("bg")), "fg": _color(st.get("fg")),
                 "size": st.get("size") if st.get("size") in ("sm", "md", "lg") else "md"}
        if a["type"] == "bubble":
            style["tx"] = round(_f(st.get("tx"), -50, 150, 20), 2)
            style["ty"] = round(_f(st.get("ty"), -50, 150, 140), 2)
        out.append({"id": _new_id(a.get("id"), taken), "type": a["type"], "start": round(start, 2), "end": round(end, 2),
                    "x": round(x, 2), "y": round(y, 2), "w": round(w, 2), "h": round(h, 2),
                    "text": _text(a.get("text"), 500), "style": style, "link": clean_link(a.get("link"))})
    out.sort(key=lambda a: (a["start"], a["id"]))
    return out


def clean_cards(raw, duration: float) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("cards are a list")
    out, taken = [], set()
    for c in raw[:20]:
        if not isinstance(c, dict) or c.get("kind") not in CARD_KINDS:
            continue
        at = _f(c.get("at"), 0, max(0, (duration or 86400) - 1), 0)
        card = {"id": _new_id(c.get("id"), taken), "kind": c["kind"], "at": round(at, 2), "teaser": _text(c.get("teaser"), 80)}
        if c["kind"] == "poll":
            p = c.get("poll") if isinstance(c.get("poll"), dict) else {}
            q = _text(p.get("question"), 140)
            opts = [_text(o, 60) for o in (p.get("options") or [])[:4] if _text(o, 60)]
            if not q or len(opts) < 2:
                raise ValueError("a poll needs a question and two to four answers")
            card["poll"] = {"question": q, "options": opts}
        else:
            link = clean_link(c.get("target"))
            want = {"video": "video", "playlist": "playlist", "channel": "channel", "tv": "tv", "link": "url"}[c["kind"]]
            if not link or link["kind"] != want:
                raise ValueError("a card needs something to point at")
            card["target"] = link
        out.append(card)
    out.sort(key=lambda c: (c["at"], c["id"]))
    return out


def clean_endscreen(raw, duration: float) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("an end screen is an object")
    start = _f(raw.get("start"), 5, 20, 10)
    if duration and duration < start + 1:
        raise ValueError(f"the video's too short for a {start:.0f}s end screen")
    els, taken = [], set()
    for e in (raw.get("elements") or [])[:4]:
        if not isinstance(e, dict) or e.get("kind") not in END_KINDS:
            continue
        w = _f(e.get("w"), 8, 60, 30)
        h = _f(e.get("h"), 8, 80, 30)
        el = {"id": _new_id(e.get("id"), taken), "kind": e["kind"],
              "x": round(_f(e.get("x"), 0, 100 - w, 5), 2), "y": round(_f(e.get("y"), 0, 100 - h, 5), 2),
              "w": round(w, 2), "h": round(h, 2)}
        if e["kind"] not in ("latest", "best"):
            link = clean_link(e.get("target"))
            want = {"video": "video", "playlist": "playlist", "channel": "channel", "tv": "tv", "link": "url"}[e["kind"]]
            if not link or link["kind"] != want:
                raise ValueError("each element needs something to point at")
            el["target"] = link
        els.append(el)
    if not els:
        return None
    return {"start": round(start, 1), "elements": els}


# ── Reading them back, with what they point at ──

def _annotations(db, post_id: int) -> list[dict]:
    rows = db.execute("SELECT * FROM video_annotations WHERE video_id=? ORDER BY start, id", (post_id,)).fetchall()
    return [{"id": r["id"], "type": r["type"], "start": r["start"], "end": r["end"], "x": r["x"], "y": r["y"],
             "w": r["w"], "h": r["h"], "text": r["text"], "style": _loads(r["style"], {}), "link": _loads(r["link"], None)}
            for r in rows]


def _cards(db, post_id: int, username: str, owner: bool) -> list[dict]:
    rows = db.execute("SELECT * FROM video_cards WHERE video_id=? ORDER BY at, id", (post_id,)).fetchall()
    votes = {}
    for v in db.execute("SELECT card_id, option, username FROM video_card_votes WHERE video_id=?", (post_id,)):
        votes.setdefault(v["card_id"], []).append((v["option"], v["username"]))
    out = []
    for r in rows:
        c = {"id": r["id"], "kind": r["kind"], "at": r["at"], "teaser": r["teaser"]}
        body = _loads(r["body"], {}) or {}
        if r["kind"] == "poll":
            poll = {"question": body.get("question", ""), "options": body.get("options", [])}
            mine = next((o for o, u in votes.get(r["id"], []) if u == username), None)
            poll["mine"] = mine
            if mine is not None or owner:
                counts = [0] * len(poll["options"])
                for o, _u in votes.get(r["id"], []):
                    if 0 <= o < len(counts):
                        counts[o] += 1
                poll["counts"] = counts
            c["poll"] = poll
        else:
            c["target"] = body.get("target")
        out.append(c)
    return out


def _endscreen(db, post_id: int) -> dict | None:
    r = db.execute("SELECT * FROM video_endscreen WHERE video_id=?", (post_id,)).fetchone()
    if r is None:
        return None
    els = _loads(r["elements"], []) or []
    return {"start": r["start_offset"], "elements": els} if els else None


def _expand(db, links: list[dict], owner: str, post_id: int, username: str) -> dict:
    """What links point at, for drawing them: {videos, playlists, tv, latest}."""
    vids = {l["id"] for l in links if l and l.get("kind") == "video"}
    pls = {l["id"] for l in links if l and l.get("kind") == "playlist"}
    tvs = {l["id"] for l in links if l and l.get("kind") == "tv"}
    out = {"videos": {}, "playlists": {}, "tv": {}, "latest": None}

    def vjson(r):
        u = V._urls(r)
        return {"id": r["post_id"], "title": r["title"], "posterSmall": u["posterSmall"], "poster": u["poster"],
                "duration": r["duration"], "username": r["username"]}
    cols = "v.post_id, v.title, v.file_id, v.status, v.poster, v.storyboard, v.original, v.duration, p.username"
    if vids:
        marks = ",".join("?" * len(vids))
        for r in db.execute(f"SELECT {cols} FROM videos v JOIN posts p ON p.id=v.post_id WHERE v.status='ready' AND v.post_id IN ({marks})", tuple(vids)):
            out["videos"][str(r["post_id"])] = vjson(r)
    r = db.execute(f"""SELECT {cols} FROM videos v JOIN posts p ON p.id=v.post_id
                       WHERE p.username=? AND v.status='ready' AND v.unlisted=0 AND v.post_id != ?
                       ORDER BY v.published DESC, v.post_id DESC LIMIT 1""", (owner, post_id)).fetchone()
    if r is not None:
        out["latest"] = vjson(r)
    if pls:
        marks = ",".join("?" * len(pls))
        for r in db.execute(f"""SELECT id, name, cover, username, featured,
                                   (SELECT COUNT(*) FROM video_playlist_items i WHERE i.playlist_id = video_playlists.id) AS n
                                FROM video_playlists WHERE id IN ({marks})""", tuple(pls)):
            if not r["featured"] and r["username"] != username:
                continue
            first = db.execute("""SELECT v.post_id, v.file_id, v.status, v.poster, v.storyboard, v.original
                                  FROM video_playlist_items i JOIN videos v ON v.post_id=i.video_id
                                  WHERE i.playlist_id=? AND v.status='ready' ORDER BY i.pos LIMIT 1""", (r["id"],)).fetchone()
            out["playlists"][r["id"]] = {
                "id": r["id"], "name": r["name"], "count": r["n"], "username": r["username"],
                "cover": f"/api/music-requests/playlists/covers/{r['cover']}/s" if r["cover"] else (V._urls(first)["posterSmall"] if first else None)}
    if tvs:
        marks = ",".join("?" * len(tvs))
        for r in db.execute(f"SELECT id, name, number, color FROM tv_channels WHERE id IN ({marks})", tuple(tvs)):
            out["tv"][r["id"]] = {"id": r["id"], "name": r["name"], "number": r["number"], "color": r["color"]}
    return out


def _links_of(anns, cards, end) -> list[dict]:
    links = [a.get("link") for a in anns if a.get("link")]
    links += [c.get("target") for c in cards if c.get("target")]
    if end:
        links += [e.get("target") for e in end["elements"] if e.get("target")]
    return links


def _overlays(db, row, username: str, owner: bool) -> dict:
    anns = _annotations(db, row["post_id"])
    cards = _cards(db, row["post_id"], username, owner)
    end = _endscreen(db, row["post_id"])
    return {"annotations": anns, "cards": cards, "endscreen": end,
            "refs": _expand(db, _links_of(anns, cards, end), row["username"], row["post_id"], username)}


def _studio_json(db, row, request: Request, username: str) -> dict:
    edited = bool(row["edit_live"])
    urls = V._urls(row)
    try:
        info = V.probe(os.path.join(V._folder(row["file_id"]), f"source.{row['ext']}")) or {}
    except Exception:  # noqa: BLE001
        info = {}
    audio = bool(info.get("audio", True))
    base_meta = _loads(row["base"], {}) if edited else None
    files = f"{BASE}/{row['post_id']}/studio/files/{row['file_id']}"
    if edited:
        board = (base_meta or {}).get("storyboard")
        source = {"duration": base_meta.get("duration"), "width": base_meta.get("width"), "height": base_meta.get("height"),
                  "fps": ((base_meta.get("rungs") or [{}])[0] or {}).get("fps"),
                  "hls": f"{files}/base/master.m3u8",
                  "storyboard": {"url": f"{files}/base/storyboard.jpg", **board} if board else None,
                  "wave": f"{files}/base/studio-wave.png" if _ensure_wave(row, audio) else None}
    else:
        rungs = _loads(row["rungs"], []) or [{}]
        source = {"duration": row["duration"], "width": row["width"], "height": row["height"],
                  "fps": (rungs[0] or {}).get("fps"), "hls": urls["hls"], "storyboard": urls["storyboard"],
                  "wave": f"{files}/studio-wave.png" if _ensure_wave(row, audio) else None}
    source["audio"] = audio
    if not source.get("fps"):
        source["fps"] = V._out_fps(info)[1] if info else 30
    return {
        "id": row["post_id"], "title": row["title"], "status": row["status"], "duration": row["duration"],
        "poster": urls["posterSmall"], "source": source,
        "edit": _loads(row["edit"], None), "live": _loads(row["edit_live"], None),
        "render": {"state": row["render"] or "", "progress": row["render_progress"] or 0, "error": row["render_error"] or None},
        **_overlays(db, row, username, True),
    }


# ── Routes ──

@router.get(f"{BASE}/{{post_id}}/studio")
def get_studio(post_id: int, request: Request):
    username, row, err = _owner(post_id, request)
    if err:
        return err
    if row["status"] != "ready":
        return _error(409, "the video's still being made")
    with get_db() as db:
        return _studio_json(db, row, request, username)


@router.put(f"{BASE}/{{post_id}}/studio/edit")
async def save_edit(post_id: int, request: Request):
    """{edit: {...} | null}: saves the edit and queues its render (null,
    or an edit that changes nothing, goes back to the upload as it came)."""
    username, row, err = _owner(post_id, request)
    if err:
        return err
    if row["status"] != "ready":
        return _error(409, "the video's still being made")
    if not media_ready():
        return _error(503, "storage is offline")
    if not shutil.which("ffmpeg"):
        return _error(503, "this server can't make videos (no ffmpeg)")
    body = await _body(request)
    if not isinstance(body, dict):
        return _error(400, "invalid JSON")
    source_len = (_loads(row["base"], {}) or {}).get("duration") if row["edit_live"] else row["duration"]
    try:
        edit = clean_edit(body.get("edit"), source_len or 0)
    except ValueError as e:
        return _error(400, str(e))
    want = json.dumps(edit) if edit else ""
    with get_db() as db:
        if want == (row["edit_live"] or "") and want == (row["edit"] or "") and (row["render"] or "") != "failed":
            return _studio_json(db, _row(db, post_id), request, username)
        state = "rendering" if row["render"] == "rendering" else "queued"
        db.execute("UPDATE videos SET edit=?, render=?, render_error='' WHERE post_id=?", (want, state, post_id))
        db.commit()
        if want == (row["edit_live"] or "") and row["render"] != "rendering":
            # Back to what's already there (a failed or queued change undone).
            db.execute("UPDATE videos SET render='' WHERE post_id=?", (post_id,))
            db.commit()
        else:
            enqueue(post_id)
        return _studio_json(db, _row(db, post_id), request, username)


@router.get(f"{BASE}/{{post_id}}/studio/files/{{file_id}}/{{path:path}}")
def studio_file(post_id: int, file_id: str, path: str, request: Request):
    _u, row, err = _owner(post_id, request)
    if err:
        return err
    if row["file_id"] != file_id or not _STUDIO_FILES.fullmatch(path):
        return _error(404, "not found")
    full = os.path.join(V._folder(file_id), *path.split("/"))
    if not os.path.isfile(full):
        return _error(404, "not found")
    return FileResponse(full, media_type=_TYPES[path.rsplit(".", 1)[1]],
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})


@router.get(f"{BASE}/{{post_id}}/overlays")
def get_overlays(post_id: int, request: Request):
    """Annotations, cards and the end screen, as the player draws them."""
    username = V._signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    with get_db() as db:
        row = _row(db, post_id)
        if row is None:
            return _error(404, "not found")
        return _overlays(db, row, username, V._can_edit(row["username"], request, username))


def _length(row) -> float:
    """The video's length once its wanted edit is made (what annotations
    and cards are timed against: the studio saves the edit first)."""
    want = _loads(row["edit"], None)
    if want:
        return edited_length(want, 0)
    if row["edit_live"]:
        return (_loads(row["base"], {}) or {}).get("duration") or row["duration"] or 0
    return row["duration"] or 0


@router.get(f"{BASE}/{{post_id}}/annotations")
def get_annotations(post_id: int, request: Request):
    if not V._signed_in(request):
        return _error(401, "not authenticated")
    with get_db() as db:
        if _row(db, post_id) is None:
            return _error(404, "not found")
        return {"annotations": _annotations(db, post_id)}


@router.put(f"{BASE}/{{post_id}}/annotations")
async def put_annotations(post_id: int, request: Request):
    _u, row, err = _owner(post_id, request)
    if err:
        return err
    body = await _body(request)
    try:
        anns = clean_annotations((body or {}).get("annotations") if isinstance(body, dict) else body, _length(row))
    except ValueError as e:
        return _error(400, str(e))
    with get_db() as db:
        db.execute("DELETE FROM video_annotations WHERE video_id=?", (post_id,))
        db.executemany("""INSERT INTO video_annotations(video_id, id, type, start, "end", x, y, w, h, text, style, link)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       [(post_id, a["id"], a["type"], a["start"], a["end"], a["x"], a["y"], a["w"], a["h"], a["text"],
                         json.dumps(a["style"]), json.dumps(a["link"]) if a["link"] else "") for a in anns])
        db.commit()
        return {"annotations": _annotations(db, post_id)}


@router.get(f"{BASE}/{{post_id}}/cards")
def get_cards(post_id: int, request: Request):
    username = V._signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    with get_db() as db:
        row = _row(db, post_id)
        if row is None:
            return _error(404, "not found")
        return {"cards": _cards(db, post_id, username, V._can_edit(row["username"], request, username))}


@router.put(f"{BASE}/{{post_id}}/cards")
async def put_cards(post_id: int, request: Request):
    username, row, err = _owner(post_id, request)
    if err:
        return err
    body = await _body(request)
    try:
        cards = clean_cards((body or {}).get("cards") if isinstance(body, dict) else body, _length(row))
    except ValueError as e:
        return _error(400, str(e))
    with get_db() as db:
        db.execute("DELETE FROM video_cards WHERE video_id=?", (post_id,))
        db.executemany("INSERT INTO video_cards(video_id, id, kind, at, teaser, body) VALUES (?, ?, ?, ?, ?, ?)",
                       [(post_id, c["id"], c["kind"], c["at"], c["teaser"],
                         json.dumps(c["poll"] if c["kind"] == "poll" else {"target": c["target"]})) for c in cards])
        # Votes stay with polls that stay (and answers that are still there).
        keep = {c["id"]: len(c["poll"]["options"]) for c in cards if c["kind"] == "poll"}
        for v in db.execute("SELECT DISTINCT card_id FROM video_card_votes WHERE video_id=?", (post_id,)).fetchall():
            if v["card_id"] not in keep:
                db.execute("DELETE FROM video_card_votes WHERE video_id=? AND card_id=?", (post_id, v["card_id"]))
            else:
                db.execute("DELETE FROM video_card_votes WHERE video_id=? AND card_id=? AND option >= ?", (post_id, v["card_id"], keep[v["card_id"]]))
        db.commit()
        return {"cards": _cards(db, post_id, username, True)}


@router.post(f"{BASE}/{{post_id}}/cards/{{card_id}}/vote")
async def vote(post_id: int, card_id: str, request: Request):
    """{option: n}: one vote a person (pressing another answer moves it)."""
    username = V._signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    body = await _body(request)
    try:
        option = int((body or {}).get("option"))
    except (TypeError, ValueError, AttributeError):
        return _error(400, "option (a number) required")
    with get_db() as db:
        row = _row(db, post_id)
        c = db.execute("SELECT * FROM video_cards WHERE video_id=? AND id=? AND kind='poll'", (post_id, card_id)).fetchone()
        if row is None or c is None:
            return _error(404, "not found")
        opts = (_loads(c["body"], {}) or {}).get("options") or []
        if not 0 <= option < len(opts):
            return _error(400, "no such answer")
        db.execute("""INSERT INTO video_card_votes(video_id, card_id, username, option) VALUES (?, ?, ?, ?)
                      ON CONFLICT(video_id, card_id, username) DO UPDATE SET option=excluded.option, at=datetime('now')""",
                   (post_id, card_id, username, option))
        db.commit()
        card = next(x for x in _cards(db, post_id, username, V._can_edit(row["username"], request, username)) if x["id"] == card_id)
        return card


@router.get(f"{BASE}/{{post_id}}/endscreen")
def get_endscreen(post_id: int, request: Request):
    if not V._signed_in(request):
        return _error(401, "not authenticated")
    with get_db() as db:
        if _row(db, post_id) is None:
            return _error(404, "not found")
        return {"endscreen": _endscreen(db, post_id)}


@router.put(f"{BASE}/{{post_id}}/endscreen")
async def put_endscreen(post_id: int, request: Request):
    _u, row, err = _owner(post_id, request)
    if err:
        return err
    body = await _body(request)
    try:
        end = clean_endscreen((body or {}).get("endscreen") if isinstance(body, dict) else None, _length(row))
    except ValueError as e:
        return _error(400, str(e))
    with get_db() as db:
        db.execute("DELETE FROM video_endscreen WHERE video_id=?", (post_id,))
        if end:
            db.execute("INSERT INTO video_endscreen(video_id, start_offset, elements) VALUES (?, ?, ?)",
                       (post_id, end["start"], json.dumps(end["elements"])))
        db.commit()
        return {"endscreen": _endscreen(db, post_id)}


def delete_for(post_id: int, db) -> None:
    """A video's studio rows, when it's deleted (videos.delete_files_for)."""
    for table in ("video_annotations", "video_cards", "video_card_votes", "video_endscreen"):
        db.execute(f"DELETE FROM {table} WHERE video_id=?", (post_id,))

