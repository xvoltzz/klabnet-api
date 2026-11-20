"""This week on klabnet: a page made each week from what people did, for
Social. Monday to Sunday on the asker's clock; any earlier week can be
looked back at.

Every line comes from something that happened (listens, posts, photos,
videos, playlists, radio stations, TV channels, reactions) and a line
only exists when there's enough behind it: no filler. Lines have a
stable key ("song", "found", "videos"…) so people can react to one and
reply under it (week_reactions, week_replies, keyed by the week's Monday
and the line). When someone does, the line's people are kept with it
(`about`), so the recap can tell them (routers/recap.py).

Computed when asked and kept a few minutes (the current week) or hours
(a past one). Reactions and replies aren't cached. Under /api/posts/
because Caddy only forwards a fixed list of /api prefixes; registered
before posts.router so /api/posts/week isn't taken for a post id."""
import calendar
import re
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_groups, get_username, is_admin
from ..config import EMOJI_MAX_CHARS, POST_MAX_CHARS
from ..db import get_db
from .listens import LISTEN_MIN

router = APIRouter()
BASE = "/api/posts/week"

_TTL_NOW = 300
_TTL_PAST = 6 * 3600
_cache: dict[tuple, tuple[float, dict]] = {}
_FMT = "%Y-%m-%d %H:%M:%S"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
LINES = ("song", "album", "artist", "found", "repeat", "reacted", "videos", "photos",
         "playlists", "stations", "channels", "albums")
SNIP = 140


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _snip(s: str) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= SNIP else s[: SNIP - 1] + "…"


def _tz(tz) -> int:
    try:
        return max(-14 * 60, min(14 * 60, int(tz)))
    except (TypeError, ValueError):
        return 0


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _today(tz: int) -> date:
    return (datetime.utcnow() - timedelta(minutes=tz)).date()


def _bounds(start: date, tz: int) -> tuple[str, str]:
    """The week's start and end as UTC SQL times (tz: getTimezoneOffset())."""
    a = datetime(start.year, start.month, start.day) + timedelta(minutes=tz)
    return a.strftime(_FMT), (a + timedelta(days=7)).strftime(_FMT)


def _who(rows, key="n") -> list[dict]:
    return [{"username": r["username"], "plays": r[key]} for r in rows]


