"""Radio: stations that are each one long audio file, on
a loop, that everyone hears at the same point, like the radio in GTA.

Nothing streams. A station has a start time (`epoch`); where it's at is
(now - epoch) mod its length, the same for everyone, so a page plays the
file from there and keeps itself on that line against this server's
clock (GET …/radio/time). The file is served with Range support, so a
page only fetches from where it tunes in.

Anyone signed in can make a station; its owner (or an admin) can edit or
delete it. A station can be uploaded for someone else: `author` is who
it's by (shown instead of the owner), and when that's someone's username
they can edit it too. Files live on the media share under radio/, named by a new id
for each upload so they can be cached forever. Covers reuse playlists'
(routers/playlists.py). Under /api/music-requests/ so it rides a prefix
Caddy already forwards."""
import json
import os
import re
import secrets
import threading
import time

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ..auth import get_groups, get_username, is_admin
from ..config import MEDIA_DIR, RADIO_MAX_MB
from ..db import get_db
from ..media import media_ready
from .playlists import _MAX_COVER_BYTES, _cover_slots, _delete_cover_files, _write_cover

router = APIRouter()
BASE = "/api/music-requests/radio"

_TYPES = {
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac", "ogg": "audio/ogg",
    "opus": "audio/ogg", "flac": "audio/flac", "wav": "audio/wav", "webm": "audio/webm",
}
_MAX_BYTES = RADIO_MAX_MB * 1024 * 1024
_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,40}")
_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
# Big files: one being written at a time is plenty for a home server.
_upload_slots = threading.BoundedSemaphore(1)
# The FM band, and how far apart two stations must be: on the dial a
# station fades in over 0.25 MHz either side, so closer ones would overlap.
FREQ_MIN, FREQ_MAX, FREQ_GAP = 87.5, 108.0, 0.5


def _frequency(value, db, station_id: str = "") -> float | JSONResponse:
    try:
        f = round(float(str(value).replace(",", ".")), 1)
    except (TypeError, ValueError):
        return _error(400, "a station needs a frequency, like 98.7")
    if not FREQ_MIN <= f <= FREQ_MAX:
        return _error(400, f"frequencies go from {FREQ_MIN} to {FREQ_MAX}")
    near = db.execute(
        "SELECT name, frequency FROM radio_stations WHERE id != ? AND ABS(frequency - ?) < ? ORDER BY ABS(frequency - ?) LIMIT 1",
        (station_id, f, FREQ_GAP - 1e-6, f),
    ).fetchone()
    if near:
        return _error(409, f"too close to {near['name']} on {near['frequency']:.1f}: keep {FREQ_GAP} apart")
    return f


def _author(value) -> str:
    return str(value or "").strip().lstrip("@")[:60]


def _can_edit(r, request: Request, username: str) -> bool:
    return username in (r["owner"], r["author"]) or is_admin(get_groups(request))


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _audio_path(file_id: str, ext: str) -> str:
    return os.path.join(MEDIA_DIR, "radio", f"{file_id}.{ext}")


def _remove_audio(file_id: str, ext: str) -> None:
    if file_id and _ID_RE.fullmatch(file_id) and ext in _TYPES:
        try:
            os.remove(_audio_path(file_id, ext))
        except OSError:
            pass


# "0:00 Artist - Title", "[1:02:03] Title", "12:30 · Title": when each part
# of the mix starts, so the picker can say what's on.
_LINE_RE = re.compile(r"^\s*\[?((?:\d+:)?\d{1,2}:\d{2})\]?\s*[-–—·.|)]?\s*(.+?)\s*$")


def _tracklist(text: str) -> list:
    out = []
    for line in (text or "").splitlines()[:500]:
        m = _LINE_RE.match(line)
        if not m:
            continue
        secs = 0
        for part in m.group(1).split(":"):
            secs = secs * 60 + int(part)
        out.append([secs, m.group(2)[:200]])
    out.sort(key=lambda t: t[0])
    return out


