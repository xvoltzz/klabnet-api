"""Music library requests — "please add this album/song", with MusicBrainz
search so a request comes with real title/artist/cover-art metadata instead
of a freeform text box.

The MusicBrainz calls happen here (server-side), not from the browser:
their API requires a real identifying User-Agent and is unauthenticated-rate-
limited to ~1 req/sec, both easier to get right in one place than from N
browser tabs, and it sidesteps any CORS question entirely.
"""

import asyncio
import os
import re
import time
import unicodedata
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from ..auth import get_groups, get_username, is_admin
from ..config import MEDIA_DIR, MUSICBRAINZ_USER_AGENT, REQUEST_FIELD_MAX_CHARS
from ..db import art_id, get_db
from ..media import media_ready

router = APIRouter()

MB_BASE = "https://musicbrainz.org/ws/2"
MB_HEADERS = {"User-Agent": MUSICBRAINZ_USER_AGENT, "Accept": "application/json"}
COVER_ART_BASE = "https://coverartarchive.org"

# MusicBrainz enforces ~1 request/sec per client and 503s anything faster —
# hit in practice with completely reasonable usage (the frontend's own
# 400ms search debounce alone can burst past this, before even counting two
# people using the feature at once, since it's all the same server IP).
# A single process-wide gate is enough here (one klabnet-api instance, no
# multi-worker deploy) — queues every call through a lock and sleeps out
# whatever's left of the window since the last one, so the server
# self-throttles regardless of how fast requests actually arrive. In
# practice a single 503 still shows up occasionally even fully spaced at
# 1.05s (their real limiter isn't purely a fixed per-request gap — a burst
# of manual testing was enough to trip it at that spacing), so this retries
# with real backoff rather than once at the same interval.
_MB_MIN_INTERVAL = 1.1
_MB_RETRY_BACKOFFS = (1.5, 3.0)  # extra attempts beyond the first, seconds apart
_mb_lock = asyncio.Lock()
_mb_last_call = 0.0


def _unauthenticated() -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": "not authenticated"})


def _artist_credit(credits: list) -> str:
    """MusicBrainz's artist-credit is a list of {name, joinphrase} pairs
    (handles "feat."/"&" collabs) — this flattens it to a plain display
    string the way the actual release title bar would read."""
    return "".join(f"{c.get('name', '')}{c.get('joinphrase', '')}" for c in credits or [])