def _build(start: date, tz: int) -> dict:
    a, b = _bounds(start, tz)
    lines: list[dict] = []
    from .videos import _urls
    with get_db() as db:
        played = "started >= ? AND started < ? AND seconds >= ?"
        args = (a, b, LISTEN_MIN)

        # The most played song, album and artist, across everyone.
        song = db.execute(
            f"""SELECT song_id, song, artist, album, cover_art, COUNT(*) AS n, COUNT(DISTINCT username) AS people
                FROM listens WHERE {played} GROUP BY song_id ORDER BY n DESC, people DESC, MAX(last) DESC LIMIT 1""",
            args).fetchone()
        if song and song["n"] >= 2:
            who = db.execute(
                f"""SELECT username, COUNT(*) AS n FROM listens WHERE {played} AND song_id = ?
                    GROUP BY username ORDER BY n DESC, username LIMIT 4""", (*args, song["song_id"])).fetchall()
            lines.append({"key": "song", "song": song["song"], "artist": song["artist"], "album": song["album"],
                          "songId": song["song_id"], "coverArt": song["cover_art"], "plays": song["n"],
                          "who": _who(who), "people": [w["username"] for w in who]})
        # The album and artist lines only when they say something the
        # song's doesn't (one song all week is the album and artist too).
        top_album = (song["album"], song["artist"]) if song and song["n"] >= 2 else None
        top_artist = song["artist"] if song and song["n"] >= 2 else None
        album = db.execute(
            f"""SELECT album, artist, cover_art, COUNT(*) AS n FROM listens WHERE {played} AND album != ''
                GROUP BY album, artist ORDER BY n DESC LIMIT 1""", args).fetchone()
        if album and lines and (album["album"], album["artist"]) == top_album:
            lines[0]["albumToo"] = True
        elif album and album["n"] >= 3:
            who = db.execute(
                f"""SELECT username, COUNT(*) AS n FROM listens WHERE {played} AND album = ? AND artist = ?
                    GROUP BY username ORDER BY n DESC, username LIMIT 4""", (*args, album["album"], album["artist"])).fetchall()
            lines.append({"key": "album", "album": album["album"], "artist": album["artist"], "coverArt": album["cover_art"],
                          "plays": album["n"], "who": _who(who), "people": [w["username"] for w in who]})
        artist = db.execute(
            f"""SELECT artist, SUM(seconds) AS s, COUNT(*) AS n FROM listens WHERE {played} AND artist != ''
                GROUP BY artist ORDER BY s DESC LIMIT 1""", args).fetchone()
        if artist and lines and artist["artist"] == top_artist:
            lines[0]["artistToo"] = True
        elif artist and artist["n"] >= 3:
            who = db.execute(
                f"""SELECT username, COUNT(*) AS n FROM listens WHERE {played} AND artist = ?
                    GROUP BY username ORDER BY n DESC, username LIMIT 4""", (*args, artist["artist"])).fetchall()
            cover = db.execute("SELECT cover_art FROM listens WHERE artist = ? AND cover_art != '' ORDER BY id DESC LIMIT 1",
                               (artist["artist"],)).fetchone()
            lines.append({"key": "artist", "artist": artist["artist"], "seconds": round(artist["s"]), "plays": artist["n"],
                          "coverArt": cover["cover_art"] if cover else "", "who": _who(who),
                          "people": [w["username"] for w in who]})

        # Found first: an artist someone played for the first time anyone
        # had, this week, that others then played too. The one most
        # people followed; failing an artist, a song.
        # Only once there's a history to be first against (a new
        # klabnet's first week, everything is "found first"), and not
        # what the lines above already name.
        found = None
        known = db.execute("SELECT 1 FROM listens WHERE started < ? LIMIT 1", (a,)).fetchone()
        named = {x.get("artist") for x in lines} | {x.get("songId") for x in lines}
        for col, kind in ((("artist", "artist"), ("song_id", "song")) if known else ()):
            cands = db.execute(
                f"""SELECT {col} AS k FROM listens WHERE {played} AND {col} != ''
                    GROUP BY {col} HAVING COUNT(DISTINCT username) >= 2""", args).fetchall()
            best = None
            for c in cands:
                first = db.execute(
                    f"""SELECT username, started, song, artist, song_id, cover_art FROM listens
                        WHERE {col} = ? AND seconds >= ? ORDER BY started, id LIMIT 1""", (c["k"], LISTEN_MIN)).fetchone()
                if c["k"] in named or not first or not (a <= first["started"] < b):
                    continue
                others = [r["username"] for r in db.execute(
                    f"""SELECT username, MIN(started) AS s FROM listens WHERE {played} AND {col} = ? AND username != ?
                        GROUP BY username ORDER BY s""", (*args, c["k"], first["username"])).fetchall()]
                if others and (not best or len(others) > len(best[1])):
                    best = (first, others)
            if best:
                f, others = best
                found = {"key": "found", "kind": kind, "by": f["username"], "artist": f["artist"],
                         "coverArt": f["cover_art"], "others": others, "people": [f["username"], *others]}
                if kind == "song":
                    found.update(song=f["song"], songId=f["song_id"])
                break
        if found:
            lines.append(found)

        # On repeat: one person, one song, played over and over.
        rep = db.execute(
            f"""SELECT username, song_id, song, artist, cover_art, COUNT(*) AS n FROM listens WHERE {played}
                GROUP BY username, song_id ORDER BY n DESC, MAX(last) DESC LIMIT 1""", args).fetchone()
        if rep and rep["n"] >= 4:
            lines.append({"key": "repeat", "username": rep["username"], "song": rep["song"], "artist": rep["artist"],
                          "songId": rep["song_id"], "coverArt": rep["cover_art"], "plays": rep["n"],
                          "people": [rep["username"]]})

        # The post, photo or video people reacted to most this week.
        top = db.execute(
            """SELECT x.post_id, COUNT(*) AS n, COUNT(DISTINCT x.username) AS people FROM post_reactions x
               JOIN posts p ON p.id = x.post_id
               WHERE x.created >= ? AND x.created < ? AND x.username != p.username
                 AND p.id NOT IN (SELECT post_id FROM videos WHERE unlisted = 1)
               GROUP BY x.post_id ORDER BY n DESC, people DESC, MAX(x.created) DESC LIMIT 1""", (a, b)).fetchone()
        if top and top["n"] >= 2:
            p = db.execute("SELECT * FROM posts WHERE id = ?", (top["post_id"],)).fetchone()
            emoji = [r["emoji"] for r in db.execute(
                """SELECT emoji, COUNT(*) AS c FROM post_reactions WHERE post_id = ? AND created >= ? AND created < ?
                   GROUP BY emoji ORDER BY c DESC LIMIT 3""", (top["post_id"], a, b))]
            line = {"key": "reacted", "postId": p["id"], "kind": p["kind"] or "post", "username": p["username"],
                    "text": _snip(p["text"]), "reactions": top["n"], "emoji": emoji, "people": [p["username"]]}
            if p["kind"] == "photo":
                ph = db.execute("SELECT id FROM photos WHERE post_id = ? ORDER BY position LIMIT 1", (p["id"],)).fetchone()
                line["photo"] = ph["id"] if ph else None
            elif p["kind"] == "video":
                v = db.execute("SELECT * FROM videos WHERE post_id = ?", (p["id"],)).fetchone()
                if v:
                    line.update(title=v["title"], posterSmall=_urls(v)["posterSmall"])
            lines.append(line)

        # New on klabnet.
        vids = db.execute(
            """SELECT v.*, p.username, p.created FROM videos v JOIN posts p ON p.id = v.post_id
               WHERE p.created >= ? AND p.created < ? AND v.unlisted = 0 AND v.status = 'ready' AND v.kind = ''
                 AND v.published >= datetime(p.created, '-2 days')
               ORDER BY p.created DESC""", (a, b)).fetchall()
        if vids:
            lines.append({"key": "videos", "count": len(vids), "people": sorted({v["username"] for v in vids}),
                          "items": [{"id": v["post_id"], "title": v["title"] or "Untitled", "username": v["username"],
                                     "posterSmall": _urls(v)["posterSmall"], "duration": v["duration"]} for v in vids[:6]]})
        phs = db.execute(
            """SELECT ph.id, ph.post_id, ph.width, ph.height, p.username FROM photos ph JOIN posts p ON p.id = ph.post_id
               WHERE p.kind = 'photo' AND p.created >= ? AND p.created < ? ORDER BY p.created DESC, ph.position""",
            (a, b)).fetchall()
        if phs:
            lines.append({"key": "photos", "count": len(phs), "people": sorted({r["username"] for r in phs}),
                          "items": [{"id": r["id"], "postId": r["post_id"], "username": r["username"],
                                     "w": r["width"], "h": r["height"]} for r in phs[:8]]})
        pls = db.execute(
            """SELECT id, name, username, cover, color FROM playlists WHERE featured = 1 AND created >= ? AND created < ?
               ORDER BY created DESC""", (a, b)).fetchall()
        if pls:
            lines.append({"key": "playlists", "count": len(pls), "people": sorted({r["username"] for r in pls}),
                          "items": [{"id": r["id"], "name": r["name"], "username": r["username"], "cover": r["cover"] or None,
                                     "color": r["color"] or None} for r in pls[:6]]})
        sts = db.execute(
            """SELECT id, name, owner, author, frequency, cover, color FROM radio_stations WHERE created >= ? AND created < ?
               ORDER BY created DESC""", (a, b)).fetchall()
        if sts:
            lines.append({"key": "stations", "count": len(sts), "people": sorted({r["author"] or r["owner"] for r in sts}),
                          "items": [{"id": r["id"], "name": r["name"], "username": r["author"] or r["owner"],
                                     "frequency": r["frequency"], "cover": r["cover"] or None, "color": r["color"] or None}
                                    for r in sts[:6]]})
        chs = db.execute(
            """SELECT id, name, number, author, created_by, cover, color FROM tv_channels WHERE created >= ? AND created < ?
               ORDER BY created DESC""", (a, b)).fetchall()
        if chs:
            lines.append({"key": "channels", "count": len(chs), "people": sorted({r["author"] or r["created_by"] for r in chs}),
                          "items": [{"id": r["id"], "name": r["name"], "number": r["number"],
                                     "username": r["author"] or r["created_by"], "cover": r["cover"] or None,
                                     "color": r["color"] or None} for r in chs[:6]]})

        # The quiet numbers under the title.
        t = db.execute(
            f"""SELECT SUM(seconds) AS s, COUNT(DISTINCT username) AS people FROM listens
                WHERE started >= ? AND started < ?""", (a, b)).fetchone()
        a_ts = calendar.timegm(datetime.strptime(a, _FMT).timetuple())
        b_ts = calendar.timegm(datetime.strptime(b, _FMT).timetuple())
        watched = db.execute("SELECT COUNT(*) FROM video_views WHERE at >= ? AND at < ?", (a_ts, b_ts)).fetchone()[0]
        posts = db.execute(
            """SELECT COUNT(*) FROM posts WHERE kind = '' AND created >= ? AND created < ?""", (a, b)).fetchone()[0]
        first = db.execute(
            """SELECT MIN(s) FROM (SELECT MIN(started) AS s FROM listens UNION ALL SELECT MIN(created) FROM posts)""").fetchone()[0]
    totals = {"seconds": round(t["s"] or 0), "listeners": t["people"] or 0, "videosWatched": watched, "posts": posts}
    return {"lines": lines, "totals": totals, "first": first}


