"""TV channels: a lineup of videos on a loop that everyone watching sees
at the same moment, like the radio (routers/radio.py) but with pictures.

A channel is its videos in order (tv_items) and a start time (`epoch`).
Where it's at is (now - epoch) mod the lineup's length, on this server's
clock (GET …/tv/time), so a page plays the video that's on from the point
it's at and keeps itself on that line. Only videos that are ready count:
one still being made (or that failed) is skipped until it's done, and the
loop is shorter meanwhile. Changing the lineup keeps the start time.

There are two ways to fill one, and a channel can mix them: string
together videos already in Videos, or upload a file straight into the
channel (POST …/upload). That one goes through Videos' own pipeline
(routers/videos.py create) but is unlisted: it plays on the channel and
stays out of Videos' lists, and it goes when the channel does.

Unlike the radio's surprise tracklist, everyone gets the whole lineup: a
TV guide says what's next. Anyone signed in can make a channel; whoever
made it, its author, or an admin edits or deletes it. Covers reuse
playlists' (routers/playlists.py). Under /api/posts/tv because Caddy
forwards /api/posts/*."""
import re
import secrets
import time

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from ..auth import get_groups, get_username, is_admin
from ..db import get_db
from ..media import media_ready
from . import videos
from .playlists import _MAX_COVER_BYTES, _cover_slots, _delete_cover_files, _write_cover

router = APIRouter()
BASE = "/api/posts/tv"

_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
NUMBER_MIN, NUMBER_MAX = 1, 99
LINEUP_MAX = 500


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _signed_in(request: Request) -> str | None:
    u = get_username(request)
    return None if u == "anonymous" else u


def _author(value) -> str:
    return str(value or "").strip().lstrip("@")[:60]


def _can_edit(r, request: Request, username: str) -> bool:
    return username in (r["created_by"], r["author"]) or is_admin(get_groups(request))


def _number(value, db, channel_id: str = "") -> int | JSONResponse:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return _error(400, "a channel needs a number, like 4")
    if not NUMBER_MIN <= n <= NUMBER_MAX:
        return _error(400, f"channel numbers go from {NUMBER_MIN} to {NUMBER_MAX}")
    taken = db.execute("SELECT name FROM tv_channels WHERE number=? AND id != ?", (n, channel_id)).fetchone()
    if taken:
        return _error(409, f"{n} is taken by {taken['name']}")
    return n


def _video_ids(raw) -> list[int] | JSONResponse:
    if not isinstance(raw, list):
        return _error(400, "videos is a list of video ids")
    try:
        ids = [int(v) for v in raw][:LINEUP_MAX]
    except (TypeError, ValueError):
        return _error(400, "videos is a list of video ids")
    return ids


def _set_lineup(db, channel_id: str, ids: list[int]) -> None:
    """The lineup, in this order: only videos that exist (one can be in it
    twice)."""
    known = set()
    if ids:
        marks = ",".join("?" * len(set(ids)))
        known = {r[0] for r in db.execute(f"SELECT post_id FROM videos WHERE post_id IN ({marks})", list(set(ids)))}
    db.execute("DELETE FROM tv_items WHERE channel_id=?", (channel_id,))
    db.executemany("INSERT INTO tv_items(channel_id, pos, video_id) VALUES (?, ?, ?)",
                   [(channel_id, i, v) for i, v in enumerate([v for v in ids if v in known])])


def _lineups(db, channel_ids: list[str]) -> dict[str, list[dict]]:
    """Each channel's lineup in order, every item with what a player needs."""
    out: dict[str, list[dict]] = {c: [] for c in channel_ids}
    if not channel_ids:
        return out
    marks = ",".join("?" * len(channel_ids))
    rows = db.execute(f"""SELECT t.channel_id, t.pos, v.post_id, v.title, v.file_id, v.status, v.progress, v.poster,
                                 v.storyboard, v.duration, v.unlisted, p.username
                          FROM tv_items t JOIN videos v ON v.post_id = t.video_id JOIN posts p ON p.id = v.post_id
                          WHERE t.channel_id IN ({marks}) ORDER BY t.channel_id, t.pos""", channel_ids).fetchall()
    for r in rows:
        u = videos._urls(r)
        out[r["channel_id"]].append({
            "id": r["post_id"], "title": r["title"], "username": r["username"], "duration": r["duration"],
            "status": r["status"], "progress": r["progress"], "hls": u["hls"], "poster": u["poster"],
            "posterSmall": u["posterSmall"], "unlisted": bool(r["unlisted"]),
        })
    return out


