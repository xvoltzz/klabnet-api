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
forwards /api/posts/*.

Programs (the guide): anyone can schedule a block of videos on any
channel at a set start, into time nothing else holds ("movie at 9 on 3").
While one is on, the channel plays it from its start, everyone at the same
point; outside programs the loop runs on as if they weren't there (it
doesn't restart after one, it's where it would have been). A program's
videos must be ready by its start: when it starts, each item is marked
aired or skipped once (tv_program_items.aired), so everyone skips the same
ones and the timeline doesn't move when a late one finishes. Programs are
kept for two days after their start, then dropped. The channel's JSON
carries the programs on now and coming (with everything a player needs),
so a page switches at the boundary on its own."""
import math
import re
import secrets
import time
from types import SimpleNamespace

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
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
PROGRAM_ITEMS_MAX = 50
PROGRAM_MAX_LEN = 24 * 3600
PROGRAM_AHEAD = 30 * 86400
DAY = 86400


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
        ids = [int(v) for v in raw[:LINEUP_MAX]]
    except (TypeError, ValueError, OverflowError):
        return _error(400, "videos is a list of video ids")
    # (An id SQLite can't bind is a 500 in the queries that look them up.)
    return [i for i in ids if 0 < i < 2 ** 63]


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
        u = _vurls(r)
        out[r["channel_id"]].append({
            "id": r["post_id"], "title": r["title"], "username": r["username"], "duration": r["duration"],
            "status": r["status"], "progress": r["progress"], "hls": u["hls"], "poster": u["poster"],
            "posterSmall": u["posterSmall"], "unlisted": bool(r["unlisted"]),
        })
    return out


def _vurls(r) -> dict:
    """Videos' file URLs for a row of ours (which doesn't select every
    column Videos may read)."""
    d = dict(r)
    d.setdefault("original", None)
    return videos._urls(d)


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


def _program_items(db, program_ids: list[str]) -> dict[str, list[dict]]:
    """Each program's videos in order, as _lineups' items plus `aired`."""
    out: dict[str, list[dict]] = {p: [] for p in program_ids}
    if not program_ids:
        return out
    marks = ",".join("?" * len(program_ids))
    rows = db.execute(f"""SELECT t.program_id, t.pos, t.aired, v.post_id, v.title, v.file_id, v.status, v.progress, v.poster,
                                 v.storyboard, v.duration, v.unlisted, p.username
                          FROM tv_program_items t JOIN videos v ON v.post_id = t.video_id JOIN posts p ON p.id = v.post_id
                          WHERE t.program_id IN ({marks}) ORDER BY t.program_id, t.pos""", program_ids).fetchall()
    for r in rows:
        u = _vurls(r)
        out[r["program_id"]].append({
            "id": r["post_id"], "pos": r["pos"], "title": r["title"], "username": r["username"], "duration": r["duration"],
            "status": r["status"], "progress": r["progress"], "hls": u["hls"], "poster": u["poster"],
            "posterSmall": u["posterSmall"], "unlisted": bool(r["unlisted"]), "aired": r["aired"],
        })
    return out


def _settle(db, now: float) -> None:
    """Programs that have started decide, once, which of their videos play:
    the ones ready by then. Later readiness doesn't move the timeline."""
    rows = db.execute("""SELECT DISTINCT t.program_id FROM tv_program_items t JOIN tv_programs p ON p.id = t.program_id
                         WHERE t.aired IS NULL AND p.start <= ?""", (now,)).fetchall()
    if not rows:
        return
    items = _program_items(db, [r[0] for r in rows])
    for pid, its in items.items():
        for i in its:
            if i["aired"] is None:
                db.execute("UPDATE tv_program_items SET aired=? WHERE program_id=? AND pos=?", (1 if _playable(i) else 0, pid, i["pos"]))
    # Ones whose videos have all gone since.
    db.execute("UPDATE tv_program_items SET aired=0 WHERE aired IS NULL AND program_id IN (SELECT id FROM tv_programs WHERE start <= ?)", (now,))
    db.commit()


def _plays(p: dict, now: float) -> list[dict]:
    """What a program plays: once started, what was ready at its start;
    before, what's ready now (a guess until then)."""
    if p["start"] <= now:
        return [i for i in p["items"] if i["aired"]]
    return [i for i in p["items"] if _playable(i)]


def _programs(db, channel_ids: list[str], frm: float, to: float, now: float | None = None) -> dict[str, list[dict]]:
    """Programs on these channels that overlap [frm, to), by start. `end`
    is the time it holds: what plays once it's started, all of its videos
    before (so a late one still has its slot)."""
    now = time.time() if now is None else now
    out: dict[str, list[dict]] = {c: [] for c in channel_ids}
    if not channel_ids:
        return out
    _settle(db, now)
    marks = ",".join("?" * len(channel_ids))
    rows = db.execute(f"""SELECT * FROM tv_programs WHERE channel_id IN ({marks}) AND start < ? AND start > ?
                          ORDER BY start""", (*channel_ids, to, frm - PROGRAM_MAX_LEN - 60)).fetchall()
    items = _program_items(db, [r["id"] for r in rows])
    for r in rows:
        p = {"id": r["id"], "channelId": r["channel_id"], "start": r["start"], "title": r["title"], "note": r["note"],
             "createdBy": r["created_by"], "created": r["created"], "items": items[r["id"]]}
        started = r["start"] <= now
        length = sum((i["duration"] or 0) for i in (_plays(p, now) if started else p["items"]))
        p["end"] = round(r["start"] + length, 3)
        if p["end"] <= frm:
            continue
        for i in p["items"]:
            i["plays"] = bool(i["aired"]) if started else _playable(i)
        out[r["channel_id"]].append(p)
    return out


def _mini(p: dict) -> dict:
    return {"id": p["id"], "title": p["title"], "createdBy": p["createdBy"], "start": p["start"], "end": p["end"]}


def _airing(items: list[dict], epoch: float, programs: list[dict], at: float) -> dict:
    """_now_next with programs: one that's on plays from its start; the
    loop's item is cut short by one coming, and picks up after it where
    the loop would have been."""
    for p in programs:
        sched = _plays(p, at)
        length = sum(i["duration"] for i in sched)
        if not (p["start"] <= at < p["start"] + length):
            continue
        pos, start = at - p["start"], 0.0
        for k, item in enumerate(sched):
            if pos < start + item["duration"] or k == len(sched) - 1:
                end = p["start"] + start + item["duration"]
                if k + 1 < len(sched):
                    nxt, nprog = sched[k + 1], _mini(p)
                else:
                    after = _airing(items, epoch, [q for q in programs if q is not p], end + 0.001)["now"]
                    nxt, nprog = after, (after or {}).get("program")
                return {
                    "total": sum(i["duration"] for i in items if _playable(i)),
                    "now": {"id": item["id"], "title": item["title"], "posterSmall": item["posterSmall"],
                            "start": round(start, 3), "duration": item["duration"], "at": round(pos - start, 2),
                            "program": _mini(p)},
                    "next": {"id": nxt["id"], "title": nxt["title"], "posterSmall": nxt["posterSmall"],
                             "in": round(end - at, 1), "program": nprog} if nxt else None,
                }
            start += item["duration"]
    nn = _now_next(items, epoch, at)
    coming = next((p for p in programs if p["start"] > at and _plays(p, at)), None)
    if coming and (nn["now"] is None or coming["start"] - at < nn["next"]["in"]):
        first = _plays(coming, at)[0]
        nn["next"] = {"id": first["id"], "title": first["title"], "posterSmall": first["posterSmall"],
                      "in": round(coming["start"] - at, 1), "program": _mini(coming)}
    return nn


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


def _json(r, items: list[dict], request: Request, username: str, watchers: dict | None = None, full: bool = False,
          programs: list[dict] | None = None) -> dict:
    t = time.time()
    programs = programs or []
    nn = _airing(items, r["epoch"], programs, t)
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
        # Programs on now and coming (a day and a half), what the player
        # follows: each one's videos, and which of them play.
        "programs": [_program_json(p, r, request, username) for p in programs],
    }
    if full or can_edit:
        # Everything in the lineup, ready or not, for the editor.
        out["lineup"] = items
    return out


def _program_json(p: dict, channel, request: Request, username: str) -> dict:
    return {**p, "channelNumber": channel["number"],
            "canEdit": username == p["createdBy"] or _can_edit(channel, request, username)}


def _soon(db, channel_ids: list[str]) -> dict[str, list[dict]]:
    t = time.time()
    return _programs(db, channel_ids, t, t + 36 * 3600, t)


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
        progs = _soon(db, [channel_id])[channel_id]
    return _json(row, items, request, username, _watchers(), full=True, programs=progs)


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


# ── Programs (the guide). Before /{channel_id}, so "programs" isn't one. ──

def _clash(db, channel_id: str, start: float, end: float, skip: str = "") -> dict | None:
    for p in _programs(db, [channel_id], start, end)[channel_id]:
        if p["id"] != skip and p["start"] < end and start < p["end"]:
            return p
    return None


def _clash_error(p: dict) -> JSONResponse:
    return JSONResponse(status_code=409, content={
        "error": f"that clashes with “{p['title']}”", "clash": _mini(p)})


def _program_videos(db, raw) -> list[dict] | JSONResponse:
    """The videos for a program, in order: each one has to exist and have a
    known length (it can still be processing; it plays if it's ready by
    the start)."""
    ids = _video_ids(raw)
    if isinstance(ids, JSONResponse):
        return ids
    if not ids:
        return _error(400, "a program needs at least one video")
    if len(ids) > PROGRAM_ITEMS_MAX:
        return _error(400, f"a program can hold {PROGRAM_ITEMS_MAX} videos")
    marks = ",".join("?" * len(set(ids)))
    found = {r["post_id"]: r for r in db.execute(
        f"SELECT post_id, title, status, duration FROM videos WHERE post_id IN ({marks})", list(set(ids)))}
    out = []
    for v in ids:
        r = found.get(v)
        if r is None:
            return _error(404, "one of those videos is gone")
        if r["status"] == "failed" or not (r["duration"] or 0) > 0.5:
            return _error(400, f"“{r['title'] or 'Untitled'}” can’t be scheduled: it didn’t process")
        out.append({"id": v, "title": r["title"], "duration": r["duration"]})
    if sum(v["duration"] for v in out) > PROGRAM_MAX_LEN:
        return _error(400, "a program can run for 24 hours at most")
    return out


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _start(value, now: float) -> float | JSONResponse:
    try:
        start = float(value)
    except (TypeError, ValueError, OverflowError):
        return _error(400, "a program needs a start time")
    if not math.isfinite(start):
        # NaN passed both checks below and was stored as NULL: a program
        # that 500'd its own answer and every later edit of it.
        return _error(400, "a program needs a start time")
    if start < now - 60:
        return _error(400, "that time has gone")
    if start > now + PROGRAM_AHEAD:
        return _error(400, "programs can be scheduled up to 30 days ahead")
    return round(max(start, now), 3)


def _channel_for(db, value):
    v = str(value or "").strip()
    row = _load(db, v)
    if row is None and v.isdigit():
        row = db.execute("SELECT * FROM tv_channels WHERE number=?", (int(v),)).fetchone()
    return row


def _program_out(db, program_id: str, request: Request, username: str) -> dict | None:
    r = db.execute("SELECT channel_id, start FROM tv_programs WHERE id=?", (program_id,)).fetchone()
    if r is None:
        return None
    channel = _load(db, r["channel_id"])
    p = next((x for x in _programs(db, [r["channel_id"]], r["start"] - 1, r["start"] + 1)[r["channel_id"]] if x["id"] == program_id), None)
    return _program_json(p, channel, request, username) if p and channel else None


def _tidy(db, now: float) -> None:
    """Programs drop off two days after they start (a day of history)."""
    old = [r[0] for r in db.execute("SELECT id FROM tv_programs WHERE start < ?", (now - 2 * DAY,))]
    if old:
        marks = ",".join("?" * len(old))
        db.execute(f"DELETE FROM tv_program_items WHERE program_id IN ({marks})", old)
        db.execute(f"DELETE FROM tv_programs WHERE id IN ({marks})", old)
        db.commit()


@router.get(f"{BASE}/programs")
def list_programs(request: Request, frm: float | None = Query(None, alias="from"), to: float | None = None):
    """Every channel's programs that overlap [from, to) (unix seconds;
    now − 1h … now + 24h by default, at most 8 days)."""
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    t = time.time()
    frm = t - 3600 if frm is None else frm
    to = t + DAY if to is None else to
    to = min(to, frm + 8 * DAY)
    with get_db() as db:
        _tidy(db, t)
        rows = db.execute("SELECT * FROM tv_channels ORDER BY number").fetchall()
        progs = _programs(db, [r["id"] for r in rows], frm, to, t)
    out = [_program_json(p, r, request, username) for r in rows for p in progs[r["id"]]]
    out.sort(key=lambda p: (p["start"], p["channelNumber"]))
    return {"now": t, "from": frm, "to": to, "programs": out}


@router.get(f"{BASE}/programs/{{program_id}}")
def get_program(program_id: str, request: Request):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    with get_db() as db:
        out = _program_out(db, program_id, request, username)
    return out if out else _error(404, "not found")


@router.post(f"{BASE}/programs")
async def create_program(request: Request):
    """{channel (id or number), start (unix seconds), title, note, videos}:
    into free time on any channel, by anyone signed in."""
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    t = time.time()
    with get_db() as db:
        channel = _channel_for(db, body.get("channel"))
        if channel is None:
            return _error(404, "no such channel")
        start = _start(body.get("start"), t)
        if isinstance(start, JSONResponse):
            return start
        vids = _program_videos(db, body.get("videos"))
        if isinstance(vids, JSONResponse):
            return vids
        end = start + sum(v["duration"] for v in vids)
        clash = _clash(db, channel["id"], start, end)
        if clash:
            return _clash_error(clash)
        title = " ".join(str(body.get("title") or "").split())[:80] or vids[0]["title"] or "Untitled"
        program_id = secrets.token_urlsafe(9)
        db.execute("INSERT INTO tv_programs(id, channel_id, start, title, note, created_by) VALUES (?, ?, ?, ?, ?, ?)",
                   (program_id, channel["id"], start, title, str(body.get("note") or "").strip()[:300], username))
        db.executemany("INSERT INTO tv_program_items(program_id, pos, video_id) VALUES (?, ?, ?)",
                       [(program_id, i, v["id"]) for i, v in enumerate(vids)])
        db.commit()
        return _program_out(db, program_id, request, username)


def _program_editable(db, program_id: str, request: Request, username: str):
    r = db.execute("SELECT * FROM tv_programs WHERE id=?", (program_id,)).fetchone()
    if r is None:
        return None, _error(404, "not found")
    channel = _load(db, r["channel_id"])
    if not (username == r["created_by"] or is_admin(get_groups(request))
            or (channel is not None and _can_edit(channel, request, username))):
        return None, _error(403, "not yours to change")
    return r, None


@router.patch(f"{BASE}/programs/{{program_id}}")
async def edit_program(program_id: str, request: Request):
    """Title and note any time; channel, start and videos only before it
    starts."""
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    t = time.time()
    with get_db() as db:
        r, err = _program_editable(db, program_id, request, username)
        if err:
            return err
        started = r["start"] <= t
        if started and any(k in body for k in ("channel", "start", "videos")):
            moved = (
                ("channel" in body and (_channel_for(db, body["channel"]) or {"id": None})["id"] != r["channel_id"])
                or ("start" in body and abs(_float(body["start"]) - r["start"]) > 1)
                or "videos" in body
            )
            if moved:
                return _error(400, "it’s already on: only its title and note can change")
        sets, args = [], []
        if "title" in body:
            title = " ".join(str(body["title"] or "").split())[:80]
            if not title:
                return _error(400, "a program needs a title")
            sets.append("title=?"); args.append(title)
        if "note" in body:
            sets.append("note=?"); args.append(str(body["note"] or "").strip()[:300])
        if not started:
            channel_id, start = r["channel_id"], r["start"]
            if "channel" in body:
                channel = _channel_for(db, body["channel"])
                if channel is None:
                    return _error(404, "no such channel")
                channel_id = channel["id"]
            if "start" in body:
                start = _start(body["start"], t)
                if isinstance(start, JSONResponse):
                    return start
            if "videos" in body:
                vids = _program_videos(db, body["videos"])
                if isinstance(vids, JSONResponse):
                    return vids
            else:
                vids = [{"id": i["id"], "duration": i["duration"] or 0}
                        for i in _program_items(db, [program_id])[program_id]]
            clash = _clash(db, channel_id, start, start + sum(v["duration"] for v in vids), skip=program_id)
            if clash:
                return _clash_error(clash)
            sets += ["channel_id=?", "start=?"]; args += [channel_id, start]
            if "videos" in body:
                db.execute("DELETE FROM tv_program_items WHERE program_id=?", (program_id,))
                db.executemany("INSERT INTO tv_program_items(program_id, pos, video_id) VALUES (?, ?, ?)",
                               [(program_id, i, v["id"]) for i, v in enumerate(vids)])
        if sets:
            db.execute(f"UPDATE tv_programs SET {', '.join(sets)} WHERE id=?", (*args, program_id))
        db.commit()
        return _program_out(db, program_id, request, username)


@router.delete(f"{BASE}/programs/{{program_id}}")
def delete_program(program_id: str, request: Request):
    """Gone from the guide; one that's on stops, and the loop carries on."""
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    with get_db() as db:
        r, err = _program_editable(db, program_id, request, username)
        if err:
            return err
        db.execute("DELETE FROM tv_program_items WHERE program_id=?", (program_id,))
        db.execute("DELETE FROM tv_programs WHERE id=?", (program_id,))
        db.commit()
    return {"ok": True}


@router.get(BASE)
def list_channels(request: Request):
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    watchers = _watchers()
    with get_db() as db:
        rows = db.execute("SELECT * FROM tv_channels ORDER BY number").fetchall()
        lineups = _lineups(db, [r["id"] for r in rows])
        progs = _soon(db, [r["id"] for r in rows])
    return {"now": time.time(), "channels": [_json(r, lineups[r["id"]], request, username, watchers, programs=progs[r["id"]]) for r in rows]}


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
        progs = _soon(db, [row["id"]])[row["id"]]
    return {"now": time.time(), **_json(row, items, request, username, _watchers(), full=True, programs=progs)}


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
        if not 0 < vid < 2 ** 63:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
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
def upload_into(channel_id: str, request: Request, file: UploadFile | None = File(None), title: str = Form(""),
                upload: str = Form("")):
    """A file straight into the channel: made like any video, but unlisted,
    and put on the end of the lineup (a new channel's only item). The file
    is the multipart `file`, or `upload`: a finished chunked upload's id
    (videos.take_upload), which is how the site sends a big one."""
    username = _signed_in(request)
    if not username:
        return _error(401, "not authenticated")
    row, err = _editable(channel_id, request, username)
    if err:
        return err
    if upload:
        saved, meta = videos.take_upload(upload, username)
        if saved is None:
            return meta
        post_id = videos.create(request, username, SimpleNamespace(filename=meta.get("name") or ""), title or row["name"], "",
                                unlisted=True, saved=saved)
    elif file is None:
        return _error(400, "a file or an upload is needed")
    else:
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
               JOIN posts p ON p.id = t.video_id
               WHERE t.channel_id=? AND v.unlisted=1
                 -- Only what this channel's own people uploaded: any id can be
                 -- put in a lineup, so someone else's unlisted video (an
                 -- imported one) mustn't be deleted with a channel it was added to.
                 AND p.username IN (?, ?, ?)
                 AND NOT EXISTS (SELECT 1 FROM tv_items o WHERE o.video_id = t.video_id AND o.channel_id != t.channel_id)
                 AND NOT EXISTS (SELECT 1 FROM tv_program_items pi JOIN tv_programs pr ON pr.id = pi.program_id
                                 WHERE pi.video_id = t.video_id AND pr.channel_id != t.channel_id)""",
            (channel_id, row["created_by"], row["author"] or row["created_by"], username))]
        db.execute("DELETE FROM tv_program_items WHERE program_id IN (SELECT id FROM tv_programs WHERE channel_id=?)", (channel_id,))
        db.execute("DELETE FROM tv_programs WHERE channel_id=?", (channel_id,))
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
