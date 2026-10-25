"""What everyone's been listening to, for Music's Activity page: the song
they're on right now and how far into it, the last thing they played,
and a few numbers (time listened, plays, favourite artist).

Nothing new is sent for it. Presence's beat (every ~8s from each open
page, routers/presence.py) already says what's playing; the beat from the
device someone's card shows is what's counted here, so two open tabs
don't count twice. A play is one row in `listens`, its seconds adding up
beat by beat while it plays. Only plays of LISTEN_MIN seconds or more
count as a listen: a skip isn't one."""
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_username
from ..db import get_db

router = APIRouter()

LISTEN_MIN = 30
# A gap between beats longer than this is the page having gone (or the
# song having been left paused): the play ends there rather than counting
# the gap.
_GAP = 45
_STEP_MAX = 30
# username -> the play being counted: {"id", "song_id", "at", "pos"}.
# This process's memory only, like presence's own beats.
_open: dict[str, dict] = {}


def record(db, username: str, beat: dict) -> None:
    """Counts one beat (the one someone's card shows) towards their plays.
    Called inside presence's own write, on its connection; it commits."""
    now = time.time()
    song_id = beat.get("song_id") or ""
    cur = _open.get(username)
    if not song_id or beat.get("remote"):
        _open.pop(username, None)
        return
    pos = beat.get("position")
    same = (cur and cur["song_id"] == song_id and now - cur["at"] < _GAP
            # Back near the start of the same song: played again (repeat, or
            # started over), so a play of its own.
            and not (pos is not None and cur["pos"] is not None and pos + 5 < cur["pos"] and pos < 15))
    if not beat.get("playing"):
        # Paused: nothing counts, and the time it sat paused won't either.
        if same:
            cur.update(at=now, pos=pos)
        return
    if same:
        step = min(now - cur["at"], _STEP_MAX)
        db.execute("UPDATE listens SET seconds = seconds + ?, last = datetime('now') WHERE id = ?", (step, cur["id"]))
        cur.update(at=now, pos=pos)
        return
    row = db.execute(
        """INSERT INTO listens(username, song_id, song, artist, album, cover_art)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (username, song_id, beat.get("song", ""), beat.get("artist", ""), beat.get("album", ""), beat.get("cover_art", "")),
    )
    _open[username] = {"id": row.lastrowid, "song_id": song_id, "at": now, "pos": pos}


def _since(days: float, tz: int) -> str:
    """The start of `days` days ago (0: today), on the asker's clock, as UTC."""
    local = datetime.utcnow() - timedelta(minutes=tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
    return (start + timedelta(minutes=tz)).strftime("%Y-%m-%d %H:%M:%S")


def _song(r) -> dict:
    return {"song": r["song"], "artist": r["artist"], "album": r["album"], "songId": r["song_id"], "coverArt": r["cover_art"]}


@router.get("/api/music-requests/activity")
def activity(request: Request, tz: int = 0):
    """Everyone who's listened to anything: now, last, and their numbers.
    tz: the asker's getTimezoneOffset() (minutes), so "today" and the
    week's days are theirs."""
    if get_username(request) == "anonymous":
        return JSONResponse(status_code=401, content={"error": "not authenticated"})
    from .presence import listening, live  # what's playing right now, from presence's beats

    tz = max(-14 * 60, min(14 * 60, tz))
    today, week = _since(0, tz), _since(6, tz)
    shift = f"{-tz:+d} minutes"
    with get_db() as db:
        totals = {r["username"]: r for r in db.execute(
            """SELECT username,
                      SUM(seconds) AS total,
                      SUM(CASE WHEN started >= ? THEN seconds ELSE 0 END) AS week,
                      SUM(CASE WHEN started >= ? THEN seconds ELSE 0 END) AS today,
                      SUM(CASE WHEN seconds >= ? THEN 1 ELSE 0 END) AS plays,
                      MIN(started) AS first
               FROM listens GROUP BY username""",
            (week, today, LISTEN_MIN),
        ).fetchall()}
        people = []
        for username, t in totals.items():
            last = db.execute(
                """SELECT * FROM listens WHERE username = ? AND seconds >= ?
                   ORDER BY last DESC LIMIT 1""",
                (username, LISTEN_MIN),
            ).fetchone()
            top_artist = db.execute(
                """SELECT artist, SUM(seconds) AS s FROM listens
                   WHERE username = ? AND artist != '' GROUP BY artist ORDER BY s DESC LIMIT 1""",
                (username,),
            ).fetchone()
            top_song = db.execute(
                """SELECT song_id, song, artist, album, cover_art, COUNT(*) AS n FROM listens
                   WHERE username = ? AND seconds >= ? GROUP BY song_id ORDER BY n DESC, MAX(last) DESC LIMIT 1""",
                (username, LISTEN_MIN),
            ).fetchone()
            days = {r["d"]: r["s"] for r in db.execute(
                """SELECT date(started, ?) AS d, SUM(seconds) AS s FROM listens
                   WHERE username = ? AND started >= ? GROUP BY d""",
                (shift, username, week),
            ).fetchall()}
            local_today = (datetime.utcnow() - timedelta(minutes=tz)).date()
            people.append({
                "username": username,
                "now": live(username),
                "last": dict(_song(last), at=last["last"]) if last else None,
                "stats": {
                    "total": round(t["total"] or 0),
                    "week": round(t["week"] or 0),
                    "today": round(t["today"] or 0),
                    "plays": t["plays"] or 0,
                    "since": t["first"],
                    "topArtist": {"name": top_artist["artist"], "seconds": round(top_artist["s"])} if top_artist else None,
                    "topSong": dict(_song(top_song), plays=top_song["n"]) if top_song and top_song["n"] > 1 else None,
                    # Oldest first, the last one today.
                    "days": [round(days.get(str(local_today - timedelta(days=i)), 0)) for i in range(6, -1, -1)],
                },
            })
        # Listening now but with nothing counted yet (just started).
        for username in listening():
            if username not in totals:
                people.append({"username": username, "now": live(username), "last": None, "stats": None})
        group = db.execute(
            """SELECT artist, SUM(seconds) AS s FROM listens
               WHERE started >= ? AND artist != '' GROUP BY artist ORDER BY s DESC LIMIT 5""",
            (week,),
        ).fetchall()
        week_total = db.execute("SELECT SUM(seconds) AS s FROM listens WHERE started >= ?", (week,)).fetchone()["s"]
        since = db.execute("SELECT MIN(started) AS s FROM listens").fetchone()["s"]

    def order(p):
        n = p["now"] or {}
        return (not n.get("playing"), not n, -(datetime.fromisoformat(p["last"]["at"]).timestamp() if p["last"] else 0))

    people.sort(key=order)
    return {
        "people": people,
        "week": {"seconds": round(week_total or 0), "artists": [{"name": r["artist"], "seconds": round(r["s"])} for r in group]},
        "since": since,
    }
