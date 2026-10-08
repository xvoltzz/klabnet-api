"""Playlists: a name, a description, a cover, a colour and an ordered list
of songs, kept here rather than in each person's prefs blob so they can be
shown to other people.

A playlist is private until its owner features it; a featured one is listed
for everyone (Music Home's shelf, Browse -> Playlists), and anyone can play
it or save it to their own sidebar. A featured playlist can also be opened
up so other people add songs to it ("collab"). Songs are the same
client-provided records favourites use, not checked against the library.

Under /api/music-requests/ because Caddy only forwards an explicit list of
/api prefixes and that one is Music's (the artwork endpoints ride it too).
Registered before music_requests.router so nothing here is ever taken for
its /api/music-requests/{request_id}.

Covers are files on the media share (playlists/<id[:2]>/<id>-l.jpg, -s.jpg:
1000px and 300px squares). A new upload gets a new id, so the URLs can be
cached forever.
"""

import io
import json
import os
import re
import secrets
import threading

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, ImageOps

from .. import photos_store  # noqa: F401  (registers the HEIC opener and Pillow's pixel limit)
from ..auth import get_groups, get_username, is_admin
from ..config import (
    MEDIA_DIR,
    PLAYLIST_COVER_MAX_MB,
    PLAYLIST_DESC_MAX_CHARS,
    PLAYLIST_MAX_SONGS,
    PLAYLIST_NAME_MAX_CHARS,
    PLAYLISTS_PER_USER,
    SONG_FIELD_MAX_CHARS,
)
from ..db import get_db
from ..media import media_ready

router = APIRouter()

BASE = "/api/music-requests/playlists"

# Navidrome ids end up in stream and cover URLs on every listener's client.
_ND_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_COVER_ID_RE = re.compile(r"[A-Za-z0-9_-]{16}")
_HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
COVER_SIZES = {"l": 1000, "s": 300}
_MAX_COVER_BYTES = PLAYLIST_COVER_MAX_MB * 1024 * 1024
_cover_slots = threading.BoundedSemaphore(2)

_SONG_TEXT = ("title", "artist", "album", "albumArtist", "displayAlbumArtist")
_SONG_IDS = ("artistId", "albumId", "coverArt")
_SONG_INTS = ("duration", "track", "year", "bitRate", "bitDepth", "samplingRate")


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _unauthenticated() -> JSONResponse:
    return _error(401, "not authenticated")


async def _json_body(request: Request) -> dict | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _clean_song(raw) -> dict | None:
    """A client's song record -> what's kept, or None if it can't be played
    or shown (no well-formed id, no title)."""
    if not isinstance(raw, dict):
        return None
    song_id = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or "").strip()[:SONG_FIELD_MAX_CHARS]
    if not _ND_ID_RE.fullmatch(song_id) or not title:
        return None
    song = {"id": song_id, "title": title}
    for k in _SONG_TEXT[1:]:
        v = str(raw.get(k) or "").strip()[:SONG_FIELD_MAX_CHARS]
        if v:
            song[k] = v
    for k in _SONG_IDS:
        v = str(raw.get(k) or "").strip()
        if _ND_ID_RE.fullmatch(v):
            song[k] = v
    for k in _SONG_INTS:
        v = raw.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < 10_000_000:
            song[k] = int(v)
    suffix = str(raw.get("suffix") or "").strip()[:8]
    if suffix.isalnum():
        song["suffix"] = suffix
    mbid = str(raw.get("musicBrainzId") or "").strip()
    if re.fullmatch(r"[0-9a-fA-F-]{1,40}", mbid):
        song["musicBrainzId"] = mbid
    return song


def _clean_songs(raw, limit: int) -> list[dict]:
    out, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        song = _clean_song(item)
        if song and song["id"] not in seen:
            seen.add(song["id"])
            out.append(song)
            if len(out) >= limit:
                break
    return out


