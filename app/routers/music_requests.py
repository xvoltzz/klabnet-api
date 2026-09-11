"""Music library requests — "please add this album/song", with MusicBrainz
search so a request comes with real title/artist/cover-art metadata instead
of a freeform text box.

The MusicBrainz calls happen here (server-side), not from the browser:
their API requires a real identifying User-Agent and is unauthenticated-rate-
limited to ~1 req/sec, both easier to get right in one place than from N
browser tabs, and it sidesteps any CORS question entirely.
"""

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_groups, get_username, is_admin
from ..config import MUSICBRAINZ_USER_AGENT, REQUEST_FIELD_MAX_CHARS
from ..db import get_db

router = APIRouter()

MB_BASE = "https://musicbrainz.org/ws/2"
MB_HEADERS = {"User-Agent": MUSICBRAINZ_USER_AGENT, "Accept": "application/json"}
COVER_ART_BASE = "https://coverartarchive.org"


def _unauthenticated() -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": "not authenticated"})


def _artist_credit(credits: list) -> str:
    """MusicBrainz's artist-credit is a list of {name, joinphrase} pairs
    (handles "feat."/"&" collabs) — this flattens it to a plain display
    string the way the actual release title bar would read."""
    return "".join(f"{c.get('name', '')}{c.get('joinphrase', '')}" for c in credits or [])


@router.get("/api/music-requests/search")
async def search_musicbrainz(q: str, type: str = "album"):
    """Proxies a MusicBrainz search so the request form can show real
    matches (with cover art) to pick from instead of a blind text field."""
    q = q.strip()
    if not q:
        return {"results": []}
    entity = "recording" if type == "song" else "release-group"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.get(
                f"{MB_BASE}/{entity}",
                params={"query": q, "fmt": "json", "limit": 8},
                headers=MB_HEADERS,
            )
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
