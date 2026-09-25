"""The GIF stash: klabnet's own GIFs, for the picker in chat and the feed.

No GIF service account: people add GIFs by pasting a link (a giphy.com or
tenor.com page, or any direct .gif) or uploading one, and the server keeps
its own copy on the media share, so a GIF never breaks when the link does.
The picker searches the stash by name, most-used first, so it grows into
the group's own reaction library.

Fetching a pasted link happens here, from inside the home network, so it
only ever goes to public addresses: every hop of a redirect is checked,
and anything resolving to a private, loopback or link-local address is
refused.
"""

import hashlib
import io
import ipaddress
import os
import re
import socket
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

import httpx
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image

from ..auth import get_groups, get_username, is_admin
from ..config import GIF_MAX_MB, MEDIA_DIR
from ..db import get_db
from ..media import media_ready

router = APIRouter()

_MAX_BYTES = GIF_MAX_MB * 1024 * 1024
_PAGE = 30
_NAME_MAX = 80
_TIMEOUT = 10.0
_ID_RE = re.compile(r"^[0-9a-f]{20}$")
_UA = "Mozilla/5.0 (compatible; klabnet-gifs/1.0; +https://klab.gg)"


class GifError(Exception):
    pass


def _dir() -> str:
    d = os.path.join(MEDIA_DIR, "gifs")
    os.makedirs(d, exist_ok=True)
    return d


def _path(gif_id: str) -> str:
    return os.path.join(_dir(), gif_id + ".gif")


def _unauthenticated() -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": "not authenticated"})


def _shape(r) -> dict:
    return {
        "id": r["id"], "name": r["name"], "by": r["username"],
        "url": f"/api/gifs/files/{r['id']}.gif", "w": r["width"], "h": r["height"],
    }


def _clean_name(name: str) -> str:
    name = re.sub(r"[-_]+", " ", name or "")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:_NAME_MAX]


# ── Fetching a pasted link, safely ──

def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            return False
    return bool(infos)


async def _fetch(client: httpx.AsyncClient, url: str, accept: str = "image/gif,text/html;q=0.9,*/*;q=0.5") -> tuple[str, str, bytes]:
    """GET url, following up to 5 redirects by hand so each hop's host is
    checked. Returns (final url, content type, body), body capped."""
    for _ in range(6):
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname or (u.port and u.port not in (80, 443)):
            raise GifError("that link doesn't look right")
        if not _public_host(u.hostname):
            raise GifError("that link doesn't look right")
        async with client.stream("GET", url, headers={"User-Agent": _UA, "Accept": accept}) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            if r.status_code != 200:
                raise GifError("couldn't fetch that link")
            body = bytearray()
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > _MAX_BYTES:
                    raise GifError(f"GIFs can be up to {GIF_MAX_MB}MB")
            return url, r.headers.get("content-type", "").split(";")[0].strip().lower(), bytes(body)
    raise GifError("that link redirects too much")


_GIPHY_ID = re.compile(r"giphy\.com/(?:gifs/(?:[\w-]*-)?|media/(?:v1\.[^/]+/)?|embed/)([A-Za-z0-9]{6,})")


def _meta(html: str, *names: str) -> str:
    for n in names:
        m = re.search(r'<meta[^>]+(?:property|name)=["\']' + re.escape(n) + r'["\'][^>]*content=["\']([^"\']+)', html, re.I) \
            or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']' + re.escape(n) + r'["\']', html, re.I)
        if m:
            return unescape(m.group(1))
    return ""


async def _gif_from_link(url: str) -> tuple[bytes, str]:
    """The GIF behind a pasted link, and a name guessed from it."""
    url = url.strip()
    guess = ""
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        m = _GIPHY_ID.search(url)
        if m:
            # Any GIPHY page, embed or media link: straight to the file.
            slug = re.search(r"giphy\.com/gifs/([\w-]+)-" + m.group(1), url)
            guess = slug.group(1) if slug else ""
            url = f"https://i.giphy.com/{m.group(1)}.gif"
        is_file = urlparse(url).path.lower().endswith(".gif")
        final, ctype, body = await _fetch(client, url, accept="image/gif,image/*;q=0.8" if is_file else "image/gif,text/html;q=0.9,*/*;q=0.5")
        if ctype.startswith("text/html"):
            # A page (Tenor, imgur, anything with a preview): its share image.
            html = body[:400_000].decode("utf-8", "replace")
            guess = guess or _meta(html, "og:title", "twitter:title")
            img = ""
            for cand in (_meta(html, "og:image"), _meta(html, "twitter:image"), _meta(html, "og:image:url")):
                if cand and ".gif" in cand.lower():
                    img = cand
                    break
            if not img:
                raise GifError("couldn't find a GIF on that page")
            # Images only: Tenor answers an Accept that allows HTML with a
            # wrapper page instead of the GIF.
            final, ctype, body = await _fetch(client, urljoin(final, img), accept="image/gif,image/*;q=0.8")
        if not guess:
            guess = unquote(os.path.splitext(os.path.basename(urlparse(final).path))[0])
    guess = re.sub(r"\s*(-\s*)?(GIF|Discover & Share GIFs|on GIPHY|Tenor).*$", "", guess, flags=re.I)
    return body, guess