def _fields(body: dict, current: dict) -> dict | JSONResponse:
    """The editable fields present in `body`, validated, laid over `current`."""
    out = dict(current)
    if "name" in body:
        name = " ".join(str(body.get("name") or "").split())[:PLAYLIST_NAME_MAX_CHARS]
        if not name:
            return _error(400, "a playlist needs a name")
        out["name"] = name
    if "description" in body:
        out["description"] = str(body.get("description") or "").strip()[:PLAYLIST_DESC_MAX_CHARS]
    if "color" in body:
        color = str(body.get("color") or "").strip()
        # Goes straight into a style attribute on every viewer's page.
        if color and not _HEX_COLOR_RE.fullmatch(color):
            return _error(400, "color must be a hex color like #a1b2c3")
        out["color"] = color.lower()
    if "featured" in body:
        out["featured"] = 1 if body.get("featured") else 0
    if "collab" in body:
        out["collab"] = 1 if body.get("collab") else 0
    return out


_NEW = {"name": "", "description": "", "color": "", "featured": 0, "collab": 0}
_SELECT = "SELECT id, username, name, description, color, cover, featured, collab, created, updated FROM playlists"


def _summaries(db, rows, username: str) -> list[dict]:
    """Rows -> what a list shows: no songs, but how many, how long, and the
    first few covers (for a playlist with no cover of its own)."""
    ids = [r["id"] for r in rows]
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    totals = {
        r["playlist_id"]: (r["n"], r["secs"] or 0)
        for r in db.execute(
            f"SELECT playlist_id, COUNT(*) AS n, SUM(duration) AS secs FROM playlist_songs WHERE playlist_id IN ({marks}) GROUP BY playlist_id",
            ids,
        ).fetchall()
    }
    art: dict[str, list[str]] = {}
    for r in db.execute(
        f"""SELECT playlist_id, cover_art, MIN(position) AS first FROM playlist_songs
            WHERE playlist_id IN ({marks}) AND cover_art != ''
            GROUP BY playlist_id, cover_art ORDER BY playlist_id, first""",
        ids,
    ).fetchall():
        covers = art.setdefault(r["playlist_id"], [])
        if len(covers) < 4:
            covers.append(r["cover_art"])
    saves = {
        r["playlist_id"]: (r["n"], bool(r["mine"]))
        for r in db.execute(
            f"SELECT playlist_id, COUNT(*) AS n, MAX(username = ?) AS mine FROM playlist_saves WHERE playlist_id IN ({marks}) GROUP BY playlist_id",
            (username, *ids),
        ).fetchall()
    }
    out = []
    for r in rows:
        n, secs = totals.get(r["id"], (0, 0))
        saved_n, saved = saves.get(r["id"], (0, False))
        out.append({
            "id": r["id"], "owner": r["username"], "mine": r["username"] == username,
            "name": r["name"], "description": r["description"], "color": r["color"], "cover": r["cover"],
            "featured": bool(r["featured"]), "collab": bool(r["collab"]),
            "count": n, "duration": secs, "art": art.get(r["id"], []),
            "saves": saved_n, "saved": saved,
            "created": r["created"], "updated": r["updated"],
        })
    return out


def _summary(db, playlist_id: str, username: str) -> dict:
    row = db.execute(f"{_SELECT} WHERE id=?", (playlist_id,)).fetchone()
    return _summaries(db, [row], username)[0]


def _visible(row, username: str) -> bool:
    return row is not None and (row["username"] == username or bool(row["featured"]))


def _can_edit(row, request: Request, username: str) -> bool:
    return row["username"] == username or is_admin(get_groups(request))


def _touch(db, playlist_id: str) -> None:
    db.execute("UPDATE playlists SET updated=datetime('now') WHERE id=?", (playlist_id,))