def _length(path: str) -> float | None:
    """The file's length in seconds, read from its headers (mutagen), or None."""
    try:
        import mutagen
        f = mutagen.File(path)
        length = float(f.info.length) if f is not None and f.info else 0.0
    except Exception:
        return None
    return length if length >= 1 else None


def _save_audio(file: UploadFile) -> tuple[str, str, float, int] | JSONResponse:
    """The upload onto the share as a new file: (id, ext, length, bytes)."""
    ext = (os.path.splitext(file.filename or "")[1][1:] or "").lower()
    if ext not in _TYPES:
        return _error(400, "a station is an audio file: mp3, m4a, aac, ogg, opus, flac, wav or webm")
    file_id = secrets.token_urlsafe(12)
    path = _audio_path(file_id, ext)
    os.makedirs(os.path.dirname(path), exist_ok=True)
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
        length = _length(tmp)
        if length is None:
            raise ValueError
        os.replace(tmp, path)
    except OverflowError:
        os.remove(tmp)
        return _error(413, f"a station can be up to {RADIO_MAX_MB}MB")
    except ValueError:
        os.remove(tmp)
        return _error(400, "couldn't read how long that file is; is it really audio?")
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return file_id, ext, length, size


def _listeners() -> dict:
    """Who's tuned in to what, from presence's beats."""
    from .presence import _beats, _best
    out: dict[str, list] = {}
    for username in list(_beats):
        b = _best(username)
        if b and b.get("radio"):
            out.setdefault(b["radio"], []).append(username)
    return out


def _json(r, request: Request, username: str, listeners: dict | None = None) -> dict:
    try:
        tracks = json.loads(r["tracklist"] or "[]")
    except ValueError:
        tracks = []
    return {
        "id": r["id"], "name": r["name"], "description": r["description"], "color": r["color"] or None,
        "owner": r["owner"], "author": r["author"] or r["owner"], "duration": r["duration"], "epoch": r["epoch"], "frequency": r["frequency"],
        "audio": f"{BASE}/{r['id']}/audio/{r['file_id']}.{r['ext']}",
        "cover": r["cover"] or None, "tracklist": tracks,
        "canEdit": _can_edit(r, request, username),
        "listeners": (listeners or {}).get(r["id"], []),
        "created": r["created"],
    }


def now_on(station_id: str) -> dict | None:
    """A station as someone tuned in to it hears it right now: for
    presence (Activity's "Listening now"). None if there's no such station."""
    if not station_id:
        return None
    with get_db() as db:
        r = db.execute("SELECT * FROM radio_stations WHERE id=?", (station_id,)).fetchone()
    if r is None or not r["duration"]:
        return None
    pos = (time.time() - r["epoch"]) % r["duration"]
    try:
        tracks = json.loads(r["tracklist"] or "[]")
    except ValueError:
        tracks = []
    track = ""
    for t in tracks:
        if t[0] <= pos:
            track = t[1]
        else:
            break
    return {"id": r["id"], "name": r["name"], "frequency": r["frequency"], "color": r["color"] or None,
            "cover": r["cover"] or None, "author": r["author"] or r["owner"], "track": track,
            "position": round(pos, 1), "duration": r["duration"]}


def _signed_in(request: Request) -> str | None:
    u = get_username(request)
    return None if u == "anonymous" else u


@router.get(f"{BASE}/time")
def server_time(request: Request):
    """This server's clock, which every page keeps its stations against."""
    return {"now": time.time()}


@router.get(BASE)
def list_stations(request: Request):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    listeners = _listeners()
    with get_db() as db:
        rows = db.execute("SELECT * FROM radio_stations ORDER BY frequency, created").fetchall()
    return {"now": time.time(), "stations": [_json(r, request, username, listeners) for r in rows]}