def _playable(item: dict) -> bool:
    return item["status"] == "ready" and bool(item["hls"]) and (item["duration"] or 0) > 0.5


def _now_next(items: list[dict], epoch: float, at: float) -> dict:
    """What's on at `at` (and how far in), and what's after it."""
    sched = [i for i in items if _playable(i)]
    total = sum(i["duration"] for i in sched)
    if not total:
        return {"total": 0, "now": None, "next": None}
    pos = (at - epoch) % total
    start = 0.0
    for k, item in enumerate(sched):
        if pos < start + item["duration"] or k == len(sched) - 1:
            nxt = sched[(k + 1) % len(sched)]
            return {
                "total": total,
                "now": {"id": item["id"], "title": item["title"], "posterSmall": item["posterSmall"],
                        "start": round(start, 3), "duration": item["duration"], "at": round(pos - start, 2)},
                "next": {"id": nxt["id"], "title": nxt["title"], "posterSmall": nxt["posterSmall"],
                         "in": round(start + item["duration"] - pos, 1)},
            }
        start += item["duration"]
    return {"total": total, "now": None, "next": None}


def _watchers() -> dict[str, list[str]]:
    """Who's watching what, from presence's beats (the device each person
    used last). Invisible people aren't counted."""
    from .presence import _beats, _latest
    out: dict[str, list[str]] = {}
    for username in list(_beats):
        b = _latest(username)
        if b and b.get("tv") and b.get("status") != "invisible" and time.time() - b.get("at", 0) < 60:
            out.setdefault(b["tv"], []).append(username)
    return out


def _json(r, items: list[dict], request: Request, username: str, watchers: dict | None = None, full: bool = False) -> dict:
    t = time.time()
    nn = _now_next(items, r["epoch"], t)
    can_edit = _can_edit(r, request, username)
    sched = [i for i in items if _playable(i)]
    out = {
        "id": r["id"], "name": r["name"], "number": r["number"], "color": r["color"] or None,
        "description": r["description"], "cover": r["cover"] or None,
        "author": r["author"] or r["created_by"], "createdBy": r["created_by"],
        "epoch": r["epoch"], "total": nn["total"], "onNow": nn["now"], "upNext": nn["next"],
        # The schedule: what plays, in order (ready videos only).
        "items": sched,
        "count": len(items),
        "canEdit": can_edit,
        "watchers": (watchers or {}).get(r["id"], []),
        "created": r["created"],
    }
    if full or can_edit:
        # Everything in the lineup, ready or not, for the editor.
        out["lineup"] = items
    return out


def _load(db, channel_id: str):
    return db.execute("SELECT * FROM tv_channels WHERE id=?", (channel_id,)).fetchone()


def _editable(channel_id: str, request: Request, username: str):
    with get_db() as db:
        row = _load(db, channel_id)
    if row is None:
        return None, _error(404, "not found")
    if not _can_edit(row, request, username):
        return None, _error(403, "not your channel")
    return row, None


def _out(channel_id: str, request: Request, username: str) -> dict:
    with get_db() as db:
        row = _load(db, channel_id)
        items = _lineups(db, [channel_id])[channel_id]
    return _json(row, items, request, username, _watchers(), full=True)


async def _body(request: Request) -> dict | JSONResponse:
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
    except Exception:
        return _error(400, "invalid JSON")
    return body


# ── Routes ──

@router.get(f"{BASE}/time")
def server_time():
    """This server's clock, which every page keeps its channel against."""
    return {"now": time.time()}


@router.get(BASE)
def list_channels(request: Request):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    watchers = _watchers()
    with get_db() as db:
        rows = db.execute("SELECT * FROM tv_channels ORDER BY number").fetchall()
        lineups = _lineups(db, [r["id"] for r in rows])
    return {"now": time.time(), "channels": [_json(r, lineups[r["id"]], request, username, watchers) for r in rows]}