def _store(data: bytes, username: str, name: str) -> dict:
    if not data:
        raise GifError("that file is empty")
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format != "GIF":
                raise GifError("that isn't a GIF")
            w, h = im.size
    except GifError:
        raise
    except Exception:
        raise GifError("that isn't a GIF")
    gif_id = hashlib.sha256(data).hexdigest()[:20]
    with get_db() as db:
        row = db.execute("SELECT * FROM gifs WHERE id=?", (gif_id,)).fetchone()
        if row:
            # Already stashed: the name fills in if it had none.
            if name and not row["name"]:
                db.execute("UPDATE gifs SET name=? WHERE id=?", (name, gif_id))
                db.commit()
                row = db.execute("SELECT * FROM gifs WHERE id=?", (gif_id,)).fetchone()
            return _shape(row)
        path = _path(gif_id)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        db.execute(
            "INSERT INTO gifs(id, username, name, width, height, bytes) VALUES (?, ?, ?, ?, ?, ?)",
            (gif_id, username, name, w, h, len(data)),
        )
        db.commit()
        return _shape(db.execute("SELECT * FROM gifs WHERE id=?", (gif_id,)).fetchone())


# ── Routes ──

@router.get("/api/gifs")
def list_gifs(request: Request, q: str = "", offset: int = 0):
    if get_username(request) == "anonymous":
        return _unauthenticated()
    offset = max(0, int(offset))
    words = [w for w in re.split(r"\s+", q.strip().lower()) if w][:6]
    where = " AND ".join("lower(name) LIKE ?" for _ in words) or "1=1"
    args = [f"%{w}%" for w in words]
    with get_db() as db:
        rows = db.execute(
            f"SELECT * FROM gifs WHERE {where} ORDER BY uses DESC, COALESCE(last_used, created) DESC LIMIT ? OFFSET ?",
            (*args, _PAGE + 1, offset),
        ).fetchall()
        total = db.execute("SELECT COUNT(*) FROM gifs").fetchone()[0]
    more = len(rows) > _PAGE
    return {"items": [_shape(r) for r in rows[:_PAGE]], "next": offset + _PAGE if more else None, "total": total}


@router.post("/api/gifs/link")
async def add_gif_link(request: Request):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    if not media_ready():
        return JSONResponse(status_code=503, content={"error": "storage is offline"})
    try:
        body = await request.json()
    except Exception:
        body = {}
    url = str(body.get("url") or "")[:2000]
    if not url:
        return JSONResponse(status_code=400, content={"error": "paste a link to a GIF"})
    try:
        data, guess = await _gif_from_link(url)
        return _store(data, username, _clean_name(str(body.get("name") or "") or guess))
    except GifError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except httpx.HTTPError:
        return JSONResponse(status_code=400, content={"error": "couldn't fetch that link"})


@router.post("/api/gifs/upload")
def add_gif_upload(request: Request, file: UploadFile = File(...), name: str = Form("")):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    if not media_ready():
        return JSONResponse(status_code=503, content={"error": "storage is offline"})
    data = file.file.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        return JSONResponse(status_code=413, content={"error": f"GIFs can be up to {GIF_MAX_MB}MB"})
    try:
        return _store(data, username, _clean_name(name or os.path.splitext(file.filename or "")[0]))
    except GifError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.post("/api/gifs/{gif_id}/used")
def gif_used(gif_id: str, request: Request):
    if get_username(request) == "anonymous":
        return _unauthenticated()
    with get_db() as db:
        db.execute("UPDATE gifs SET uses = uses + 1, last_used = datetime('now') WHERE id=?", (gif_id,))
        db.commit()
    return {"ok": True}


@router.delete("/api/gifs/{gif_id}")
def delete_gif(gif_id: str, request: Request):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    with get_db() as db:
        row = db.execute("SELECT username FROM gifs WHERE id=?", (gif_id,)).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "not found"})
        if row["username"] != username and not is_admin(get_groups(request)):
            return JSONResponse(status_code=403, content={"error": "that's someone else's GIF"})
        db.execute("DELETE FROM gifs WHERE id=?", (gif_id,))
        db.commit()
    # The file stays: GIFs already posted in chat or the feed point at it.
    return {"ok": True}


@router.get("/api/gifs/files/{name}")
def gif_file(name: str):
    gif_id = name[:-4] if name.endswith(".gif") else name
    if not _ID_RE.match(gif_id) or not media_ready():
        return JSONResponse(status_code=404, content={"error": "not found"})
    path = _path(gif_id)
    if not os.path.isfile(path):
        return JSONResponse(status_code=404, content={"error": "not found"})
    # A GIF's id is its content hash, so the file never changes.
    return FileResponse(path, media_type="image/gif", headers={"Cache-Control": "private, max-age=31536000, immutable"})