async def _mb_get(url: str, params: dict) -> httpx.Response:
    """GET against MusicBrainz, self-throttled to _MB_MIN_INTERVAL apart,
    retrying with real backoff on a 503 (their own rate-limit response) —
    belt-and-suspenders in case something outside this process also hit
    their API in the same window, or their limiter is stricter than a
    fixed per-request gap in practice."""
    global _mb_last_call
    async with _mb_lock:
        wait = _MB_MIN_INTERVAL - (time.monotonic() - _mb_last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.get(url, params=params, headers=MB_HEADERS)
            for backoff in _MB_RETRY_BACKOFFS:
                if res.status_code != 503:
                    break
                await asyncio.sleep(backoff)
                res = await client.get(url, params=params, headers=MB_HEADERS)
            _mb_last_call = time.monotonic()
    return res


@router.get("/api/music-requests/search")
async def search_musicbrainz(q: str, type: str = "album"):
    """Proxies a MusicBrainz search so the request form can show real
    matches (with cover art) to pick from instead of a blind text field."""
    q = q.strip()
    if not q:
        return {"results": []}
    entity = "recording" if type == "song" else "release-group"
    try:
        res = await _mb_get(f"{MB_BASE}/{entity}", {"query": q, "fmt": "json", "limit": 8})
        res.raise_for_status()
        data = res.json()
    except httpx.HTTPError:
        return JSONResponse(status_code=502, content={"error": "musicbrainz lookup failed"})

    results = []
    if entity == "release-group":
        for rg in data.get("release-groups", []):
            results.append({
                "mbid": rg.get("id", ""),
                "title": rg.get("title", ""),
                "artist": _artist_credit(rg.get("artist-credit")),
                "year": (rg.get("first-release-date") or "")[:4],
                "cover_art_url": f"{COVER_ART_BASE}/release-group/{rg.get('id', '')}/front-250",
            })
    else:
        for rec in data.get("recordings", []):
            releases = rec.get("releases") or []
            release_group = (releases[0].get("release-group") if releases else None) or {}
            rg_id = release_group.get("id", "")
            release_id = releases[0].get("id", "") if releases else ""
            # A release-group cover is preferred (matches the album's actual
            # cover, not a single-release variant), falling back to the
            # specific release's own cover if there's no release-group link.
            cover_url = (
                f"{COVER_ART_BASE}/release-group/{rg_id}/front-250" if rg_id
                else f"{COVER_ART_BASE}/release/{release_id}/front-250" if release_id
                else ""
            )
            results.append({
                "mbid": rec.get("id", ""),
                "title": rec.get("title", ""),
                "artist": _artist_credit(rec.get("artist-credit")),
                "year": (rec.get("first-release-date") or "")[:4],
                "cover_art_url": cover_url,
            })
    return {"results": results}


@router.get("/api/music-requests")
def list_requests():
    with get_db() as db:
        rows = db.execute(
            """SELECT id, username, req_type, title, artist, mbid, cover_art_url, status, created
               FROM music_requests ORDER BY id DESC"""
        ).fetchall()
    return {"requests": [dict(r) for r in rows]}


@router.post("/api/music-requests")
async def create_request(request: Request):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})

    req_type = "song" if body.get("req_type") == "song" else "album"
    title = str(body.get("title", "")).strip()[:REQUEST_FIELD_MAX_CHARS]
    artist = str(body.get("artist", "")).strip()[:REQUEST_FIELD_MAX_CHARS]
    mbid = str(body.get("mbid", "")).strip()[:64]
    cover_art_url = str(body.get("cover_art_url", "")).strip()[:500]
    if not title:
        return JSONResponse(status_code=400, content={"error": "title required"})

    with get_db() as db:
        cur = db.execute(
            """INSERT INTO music_requests(username, req_type, title, artist, mbid, cover_art_url)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (username, req_type, title, artist, mbid, cover_art_url),
        )
        db.commit()
        row = db.execute(
            """SELECT id, username, req_type, title, artist, mbid, cover_art_url, status, created
               FROM music_requests WHERE id=?""",
            (cur.lastrowid,),
        ).fetchone()
    return dict(row)


@router.put("/api/music-requests/{request_id}")
async def update_request_status(request_id: int, request: Request):
    """Marking fulfilled/dismissed is an admin action (whoever actually adds
    the album to the library) — unlike the requester-or-admin delete below,
    a plain requester has no way to know the library was actually updated."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    if not is_admin(get_groups(request)):
        return JSONResponse(status_code=403, content={"error": "admin access required"})
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    status = str(body.get("status", "")).strip()
    if status not in ("pending", "fulfilled", "dismissed"):
        return JSONResponse(status_code=400, content={"error": "invalid status"})
    with get_db() as db:
        if db.execute("SELECT 1 FROM music_requests WHERE id=?", (request_id,)).fetchone() is None:
            return JSONResponse(status_code=404, content={"error": "not found"})
        db.execute("UPDATE music_requests SET status=? WHERE id=?", (status, request_id))
        db.commit()
        row = db.execute(
            """SELECT id, username, req_type, title, artist, mbid, cover_art_url, status, created
               FROM music_requests WHERE id=?""",
            (request_id,),
        ).fetchone()
    return dict(row)


@router.delete("/api/music-requests/{request_id}")
def delete_request(request_id: int, request: Request):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    with get_db() as db:
        row = db.execute("SELECT username FROM music_requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "not found"})
        if row["username"] != username and not is_admin(get_groups(request)):
            return JSONResponse(status_code=403, content={"error": "not your request"})
        db.execute("DELETE FROM music_requests WHERE id=?", (request_id,))
        db.commit()
    return {"ok": True}


# ── High-resolution artwork ──────────────────────────────────────────────
# klabnet's Cover Flow shows album art big (up to ~800px on a large screen,
# more in the full-size viewer) and many libraries' embedded art is small.
# This finds the same cover at high resolution for display: the iTunes
# catalogue first (up to 3000x3000), then Deezer's (its search finds plenty
# iTunes' misses), the Cover Art Archive when the album has a MusicBrainz
# ID. Only a confident match counts (the same artist and
# the same album name once edition noise is stripped): wrong art is worse
# than small art. Results, misses included, are cached per album.

ITUNES_SEARCH = "https://itunes.apple.com/search"
DEEZER_SEARCH = "https://api.deezer.com/search/album"
_ART_MISS_RETRY_DAYS = 14
_EDITION_NOISE = re.compile(
    r"\s*[\(\[][^\)\]]*(deluxe|remaster|master|mix|reissue|edition|expanded|anniversary|bonus|version|explicit|clean|mono|stereo|special)[^\)\]]*[\)\]]"
    r"|\s+-\s+(single|ep)$",
    re.I,
)


_FOLD = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L", "$": "s"})


def _norm(s: str) -> str:
    # Letters accents don't decompose (NØIR, Æ...), then the accents.
    s = unicodedata.normalize("NFKD", (s or "").translate(_FOLD)).encode("ascii", "ignore").decode().lower()
    s = _EDITION_NOISE.sub("", s)
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _first_artist(s: str) -> str:
    """"Massive Attack, Nicolette" / "A feat. B" / "A & B": the lead act."""
    return _norm(re.split(r",|\bfeat\.?|\bft\.?|\bwith\b", s or "", maxsplit=1, flags=re.I)[0])


def _same_artist(ours: str, theirs: str) -> bool:
    a, b = _norm(ours), _norm(theirs)
    if not a or not b:
        return False
    return a == b or _first_artist(ours) == _first_artist(theirs) or a in b or b in a


async def _itunes_art(artist: str, album: str):
    want = _norm(album)
    # Artist and album first; then the album alone, for artists credited
    # differently there ("The Voidz" is "Julian Casablancas+The Voidz").
    async with httpx.AsyncClient(timeout=8.0) as client:
        for term, limit in ((f"{artist} {album}", 15), (album, 40)):
            res = await client.get(ITUNES_SEARCH, params={"term": term, "entity": "album", "media": "music", "limit": limit})
            if res.status_code != 200:
                return None
            for r in res.json().get("results", []):
                art = r.get("artworkUrl100") or ""
                if not art or "100x100" not in art:
                    continue
                if _norm(r.get("collectionName", "")) == want and _same_artist(artist, r.get("artistName", "")):
                    return {
                        "large": art.replace("100x100bb", "1200x1200bb"),
                        "full": art.replace("100x100bb", "3000x3000bb"),
                        "source": "itunes",
                    }
    return None


async def _deezer_art(artist: str, album: str):
    want = _norm(album)
    async with httpx.AsyncClient(timeout=8.0) as client:
        for q in (f'artist:"{artist}" album:"{album}"', f"{artist} {album}"):
            res = await client.get(DEEZER_SEARCH, params={"q": q, "limit": 15})
            if res.status_code != 200:
                return None
            for r in res.json().get("data", []) or []:
                xl = r.get("cover_xl") or ""
                if not xl or "1000x1000" not in xl:
                    continue
                if _norm(r.get("title", "")) == want and _same_artist(artist, (r.get("artist") or {}).get("name", "")):
                    return {"large": xl, "full": xl.replace("1000x1000", "1400x1400"), "source": "deezer"}
    return None


async def _caa_art(mbid: str):
    url = f"{COVER_ART_BASE}/release/{mbid}/front-1200"
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
        res = await client.head(url, headers=MB_HEADERS)
    if res.status_code in (200, 301, 302, 307, 308):
        return {"large": url, "full": f"{COVER_ART_BASE}/release/{mbid}/front", "source": "coverartarchive"}
    return None


@router.get("/api/music-requests/artwork")
async def hires_artwork(request: Request, artist: str = "", album: str = "", mbid: str = ""):
    if get_username(request) == "anonymous":
        return _unauthenticated()
    artist, album = artist.strip()[:200], album.strip()[:200]
    mbid = mbid.strip() if re.fullmatch(r"[0-9a-fA-F-]{36}", mbid.strip() or "") else ""
    if not (album and artist) and not mbid:
        return JSONResponse(status_code=400, content={"error": "artist and album, or mbid"})
    key = f"{_first_artist(artist)}|{_norm(album)}|{mbid.lower()}"

    with get_db() as db:
        row = db.execute(
            "SELECT large, full, source, julianday('now') - julianday(checked) AS age FROM art_cache WHERE key=?", (key,)
        ).fetchone()
    # source "none": looked, nothing confident. (null: couldn't look.)
    if row and (row["large"] or row["age"] < _ART_MISS_RETRY_DAYS):
        return _art_answer(key, row["large"], row["full"], row["source"] or "none")

    found = None
    try:
        if artist and album:
            found = await _itunes_art(artist, album)
        if not found and artist and album:
            found = await _deezer_art(artist, album)
        if not found and mbid:
            found = await _caa_art(mbid)
    except (httpx.HTTPError, ValueError):
        # The catalogue being unreachable isn't "no art": don't cache it.
        return {"large": None, "full": None, "source": None}

    with get_db() as db:
        db.execute(
            """INSERT INTO art_cache(key, large, full, source, checked, id) VALUES (?, ?, ?, ?, datetime('now'), ?)
               ON CONFLICT(key) DO UPDATE SET large=excluded.large, full=excluded.full,
                   source=excluded.source, checked=excluded.checked, id=excluded.id""",
            (key, (found or {}).get("large", ""), (found or {}).get("full", ""), (found or {}).get("source", ""), art_id(key)),
        )
        db.commit()
    if not found:
        return {"large": None, "full": None, "source": "none"}
    return _art_answer(key, found["large"], found["full"], found["source"])


# ── Keeping the art ──
# The images are kept on the media share, fetched from the catalogue the
# first time anyone asks for one, so browsers only ever load them from
# here: nobody's browser talks to Apple or Deezer, and art already kept
# stays up if a catalogue drops it. Without the share, the catalogue's
# own links are handed out instead.

_ART_HOSTS = ("mzstatic.com", "dzcdn.net", "coverartarchive.org", "archive.org")
_ART_MAX_BYTES = 25 * 1024 * 1024
_ART_FILE_RE = re.compile(r"^([0-9a-f]{20})-([lf])$")
_ART_RETRY_SECS = 3600
_art_fetching: dict = {}
_art_failed: dict = {}  # file name -> when fetching it last failed


def _art_answer(key: str, large: str, full: str, source: str) -> dict:
    if not large:
        return {"large": None, "full": None, "source": source}
    if not media_ready():
        return {"large": large, "full": full or large, "source": source}
    base = f"/api/music-requests/artwork/files/{art_id(key)}"
    return {"large": base + "-l", "full": base + "-f", "source": source}


def _art_dir() -> str:
    d = os.path.join(MEDIA_DIR, "art")
    os.makedirs(d, exist_ok=True)
    return d


def _art_type(head: bytes):
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _art_host_ok(url: str) -> bool:
    u = urlparse(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in _ART_HOSTS)


async def _art_download(url: str, path: str) -> bool:
    """Fetch one image from a catalogue into path. Only ever goes to the
    catalogues' own image hosts, every redirect included."""
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False, headers={"User-Agent": MUSICBRAINZ_USER_AGENT}) as client:
        for _ in range(5):
            if not _art_host_ok(url):
                return False
            async with client.stream("GET", url) as res:
                if res.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, res.headers.get("location", ""))
                    continue
                if res.status_code != 200:
                    return False
                data = bytearray()
                async for chunk in res.aiter_bytes():
                    data += chunk
                    if len(data) > _ART_MAX_BYTES:
                        return False
            if not _art_type(bytes(data[:12])):
                return False
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            return True
    return False


