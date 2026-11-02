"""Lyrics, from LRCLIB (lrclib.net), for everyone at once.

The site used to ask LRCLIB from each browser. LRCLIB rate-limits by IP
(429 with a Retry-After, sometimes a 503), and everyone at home shares
one, so a few songs played around the house used up the allowance and
lyrics "couldn't be reached" for a while. Here they're asked once, kept
for everyone (lyrics_cache; a song with none is asked again after
MISS_DAYS), asked one at a time a little apart, and a 429/503 is waited
out (its Retry-After, briefly) and tried again.

The lookup is the site's: an exact match on title, artist, album and
length; failing that, a search by title and artist, synced lyrics first,
then the nearest length (within 8s). Under /api/music-requests/ because
Caddy only forwards a fixed list of /api prefixes."""
import asyncio
import json
import time
import unicodedata

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_username
from ..config import MUSICBRAINZ_USER_AGENT
from ..db import get_db

router = APIRouter()

BASE = "https://lrclib.net/api"
HEADERS = {"User-Agent": MUSICBRAINZ_USER_AGENT, "Accept": "application/json"}
MISS_DAYS = 3
_MIN_INTERVAL = 0.35
_RETRIES = 3
_MAX_WAIT = 4.0
_lock = asyncio.Lock()
_last = 0.0


class _Busy(Exception):
    pass


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return " ".join(s.split())


def _key(title: str, artist: str, album: str, duration: int) -> str:
    return "\x1f".join([_norm(title), _norm(artist), _norm(album), str(duration)])


async def _get(client: httpx.AsyncClient, path: str, params: dict) -> httpx.Response:
    """One request to LRCLIB, spaced from the last, its rate limit waited out."""
    global _last
    async with _lock:
        for attempt in range(_RETRIES + 1):
            wait = _MIN_INTERVAL - (time.monotonic() - _last)
            if wait > 0:
                await asyncio.sleep(wait)
            res = await client.get(f"{BASE}/{path}", params=params, headers=HEADERS)
            _last = time.monotonic()
            if res.status_code not in (429, 503):
                return res
            if attempt == _RETRIES:
                break
            try:
                after = float(res.headers.get("retry-after", "1"))
            except ValueError:
                after = 1.0
            await asyncio.sleep(min(_MAX_WAIT, max(0.5, after)) * (1 + attempt * 0.5))
    raise _Busy()


async def _lookup(title: str, artist: str, album: str, duration: int):
    async with httpx.AsyncClient(timeout=8.0) as client:
        params = {"track_name": title, "artist_name": artist, "album_name": album}
        if duration:
            params["duration"] = str(duration)
        res = await _get(client, "get", params)
        if res.status_code == 200:
            return res.json()
        if res.status_code != 404:
            raise _Busy()
        sr = await _get(client, "search", {"track_name": title, "artist_name": artist})
        if sr.status_code != 200:
            raise _Busy()
        hits = [h for h in (sr.json() or []) if h.get("syncedLyrics") or h.get("plainLyrics")]
        off = lambda h: abs((h.get("duration") or 0) - duration) if duration and h.get("duration") else 0
        near = [h for h in hits if off(h) <= 8]
        near.sort(key=lambda h: (not h.get("syncedLyrics"), off(h)))
        return near[0] if near else None


def _cached(key: str):
    with get_db() as db:
        return db.execute(
            "SELECT body, julianday('now') - julianday(checked) AS age FROM lyrics_cache WHERE key=?", (key,)
        ).fetchone()


def _store(key: str, body: str):
    with get_db() as db:
        db.execute(
            """INSERT INTO lyrics_cache(key, body, checked) VALUES (?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET body=excluded.body, checked=excluded.checked""",
            (key, body),
        )
        db.commit()


@router.get("/api/music-requests/lyrics")
async def lyrics(request: Request, title: str = "", artist: str = "", album: str = "", duration: float = 0):
    """{"lyrics": {syncedLyrics, plainLyrics, …}} or {"lyrics": null} for
    none; 503 {"error": "busy"} when LRCLIB couldn't be reached."""
    if get_username(request) == "anonymous":
        return JSONResponse(status_code=401, content={"error": "not authenticated"})
    title, artist, album = title.strip()[:300], artist.strip()[:300], album.strip()[:300]
    if not title:
        return {"lyrics": None}
    dur = int(round(duration or 0))
    key = _key(title, artist, album, dur)
    row = await asyncio.to_thread(_cached, key)
    if row and (row["body"] or row["age"] < MISS_DAYS):
        return {"lyrics": json.loads(row["body"]) if row["body"] else None, "cached": True}
    try:
        found = await _lookup(title, artist, album, dur)
    except (_Busy, httpx.HTTPError, ValueError):
        return JSONResponse(status_code=503, content={"error": "busy"})
    keep = None
    if found:
        keep = {k: found.get(k) for k in ("id", "trackName", "artistName", "albumName", "duration", "instrumental", "plainLyrics", "syncedLyrics")}
    await asyncio.to_thread(_store, key, json.dumps(keep) if keep else "")
    return {"lyrics": keep}