@router.post(BASE)
def create_station(request: Request, file: UploadFile = File(...), name: str = Form(...),
                   frequency: str = Form(""), author: str = Form(""), description: str = Form(""), color: str = Form(""), tracklist: str = Form("")):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    name = name.strip()[:60]
    if not name:
        return _error(400, "a station needs a name")
    if not media_ready():
        return _error(503, "storage is offline")
    # Checked before the (big) upload is written, and again after.
    with get_db() as db:
        freq = _frequency(frequency, db)
    if isinstance(freq, JSONResponse):
        return freq
    saved = _save_audio(file)
    if isinstance(saved, JSONResponse):
        return saved
    file_id, ext, length, size = saved
    station_id = secrets.token_urlsafe(9)
    with get_db() as db:
        freq = _frequency(frequency, db)
        if isinstance(freq, JSONResponse):
            _remove_audio(file_id, ext)
            return freq
        pos = db.execute("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM radio_stations").fetchone()["p"]
        db.execute(
            """INSERT INTO radio_stations(id, name, description, color, owner, file_id, ext, duration, size, epoch, tracklist, position, frequency, author)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (station_id, name, description.strip()[:300], color if _COLOR_RE.fullmatch(color or "") else "",
             username, file_id, ext, length, size, round(time.time(), 3), json.dumps(_tracklist(tracklist)), pos, freq,
             "" if _author(author) == username else _author(author)),
        )
        db.commit()
        row = db.execute("SELECT * FROM radio_stations WHERE id=?", (station_id,)).fetchone()
    return _json(row, request, username)


def _editable(station_id: str, request: Request, username: str):
    with get_db() as db:
        row = db.execute("SELECT * FROM radio_stations WHERE id=?", (station_id,)).fetchone()
    if row is None:
        return None, _error(404, "not found")
    if not _can_edit(row, request, username):
        return None, _error(403, "not your station")
    return row, None


@router.patch(f"{BASE}/{{station_id}}")
async def edit_station(station_id: str, request: Request):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    row, err = _editable(station_id, request, username)
    if err:
        return err
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
    except Exception:
        return _error(400, "invalid JSON")
    sets, args = [], []
    if "name" in body:
        name = str(body["name"]).strip()[:60]
        if not name:
            return _error(400, "a station needs a name")
        sets.append("name=?"); args.append(name)
    if "description" in body:
        sets.append("description=?"); args.append(str(body["description"]).strip()[:300])
    if "color" in body:
        c = str(body["color"] or "")
        sets.append("color=?"); args.append(c if _COLOR_RE.fullmatch(c) else "")
    if "frequency" in body:
        with get_db() as db:
            freq = _frequency(body["frequency"], db, station_id)
        if isinstance(freq, JSONResponse):
            return freq
        sets.append("frequency=?"); args.append(freq)
    if "author" in body:
        a = _author(body["author"])
        sets.append("author=?"); args.append("" if a == row["owner"] else a)
    if "tracklist" in body:
        sets.append("tracklist=?"); args.append(json.dumps(_tracklist(str(body["tracklist"] or ""))))
    if sets:
        with get_db() as db:
            db.execute(f"UPDATE radio_stations SET {', '.join(sets)}, updated=datetime('now') WHERE id=?", (*args, station_id))
            db.commit()
    with get_db() as db:
        row = db.execute("SELECT * FROM radio_stations WHERE id=?", (station_id,)).fetchone()
    return _json(row, request, username, _listeners())


@router.put(f"{BASE}/{{station_id}}/audio")
def replace_audio(station_id: str, request: Request, file: UploadFile = File(...)):
    """A new mix for a station. It keeps its start time: it carries on from
    wherever the new length puts it."""
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    row, err = _editable(station_id, request, username)
    if err:
        return err
    if not media_ready():
        return _error(503, "storage is offline")
    saved = _save_audio(file)
    if isinstance(saved, JSONResponse):
        return saved
    file_id, ext, length, size = saved
    with get_db() as db:
        db.execute("UPDATE radio_stations SET file_id=?, ext=?, duration=?, size=?, updated=datetime('now') WHERE id=?",
                   (file_id, ext, length, size, station_id))
        db.commit()
        new = db.execute("SELECT * FROM radio_stations WHERE id=?", (station_id,)).fetchone()
    _remove_audio(row["file_id"], row["ext"])
    return _json(new, request, username, _listeners())


@router.post(f"{BASE}/{{station_id}}/cover")
def upload_cover(station_id: str, request: Request, file: UploadFile = File(...)):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    row, err = _editable(station_id, request, username)
    if err:
        return err
    if not media_ready():
        return _error(503, "storage is offline")
    data = file.file.read(_MAX_COVER_BYTES + 1)
    if len(data) > _MAX_COVER_BYTES:
        return _error(413, "that image is too big")
    try:
        with _cover_slots:
            cover_id = _write_cover(data)
    except ValueError as e:
        return _error(400, str(e))
    with get_db() as db:
        db.execute("UPDATE radio_stations SET cover=?, updated=datetime('now') WHERE id=?", (cover_id, station_id))
        db.commit()
        new = db.execute("SELECT * FROM radio_stations WHERE id=?", (station_id,)).fetchone()
    _delete_cover_files(row["cover"])
    return _json(new, request, username, _listeners())


@router.delete(f"{BASE}/{{station_id}}")
def delete_station(station_id: str, request: Request):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    row, err = _editable(station_id, request, username)
    if err:
        return err
    with get_db() as db:
        db.execute("DELETE FROM radio_stations WHERE id=?", (station_id,))
        db.execute("DELETE FROM radio_chat WHERE station=?", (station_id,))
        db.commit()
    _remove_audio(row["file_id"], row["ext"])
    _delete_cover_files(row["cover"])
    return {"ok": True}


@router.get(f"{BASE}/{{station_id}}/audio/{{name}}")
def station_audio(station_id: str, name: str, request: Request):
    """The station's file, with Range (a page only fetches from where it
    tunes in). Its name is per upload, so it never changes under a URL."""
    if not _signed_in(request):
        return _error(401, "not authenticated")
    file_id, _, ext = name.partition(".")
    with get_db() as db:
        row = db.execute("SELECT file_id, ext FROM radio_stations WHERE id=?", (station_id,)).fetchone()
    if row is None or row["file_id"] != file_id or row["ext"] != ext:
        return _error(404, "not found")
    path = _audio_path(file_id, ext)
    if not os.path.isfile(path):
        return _error(404, "not found")
    return FileResponse(path, media_type=_TYPES[ext], headers={"Cache-Control": "private, max-age=31536000, immutable"})


# ── A station's chat ──
# Pages poll it while the station's showing (GET …/chat?after=<last id>).
# Each line keeps what was on when it was said, from the tracklist.
_CHAT_KEEP = 300
_CHAT_MAX = 500


def _chat_json(r) -> dict:
    return {"id": r["id"], "username": r["username"], "body": r["body"], "track": r["track"], "ts": r["ts"]}


@router.get(f"{BASE}/{{station_id}}/chat")
def station_chat(station_id: str, request: Request, after: int = 0):
    if not _signed_in(request):
        return _error(401, "not authenticated")
    with get_db() as db:
        if after > 0:
            rows = db.execute("SELECT * FROM radio_chat WHERE station=? AND id>? ORDER BY id LIMIT 200", (station_id, after)).fetchall()
        else:
            rows = db.execute("SELECT * FROM (SELECT * FROM radio_chat WHERE station=? ORDER BY id DESC LIMIT 100) ORDER BY id", (station_id,)).fetchall()
    return {"messages": [_chat_json(r) for r in rows]}


@router.post(f"{BASE}/{{station_id}}/chat")
async def post_chat(station_id: str, request: Request):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    try:
        data = await request.json()
    except ValueError:
        return _error(400, "bad request")
    body = str((data or {}).get("body") or "").strip()[:_CHAT_MAX]
    if not body:
        return _error(400, "say something")
    on = now_on(station_id)
    if on is None:
        return _error(404, "no such station")
    with get_db() as db:
        cur = db.execute("INSERT INTO radio_chat (station, username, body, track, ts) VALUES (?,?,?,?,?)",
                         (station_id, username, body, on["track"], time.time()))
        db.execute("DELETE FROM radio_chat WHERE station=? AND id <= ?", (station_id, cur.lastrowid - _CHAT_KEEP))
        db.commit()
        row = db.execute("SELECT * FROM radio_chat WHERE id=?", (cur.lastrowid,)).fetchone()
    return _chat_json(row)