def _week(start: date, tz: int) -> dict:
    key = (start.isoformat(), tz)
    hit = _cache.get(key)
    now = time.time()
    current = start >= _monday(_today(tz))
    if hit and now - hit[0] < (_TTL_NOW if current else _TTL_PAST):
        return hit[1]
    body = _build(start, tz)
    if len(_cache) > 200:
        _cache.clear()
    _cache[key] = (now, body)
    return body


def _start(raw: str, tz: int) -> date | None:
    if not raw:
        return _monday(_today(tz))
    if not _DATE.fullmatch(raw):
        return None
    try:
        return _monday(date.fromisoformat(raw))
    except ValueError:
        return None


def _social(db, week: str, me: str) -> tuple[dict, dict]:
    reactions: dict[str, dict] = {}
    for r in db.execute(
        """SELECT line, emoji, COUNT(*) AS cnt, SUM(CASE WHEN username = ? THEN 1 ELSE 0 END) AS mine
           FROM week_reactions WHERE week = ? GROUP BY line, emoji ORDER BY MIN(created)""", (me, week)):
        reactions.setdefault(r["line"], {})[r["emoji"]] = {"count": r["cnt"], "mine": bool(r["mine"])}
    replies: dict[str, list] = {}
    for r in db.execute("SELECT id, line, username, text, created FROM week_replies WHERE week = ? ORDER BY id", (week,)):
        replies.setdefault(r["line"], []).append({"id": r["id"], "username": r["username"], "text": r["text"], "created": r["created"]})
    return reactions, replies