async def _art_fetch(name: str, url: str, path: str) -> bool:
    """Download one file, once however many people ask at the same time,
    and not again for a while if the catalogue wouldn't give it."""
    if time.time() - _art_failed.get(name, 0) < _ART_RETRY_SECS:
        return False
    task = _art_fetching.get(name)
    if task is None:
        task = _art_fetching[name] = asyncio.ensure_future(_art_download(url, path))
        task.add_done_callback(lambda _t: _art_fetching.pop(name, None))
    try:
        ok = await asyncio.shield(task)
    except (httpx.HTTPError, OSError):
        ok = False
    if not ok:
        _art_failed[name] = time.time()
    return ok


@router.get("/api/music-requests/artwork/files/{name}")
async def hires_artwork_file(name: str):
    m = _ART_FILE_RE.match(name)
    if not m or not media_ready():
        return JSONResponse(status_code=404, content={"error": "not found"})
    aid, size = m.groups()
    d = _art_dir()
    path = os.path.join(d, name)
    if not os.path.isfile(path):
        with get_db() as db:
            row = db.execute("SELECT large, full FROM art_cache WHERE id=? AND large != ''", (aid,)).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "not found"})
        ok = await _art_fetch(name, row["large"] if size == "l" else (row["full"] or row["large"]), path)
        if not ok and size == "f":
            # No full-size copy to be had: the large one will do.
            path = os.path.join(d, aid + "-l")
            ok = os.path.isfile(path) or await _art_fetch(aid + "-l", row["large"], path)
        if not ok:
            return JSONResponse(status_code=502, content={"error": "couldn't get the artwork"})
    with open(path, "rb") as f:
        media_type = _art_type(f.read(12)) or "application/octet-stream"
    # A file only ever holds one album's art.
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=31536000, immutable"})