def _insert_songs(db, playlist_id: str, songs: list[dict], username: str) -> int:
    """Appends the ones that aren't in it yet, up to the cap. Returns how many went in."""
    row = db.execute(
        "SELECT COUNT(*) AS n, COALESCE(MAX(position), -1) AS last FROM playlist_songs WHERE playlist_id=?",
        (playlist_id,),
    ).fetchone()
    room, position, added = PLAYLIST_MAX_SONGS - row["n"], row["last"], 0
    for song in songs:
        if added >= room:
            break
        position += 1
        cur = db.execute(
            """INSERT OR IGNORE INTO playlist_songs(playlist_id, song_id, position, song_json, cover_art, duration, added_by)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (playlist_id, song["id"], position, json.dumps(song), song.get("coverArt", ""), song.get("duration", 0), username),
        )
        added += cur.rowcount
    return added


def _create(db, username: str, fields: dict, songs: list[dict], legacy_id: str = "") -> str:
    playlist_id = secrets.token_urlsafe(9)  # 12 url-safe chars
    db.execute(
        """INSERT INTO playlists(id, username, name, description, color, featured, collab, legacy_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (playlist_id, username, fields["name"], fields["description"], fields["color"],
         fields["featured"], fields["collab"] if fields["featured"] else 0, legacy_id),
    )
    _insert_songs(db, playlist_id, songs, username)
    return playlist_id


# ── Lists ──

@router.get(BASE)
def list_playlists(request: Request):
    """Everything the caller can see: their own (oldest first, the order
    they made them in), then other people's featured ones, most recently
    changed first. `mine` and `saved` say which is which."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    with get_db() as db:
        mine = db.execute(f"{_SELECT} WHERE username=? ORDER BY created, rowid", (username,)).fetchall()
        others = db.execute(
            f"{_SELECT} WHERE featured=1 AND username!=? ORDER BY updated DESC LIMIT 300", (username,)
        ).fetchall()
        return {"playlists": _summaries(db, [*mine, *others], username),
                "limits": {"name": PLAYLIST_NAME_MAX_CHARS, "description": PLAYLIST_DESC_MAX_CHARS,
                           "songs": PLAYLIST_MAX_SONGS, "covers": media_ready()}}


@router.post(BASE)
async def create_playlist(request: Request):
    """Body: {name, description?, color?, featured?, collab?, songs?}."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    body = await _json_body(request)
    if body is None:
        return _error(400, "invalid JSON")
    fields = _fields({"name": ""} | body, _NEW)
    if isinstance(fields, JSONResponse):
        return fields
    songs = _clean_songs(body.get("songs"), PLAYLIST_MAX_SONGS)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT COUNT(*) FROM playlists WHERE username=?", (username,)).fetchone()[0] >= PLAYLISTS_PER_USER:
            return _error(400, f"that's the most playlists one person can have ({PLAYLISTS_PER_USER})")
        playlist_id = _create(db, username, fields, songs)
        db.commit()
        return _summary(db, playlist_id, username)


@router.post(f"{BASE}/import")
async def import_playlists(request: Request):
    """Playlists from before they lived here (each browser kept them in
    prefs): body {playlists: [{id, name, songs}]}. Each old id is taken
    once per person, ever, so several devices holding the same old list
    don't make copies, and one deleted since doesn't come back."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    body = await _json_body(request)
    if body is None or not isinstance(body.get("playlists"), list):
        return _error(400, "invalid JSON")
    imported = 0
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        have = db.execute("SELECT COUNT(*) FROM playlists WHERE username=?", (username,)).fetchone()[0]
        for raw in body["playlists"][:PLAYLISTS_PER_USER]:
            if not isinstance(raw, dict):
                continue
            legacy_id = str(raw.get("id") or "").strip()[:64]
            name = " ".join(str(raw.get("name") or "").split())[:PLAYLIST_NAME_MAX_CHARS]
            if not legacy_id or not name or have >= PLAYLISTS_PER_USER:
                continue
            cur = db.execute(
                "INSERT OR IGNORE INTO playlist_imports(username, legacy_id) VALUES (?, ?)", (username, legacy_id)
            )
            if not cur.rowcount:
                continue
            _create(db, username, _NEW | {"name": name}, _clean_songs(raw.get("songs"), PLAYLIST_MAX_SONGS), legacy_id)
            have += 1
            imported += 1
        db.commit()
    return {"imported": imported}


# ── One playlist ──

@router.get(f"{BASE}/{{playlist_id}}")
def get_playlist(playlist_id: str, request: Request):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    with get_db() as db:
        row = db.execute(f"{_SELECT} WHERE id=?", (playlist_id,)).fetchone()
        if not _visible(row, username):
            return _error(404, "not found")
        playlist = _summaries(db, [row], username)[0]
        songs = []
        for s in db.execute(
            "SELECT song_json, added_by, added FROM playlist_songs WHERE playlist_id=? ORDER BY position", (playlist_id,)
        ).fetchall():
            try:
                song = json.loads(s["song_json"])
            except ValueError:
                continue
            song["addedBy"], song["added"] = s["added_by"], s["added"]
            songs.append(song)
        playlist["songs"] = songs
        playlist["can_edit"] = _can_edit(row, request, username)
    return playlist


@router.patch(f"{BASE}/{{playlist_id}}")
async def edit_playlist(playlist_id: str, request: Request):
    """Any of {name, description, color, featured, collab}. Owner or admin."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    body = await _json_body(request)
    if body is None:
        return _error(400, "invalid JSON")
    with get_db() as db:
        row = db.execute(f"{_SELECT} WHERE id=?", (playlist_id,)).fetchone()
        if not _visible(row, username):
            return _error(404, "not found")
        if not _can_edit(row, request, username):
            return _error(403, "not your playlist")
        fields = _fields(body, {k: row[k] for k in _NEW})
        if isinstance(fields, JSONResponse):
            return fields
        if not fields["featured"]:
            fields["collab"] = 0
        db.execute(
            "UPDATE playlists SET name=?, description=?, color=?, featured=?, collab=?, updated=datetime('now') WHERE id=?",
            (fields["name"], fields["description"], fields["color"], fields["featured"], fields["collab"], playlist_id),
        )
        # No longer featured: it leaves the sidebars of the people who saved it.
        if not fields["featured"]:
            db.execute("DELETE FROM playlist_saves WHERE playlist_id=?", (playlist_id,))
        db.commit()
        return _summary(db, playlist_id, username)


@router.delete(f"{BASE}/{{playlist_id}}")
def delete_playlist(playlist_id: str, request: Request):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    with get_db() as db:
        row = db.execute(f"{_SELECT} WHERE id=?", (playlist_id,)).fetchone()
        if not _visible(row, username):
            return _error(404, "not found")
        if not _can_edit(row, request, username):
            return _error(403, "not your playlist")
        db.execute("DELETE FROM playlist_songs WHERE playlist_id=?", (playlist_id,))
        db.execute("DELETE FROM playlist_saves WHERE playlist_id=?", (playlist_id,))
        db.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
        db.commit()
    _delete_cover_files(row["cover"])
    return {"ok": True}


# ── Its songs ──

@router.post(f"{BASE}/{{playlist_id}}/songs")
async def add_songs(playlist_id: str, request: Request):
    """Body: {songs: [...]}. The owner, or anyone when it's featured and
    open to additions. Songs already in it are skipped."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    body = await _json_body(request)
    if body is None:
        return _error(400, "invalid JSON")
    songs = _clean_songs(body.get("songs"), PLAYLIST_MAX_SONGS)
    if not songs:
        return _error(400, "no songs to add")
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(f"{_SELECT} WHERE id=?", (playlist_id,)).fetchone()
        if not _visible(row, username):
            return _error(404, "not found")
        if not (_can_edit(row, request, username) or (row["featured"] and row["collab"])):
            return _error(403, "this playlist isn't open to additions")
        added = _insert_songs(db, playlist_id, songs, username)
        if added:
            _touch(db, playlist_id)
        db.commit()
        count = db.execute("SELECT COUNT(*) FROM playlist_songs WHERE playlist_id=?", (playlist_id,)).fetchone()[0]
    return {"added": added, "skipped": len(songs) - added, "count": count, "full": count >= PLAYLIST_MAX_SONGS}


@router.delete(f"{BASE}/{{playlist_id}}/songs/{{song_id}}")
def remove_song(playlist_id: str, song_id: str, request: Request):
    """The owner can take out anything; on an open playlist, other people
    can take back what they added themselves."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    with get_db() as db:
        row = db.execute(f"{_SELECT} WHERE id=?", (playlist_id,)).fetchone()
        if not _visible(row, username):
            return _error(404, "not found")
        song = db.execute(
            "SELECT added_by FROM playlist_songs WHERE playlist_id=? AND song_id=?", (playlist_id, song_id)
        ).fetchone()
        if song is None:
            return {"ok": True}  # already gone: the same end
        if not (_can_edit(row, request, username) or (row["collab"] and song["added_by"] == username)):
            return _error(403, "not yours to remove")
        db.execute("DELETE FROM playlist_songs WHERE playlist_id=? AND song_id=?", (playlist_id, song_id))
        _touch(db, playlist_id)
        db.commit()
    return {"ok": True}


@router.put(f"{BASE}/{{playlist_id}}/order")
async def reorder_songs(playlist_id: str, request: Request):
    """Body: {ids: [song ids in their new order]}. Songs left out (added by
    someone else meanwhile) keep their order, after the ones named."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    body = await _json_body(request)
    if body is None or not isinstance(body.get("ids"), list):
        return _error(400, "invalid JSON")
    wanted = list(dict.fromkeys(str(i) for i in body["ids"]))
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(f"{_SELECT} WHERE id=?", (playlist_id,)).fetchone()
        if not _visible(row, username):
            return _error(404, "not found")
        if not _can_edit(row, request, username):
            return _error(403, "not your playlist")
        current = [r["song_id"] for r in db.execute(
            "SELECT song_id FROM playlist_songs WHERE playlist_id=? ORDER BY position", (playlist_id,)).fetchall()]
        have = set(current)
        named = [i for i in wanted if i in have]
        named_set = set(named)
        order = named + [i for i in current if i not in named_set]
        db.executemany(
            "UPDATE playlist_songs SET position=? WHERE playlist_id=? AND song_id=?",
            [(position, playlist_id, song_id) for position, song_id in enumerate(order)],
        )
        _touch(db, playlist_id)
        db.commit()
    return {"ok": True, "ids": order}


# ── Saving someone else's to your own sidebar ──

@router.put(f"{BASE}/{{playlist_id}}/save")
async def save_playlist(playlist_id: str, request: Request):
    """Body: {saved: bool}."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    body = await _json_body(request)
    if body is None:
        return _error(400, "invalid JSON")
    with get_db() as db:
        row = db.execute(f"{_SELECT} WHERE id=?", (playlist_id,)).fetchone()
        if not _visible(row, username):
            return _error(404, "not found")
        if row["username"] == username:
            return _error(400, "that one's already yours")
        if body.get("saved"):
            db.execute("INSERT OR IGNORE INTO playlist_saves(playlist_id, username) VALUES (?, ?)", (playlist_id, username))
        else:
            db.execute("DELETE FROM playlist_saves WHERE playlist_id=? AND username=?", (playlist_id, username))
        db.commit()
        return _summary(db, playlist_id, username)


# ── Covers ──

def _cover_path(cover_id: str, size: str) -> str:
    return os.path.join(MEDIA_DIR, "playlists", cover_id[:2], f"{cover_id}-{size}.jpg")


def _delete_cover_files(cover_id: str) -> None:
    if not cover_id or not _COVER_ID_RE.fullmatch(cover_id):
        return
    for size in COVER_SIZES:
        try:
            os.remove(_cover_path(cover_id, size))
        except OSError:
            pass


def _write_cover(data: bytes) -> str:
    """Any image -> square JPEGs at COVER_SIZES, cropped from the middle
    (the site's cropper sends a square already). Returns the new cover id;
    raises ValueError with a message for the uploader."""
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > Image.MAX_IMAGE_PIXELS:
            raise ValueError("that image is too large")
        img.load()
    except ValueError:
        raise
    except Image.DecompressionBombError:
        raise ValueError("that image is too large")
    except Exception:
        raise ValueError("that file isn't an image we can read")
    img = ImageOps.exif_transpose(img)
    if "A" in img.getbands() or img.mode == "P":
        img = img.convert("RGBA")
        flat = Image.new("RGB", img.size, (16, 16, 16))
        flat.paste(img, mask=img.getchannel("A"))
        img = flat
    elif img.mode != "RGB":
        img = img.convert("RGB")
    img.info = {}  # no metadata (a phone photo's location) rides along
    img = ImageOps.fit(img, (COVER_SIZES["l"], COVER_SIZES["l"]), Image.LANCZOS)
    cover_id = secrets.token_urlsafe(12)  # 16 url-safe chars
    os.makedirs(os.path.dirname(_cover_path(cover_id, "l")), exist_ok=True)
    try:
        for size, edge in COVER_SIZES.items():
            out = img if edge == img.width else img.resize((edge, edge), Image.LANCZOS)
            tmp = _cover_path(cover_id, size) + ".partial"
            out.save(tmp, "JPEG", quality=90, optimize=True, progressive=True)
            os.replace(tmp, _cover_path(cover_id, size))
    except Exception:
        _delete_cover_files(cover_id)
        raise
    return cover_id


@router.post(f"{BASE}/{{playlist_id}}/cover")
def upload_cover(playlist_id: str, request: Request, file: UploadFile = File(...)):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    if not media_ready():
        return _error(503, "cover storage is offline")
    with get_db() as db:
        row = db.execute(f"{_SELECT} WHERE id=?", (playlist_id,)).fetchone()
        if not _visible(row, username):
            return _error(404, "not found")
        if not _can_edit(row, request, username):
            return _error(403, "not your playlist")
    data = file.file.read(_MAX_COVER_BYTES + 1)
    if len(data) > _MAX_COVER_BYTES:
        return _error(413, f"covers can be up to {PLAYLIST_COVER_MAX_MB}MB")
    try:
        with _cover_slots:
            cover_id = _write_cover(data)
    except ValueError as e:
        return _error(400, str(e))
    with get_db() as db:
        # Whatever it has by now, not what it had before the upload: two
        # uploads at once must not leave the loser's files behind.
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT cover FROM playlists WHERE id=?", (playlist_id,)).fetchone()
        if row is None:
            db.rollback()
            _delete_cover_files(cover_id)
            return _error(404, "not found")
        db.execute("UPDATE playlists SET cover=?, updated=datetime('now') WHERE id=?", (cover_id, playlist_id))
        db.commit()
        _delete_cover_files(row["cover"])
        return _summary(db, playlist_id, username)


@router.delete(f"{BASE}/{{playlist_id}}/cover")
def remove_cover(playlist_id: str, request: Request):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    with get_db() as db:
        row = db.execute(f"{_SELECT} WHERE id=?", (playlist_id,)).fetchone()
        if not _visible(row, username):
            return _error(404, "not found")
        if not _can_edit(row, request, username):
            return _error(403, "not your playlist")
        db.execute("UPDATE playlists SET cover='', updated=datetime('now') WHERE id=?", (playlist_id,))
        db.commit()
        _delete_cover_files(row["cover"])
        return _summary(db, playlist_id, username)


@router.get(f"{BASE}/covers/{{cover_id}}/{{size}}")
def get_cover(cover_id: str, size: str):
    if size not in COVER_SIZES or not _COVER_ID_RE.fullmatch(cover_id):
        return _error(404, "not found")
    path = _cover_path(cover_id, size)
    if not os.path.isfile(path):
        return _error(404, "not found")
    # A cover id's files never change: a new upload is a new id.
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})