@router.get(BASE)
def get_week(request: Request, start: str = "", tz: int = 0):
    me = get_username(request)
    if me == "anonymous":
        return _error(401, "not authenticated")
    tz = _tz(tz)
    s = _start(start, tz)
    if s is None:
        return _error(400, "start is a date, YYYY-MM-DD")
    this = _monday(_today(tz))
    if s > this:
        s = this
    body = _week(s, tz)
    first = body["first"]
    prev = s - timedelta(days=7)
    has_prev = bool(first) and _bounds(s, tz)[0] > first
    week = s.isoformat()
    with get_db() as db:
        reactions, replies = _social(db, week, me)
    return {
        "start": week, "end": (s + timedelta(days=6)).isoformat(),
        "prev": prev.isoformat() if has_prev else None,
        "next": (s + timedelta(days=7)).isoformat() if s < this else None,
        "current": s == this,
        "lines": body["lines"], "totals": body["totals"],
        "reactions": reactions, "replies": replies,
    }


async def _body(request: Request) -> dict:
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


def _line(week: str, line: str, tz: int) -> dict | None:
    """The line as the page shows it, or None if that week has no such line."""
    s = _start(week, tz)
    if s is None or s.isoformat() != week or line not in LINES:
        return None
    if line == "albums":
        # New albums come from Navidrome, which the site asks itself.
        return {"key": "albums", "people": []}
    return next((x for x in _week(s, tz)["lines"] if x["key"] == line), None)


_LABELS = {
    "song": "most played song", "album": "most played album", "artist": "most played artist",
    "found": "found first", "repeat": "on repeat", "reacted": "most reacted", "videos": "new videos",
    "photos": "new photos", "playlists": "new playlists", "stations": "new stations",
    "channels": "new channels", "albums": "new albums",
}