@router.get(f"{BASE}/{{channel_id}}")
def get_channel(channel_id: str, request: Request):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    with get_db() as db:
        row = _load(db, channel_id)
        if row is None:
            # A channel by its number, too (#videos/tv/4).
            row = db.execute("SELECT * FROM tv_channels WHERE number=?", (int(channel_id),)).fetchone() if channel_id.isdigit() else None
        if row is None:
            return _error(404, "not found")
        items = _lineups(db, [row["id"]])[row["id"]]
    return {"now": time.time(), **_json(row, items, request, username, _watchers(), full=True)}


@router.post(BASE)
async def create_channel(request: Request):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    name = " ".join(str(body.get("name") or "").split())[:60]
    if not name:
        return _error(400, "a channel needs a name")
    ids = _video_ids(body.get("videos") or [])
    if isinstance(ids, JSONResponse):
        return ids
    color = str(body.get("color") or "")
    author = _author(body.get("author"))
    channel_id = secrets.token_urlsafe(9)
    with get_db() as db:
        number = _number(body.get("number"), db)
        if isinstance(number, JSONResponse):
            return number
        db.execute("""INSERT INTO tv_channels(id, name, number, color, description, author, created_by, epoch)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                   (channel_id, name, number, color if _COLOR_RE.fullmatch(color) else "",
                    str(body.get("description") or "").strip()[:300], "" if author == username else author,
                    username, round(time.time(), 3)))
        _set_lineup(db, channel_id, ids)
        db.commit()
    return _out(channel_id, request, username)


@router.patch(f"{BASE}/{{channel_id}}")
async def edit_channel(channel_id: str, request: Request):
    """Name, number, colour, description, author, and the lineup (`videos`,
    the whole list in order). The start time stays."""
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    row, err = _editable(channel_id, request, username)
    if err:
        return err
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    sets, args = [], []
    if "name" in body:
        name = " ".join(str(body["name"] or "").split())[:60]
        if not name:
            return _error(400, "a channel needs a name")
        sets.append("name=?"); args.append(name)
    if "description" in body:
        sets.append("description=?"); args.append(str(body["description"] or "").strip()[:300])
    if "color" in body:
        c = str(body["color"] or "")
        sets.append("color=?"); args.append(c if _COLOR_RE.fullmatch(c) else "")
    if "author" in body:
        a = _author(body["author"])
        sets.append("author=?"); args.append("" if a == row["created_by"] else a)
    ids = None
    if "videos" in body:
        ids = _video_ids(body["videos"])
        if isinstance(ids, JSONResponse):
            return ids
    with get_db() as db:
        if "number" in body:
            number = _number(body["number"], db, channel_id)
            if isinstance(number, JSONResponse):
                return number
            sets.append("number=?"); args.append(number)
        if sets:
            db.execute(f"UPDATE tv_channels SET {', '.join(sets)}, updated=datetime('now') WHERE id=?", (*args, channel_id))
        if ids is not None:
            _set_lineup(db, channel_id, ids)
        db.commit()
    return _out(channel_id, request, username)


@router.post(f"{BASE}/{{channel_id}}/items")
async def add_item(channel_id: str, request: Request):
    """One video onto the end of the lineup."""
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    row, err = _editable(channel_id, request, username)
    if err:
        return err
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    try:
        vid = int(body.get("video"))
    except (TypeError, ValueError):
        return _error(400, "video (an id) required")
    with get_db() as db:
        if db.execute("SELECT 1 FROM videos WHERE post_id=?", (vid,)).fetchone() is None:
            return _error(404, "no such video")
        pos = db.execute("SELECT COALESCE(MAX(pos), -1) + 1 FROM tv_items WHERE channel_id=?", (channel_id,)).fetchone()[0]
        if pos >= LINEUP_MAX:
            return _error(400, f"a channel can hold {LINEUP_MAX} videos")
        db.execute("INSERT INTO tv_items(channel_id, pos, video_id) VALUES (?, ?, ?)", (channel_id, pos, vid))
        db.commit()
    return _out(channel_id, request, username)


@router.put(f"{BASE}/{{channel_id}}/items")
async def set_items(channel_id: str, request: Request):
    """The whole lineup in its new order (reordering and removing)."""
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    row, err = _editable(channel_id, request, username)
    if err:
        return err
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    ids = _video_ids(body.get("videos"))
    if isinstance(ids, JSONResponse):
        return ids
    with get_db() as db:
        _set_lineup(db, channel_id, ids)
        db.commit()
    return _out(channel_id, request, username)


@router.delete(f"{BASE}/{{channel_id}}/items/{{pos}}")
def remove_item(channel_id: str, pos: int, request: Request):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    row, err = _editable(channel_id, request, username)
    if err:
        return err
    with get_db() as db:
        ids = [r[0] for r in db.execute("SELECT video_id FROM tv_items WHERE channel_id=? ORDER BY pos", (channel_id,))]
        if 0 <= pos < len(ids):
            del ids[pos]
            _set_lineup(db, channel_id, ids)
            db.commit()
    return _out(channel_id, request, username)


@router.post(f"{BASE}/{{channel_id}}/upload")
def upload_into(channel_id: str, request: Request, file: UploadFile = File(...), title: str = Form("")):
    """A file straight into the channel: made like any video, but unlisted,
    and put on the end of the lineup (a new channel's only item)."""
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    row, err = _editable(channel_id, request, username)
    if err:
        return err
    post_id = videos.create(request, username, file, title or row["name"], "", unlisted=True)
    if isinstance(post_id, JSONResponse):
        return post_id
    with get_db() as db:
        pos = db.execute("SELECT COALESCE(MAX(pos), -1) + 1 FROM tv_items WHERE channel_id=?", (channel_id,)).fetchone()[0]
        db.execute("INSERT INTO tv_items(channel_id, pos, video_id) VALUES (?, ?, ?)", (channel_id, pos, post_id))
        db.commit()
    return _out(channel_id, request, username)


@router.post(f"{BASE}/{{channel_id}}/cover")
def upload_cover(channel_id: str, request: Request, file: UploadFile = File(...)):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    row, err = _editable(channel_id, request, username)
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
        db.execute("UPDATE tv_channels SET cover=?, updated=datetime('now') WHERE id=?", (cover_id, channel_id))
        db.commit()
    _delete_cover_files(row["cover"])
    return _out(channel_id, request, username)


@router.delete(f"{BASE}/{{channel_id}}/cover")
def remove_cover(channel_id: str, request: Request):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    row, err = _editable(channel_id, request, username)
    if err:
        return err
    with get_db() as db:
        db.execute("UPDATE tv_channels SET cover='', updated=datetime('now') WHERE id=?", (channel_id,))
        db.commit()
    _delete_cover_files(row["cover"])
    return _out(channel_id, request, username)


@router.delete(f"{BASE}/{{channel_id}}")
def delete_channel(channel_id: str, request: Request):
    """The channel goes; the videos it strung together stay in Videos. Its
    unlisted ones (uploaded straight into it) go with it, unless another
    channel plays them too."""
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    row, err = _editable(channel_id, request, username)
    if err:
        return err
    gone: list[str] = []
    with get_db() as db:
        own = [r[0] for r in db.execute(
            """SELECT DISTINCT t.video_id FROM tv_items t JOIN videos v ON v.post_id = t.video_id
               WHERE t.channel_id=? AND v.unlisted=1
                 AND NOT EXISTS (SELECT 1 FROM tv_items o WHERE o.video_id = t.video_id AND o.channel_id != t.channel_id)""",
            (channel_id,))]
        db.execute("DELETE FROM tv_items WHERE channel_id=?", (channel_id,))
        db.execute("DELETE FROM tv_channels WHERE id=?", (channel_id,))
        for post_id in own:
            file_id = videos.delete_files_for(post_id, db)
            for table in ("post_tags", "post_reactions", "post_replies"):
                db.execute(f"DELETE FROM {table} WHERE post_id=?", (post_id,))
            db.execute("DELETE FROM posts WHERE id=?", (post_id,))
            if file_id:
                gone.append(file_id)
        db.commit()
    for file_id in gone:
        videos._remove_folder(file_id)
    _delete_cover_files(row["cover"])
    return {"ok": True}
