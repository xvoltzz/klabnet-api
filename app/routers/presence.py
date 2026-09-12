from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_username, touch_user
from ..config import LISTENING_HEARTBEAT_CAP_SECONDS, PRESENCE_TTL
from ..db import get_db

router = APIRouter()


def _unauthenticated() -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": "not authenticated"})


@router.post("/api/presence")
async def post_presence(request: Request):
    """Called by the dashboard every ~8s to broadcast what you're listening to."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})

    song       = str(body.get("song",   "")).strip()[:200]
    artist     = str(body.get("artist", "")).strip()[:200]
    song_id    = str(body.get("songId", "")).strip()[:100]
    playing    = bool(body.get("playing", False))
    party_host = str(body.get("partyHost", "")).strip()[:100]

    touch_user(username)
    with get_db() as db:
        # Read the PREVIOUS heartbeat before overwriting it — if that one
        # said "playing", the gap between it and this one is real listening
        # time (this heartbeat's own `playing` doesn't matter here: even if
        # they just paused, the elapsed time since the last heartbeat was
        # still spent listening). Feeds the (experimental) leaderboard's
        # minutes-listened stat; see config.LISTENING_HEARTBEAT_CAP_SECONDS
        # for why this is capped rather than trusted outright.
        prev = db.execute(
            "SELECT playing, updated FROM presence WHERE username=?", (username,)
        ).fetchone()
        db.execute(
            """INSERT INTO presence(username, song, artist, song_id, playing, party_host, updated)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(username) DO UPDATE SET
                   song       = excluded.song,
                   artist     = excluded.artist,
                   song_id    = excluded.song_id,
                   playing    = excluded.playing,
                   party_host = excluded.party_host,
                   updated    = excluded.updated""",
            (username, song, artist, song_id, 1 if playing else 0, party_host),
        )
        if prev and prev["playing"]:
            try:
                prev_dt = datetime.strptime(prev["updated"], "%Y-%m-%d %H:%M:%S")
                elapsed = (datetime.utcnow() - prev_dt).total_seconds()
            except (ValueError, TypeError):
                elapsed = 0
            elapsed = max(0, min(int(elapsed), LISTENING_HEARTBEAT_CAP_SECONDS))
            if elapsed:
                db.execute(
                    """INSERT INTO listening_stats(username, seconds_listened, updated)
                       VALUES (?, ?, datetime('now'))
                       ON CONFLICT(username) DO UPDATE SET
                           seconds_listened = seconds_listened + excluded.seconds_listened,
                           updated = excluded.updated""",
                    (username, elapsed),
                )
        db.commit()
    return {"ok": True}


@router.get("/api/presence")
def get_presence(request: Request):
    """Returns all listeners active within the last PRESENCE_TTL seconds."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()

    cutoff = (datetime.utcnow() - timedelta(seconds=PRESENCE_TTL)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as db:
        rows = db.execute(
            """SELECT username, song, artist, song_id, playing, party_host, updated
               FROM   presence
               WHERE  updated >= ?
               ORDER  BY updated DESC""",
            (cutoff,),
        ).fetchall()

    listeners = [
        {
            "username":  r["username"],
            "song":      r["song"],
            "artist":    r["artist"],
            "songId":    r["song_id"],
            "playing":   bool(r["playing"]),
            "partyHost": r["party_host"] or None,
            "updated":   r["updated"],
        }
        for r in rows
    ]
    return {"listeners": listeners}


@router.delete("/api/presence")
def clear_presence(request: Request):
    """Let a user remove themselves from presence (e.g. on logout/close)."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    with get_db() as db:
        db.execute("DELETE FROM presence WHERE username=?", (username,))
        db.commit()
    return {"ok": True}