@router.put(f"{BASE}/{{week}}/{{line}}/reactions")
async def react(week: str, line: str, request: Request):
    """Toggles the caller's reaction on a line of a week."""
    me = get_username(request)
    if me == "anonymous":
        return _error(401, "not authenticated")
    body = await _body(request)
    tz = _tz(body.get("tz"))
    ln = _line(week, line, tz)
    if ln is None:
        return _error(404, "no such line that week")
    emoji = str(body.get("emoji", "")).strip()[:EMOJI_MAX_CHARS]
    if not emoji:
        return _error(400, "emoji required")
    about = "," + ",".join(ln.get("people") or []) + ","
    with get_db() as db:
        had = db.execute("SELECT 1 FROM week_reactions WHERE week=? AND line=? AND username=? AND emoji=?",
                         (week, line, me, emoji)).fetchone()
        if had:
            db.execute("DELETE FROM week_reactions WHERE week=? AND line=? AND username=? AND emoji=?", (week, line, me, emoji))
        else:
            if db.execute("SELECT COUNT(*) FROM week_reactions WHERE week=? AND line=? AND username=?",
                          (week, line, me)).fetchone()[0] >= 12:
                return _error(400, "that's a lot of reactions")
            db.execute("""INSERT OR IGNORE INTO week_reactions(week, line, username, emoji, about, label)
                          VALUES (?, ?, ?, ?, ?, ?)""", (week, line, me, emoji, about, _LABELS[line]))
        db.commit()
        reactions, _ = _social(db, week, me)
    return {"reactions": reactions.get(line, {})}


@router.post(f"{BASE}/{{week}}/{{line}}/replies")
async def reply(week: str, line: str, request: Request):
    me = get_username(request)
    if me == "anonymous":
        return _error(401, "not authenticated")
    body = await _body(request)
    ln = _line(week, line, _tz(body.get("tz")))
    if ln is None:
        return _error(404, "no such line that week")
    text = str(body.get("text", "")).strip()[:POST_MAX_CHARS]
    if not text:
        return _error(400, "say something")
    about = "," + ",".join(ln.get("people") or []) + ","
    with get_db() as db:
        cur = db.execute("""INSERT INTO week_replies(week, line, username, text, about, label)
                            VALUES (?, ?, ?, ?, ?, ?)""", (week, line, me, text, about, _LABELS[line]))
        db.commit()
        r = db.execute("SELECT id, username, text, created FROM week_replies WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(r)


@router.delete(f"{BASE}/replies/{{reply_id}}")
def delete_reply(reply_id: int, request: Request):
    me = get_username(request)
    if me == "anonymous":
        return _error(401, "not authenticated")
    with get_db() as db:
        r = db.execute("SELECT username FROM week_replies WHERE id=?", (reply_id,)).fetchone()
        if r is None:
            return _error(404, "not found")
        if r["username"] != me and not is_admin(get_groups(request)):
            return _error(403, "not yours")
        db.execute("DELETE FROM week_replies WHERE id=?", (reply_id,))
        db.commit()
    return {"ok": True}


def recap_items(db, me: str, at: str) -> list[dict]:
    """For the recap (routers/recap.py): others reacting to or replying under
    a line of a week that's about you, and replies under a line you've
    replied to. One item a line."""
    def iso(s):
        return s.replace(" ", "T") + "Z" if s and "T" not in s else s
    items: dict[tuple, dict] = {}
    like = f"%,{me},%"
    for r in db.execute(
        """SELECT week, line, username, emoji, created, label FROM week_reactions
           WHERE username != ? AND created > ? AND about LIKE ? ORDER BY created DESC LIMIT 100""", (me, at, like)):
        it = items.setdefault(("react", r["week"], r["line"]), {
            "kind": "week-react", "at": iso(r["created"]), "who": [], "emoji": [], "week": r["week"], "line": r["line"],
            "on": r["label"]})
        if r["username"] not in it["who"]:
            it["who"].append(r["username"])
        if r["emoji"] not in it["emoji"]:
            it["emoji"].append(r["emoji"])
    for r in db.execute(
        """SELECT w.week, w.line, w.username, w.text, w.created, w.label FROM week_replies w
           WHERE w.username != ? AND w.created > ?
             AND (w.about LIKE ? OR EXISTS (SELECT 1 FROM week_replies m WHERE m.week = w.week AND m.line = w.line
                                             AND m.username = ? AND m.id < w.id))
           ORDER BY w.id DESC LIMIT 60""", (me, at, like, me)):
        it = items.setdefault(("reply", r["week"], r["line"]), {
            "kind": "week-reply", "at": iso(r["created"]), "who": [], "week": r["week"], "line": r["line"],
            "on": r["label"], "text": _snip(r["text"])})
        if r["username"] not in it["who"]:
            it["who"].append(r["username"])
    return list(items.values())
