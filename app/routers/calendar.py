"""Personal calendars + "book with" requests — see db.py's calendar_events
comment for the data model. Deliberately minimal: no recurrence, no
availability rules, no external sync. A booking is just a proposed time
the invitee accepts or declines.
"""

from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_username
from ..config import CALENDAR_NOTES_MAX_CHARS, CALENDAR_TITLE_MAX_CHARS
from ..db import get_db

router = APIRouter()


def _unauthenticated() -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": "not authenticated"})


def _row_to_event(r) -> dict:
    return {
        "id": r["id"],
        "requester": r["requester_username"],
        "invitee": r["invitee_username"] or None,
        "title": r["title"],
        "notes": r["notes"],
        "start_time": r["start_time"],
        "end_time": r["end_time"],
        "status": r["status"],
        "created": r["created"],
    }


@router.get("/api/calendar")
def get_calendar(request: Request):
    """Everything you're involved in — your own personal events, requests
    you've sent, and requests sent to you (any status) — sorted by start
    time so the frontend can just render it as an agenda list."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    with get_db() as db:
        rows = db.execute(
            """SELECT * FROM calendar_events
               WHERE requester_username=? OR invitee_username=?
               ORDER BY start_time ASC""",
            (username, username),
        ).fetchall()
    return {"events": [_row_to_event(r) for r in rows]}


@router.post("/api/calendar")
async def create_event(request: Request):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})

    title = str(body.get("title", "")).strip()[:CALENDAR_TITLE_MAX_CHARS]
    notes = str(body.get("notes", "")).strip()[:CALENDAR_NOTES_MAX_CHARS]
    start_time = str(body.get("start_time", "")).strip()
    end_time = str(body.get("end_time", "")).strip()
    invitee = str(body.get("invitee", "")).strip().lower()

    if not title:
        return JSONResponse(status_code=400, content={"error": "title required"})
    try:
        start_dt = datetime.fromisoformat(start_time)
        end_dt = datetime.fromisoformat(end_time)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "start_time/end_time must be ISO datetimes"})
    if end_dt <= start_dt:
        return JSONResponse(status_code=400, content={"error": "end_time must be after start_time"})

    # Booking yourself isn't a real invite — treat it as a plain personal event.
    if invitee == username:
        invitee = ""
    status = "pending" if invitee else "accepted"

    with get_db() as db:
        cur = db.execute(
            """INSERT INTO calendar_events(requester_username, invitee_username, title, notes, start_time, end_time, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (username, invitee, title, notes, start_time, end_time, status),
        )
        db.commit()
        row = db.execute("SELECT * FROM calendar_events WHERE id=?", (cur.lastrowid,)).fetchone()
    return _row_to_event(row)


@router.put("/api/calendar/{event_id}")
async def respond_to_event(event_id: int, request: Request):
    """Accept/decline a booking request — only the invitee can respond,
    and only while it's still pending (no re-litigating a settled one)."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    status = str(body.get("status", "")).strip()
    if status not in ("accepted", "declined"):
        return JSONResponse(status_code=400, content={"error": "status must be accepted or declined"})

    with get_db() as db:
        row = db.execute("SELECT * FROM calendar_events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "not found"})
        if row["invitee_username"] != username:
            return JSONResponse(status_code=403, content={"error": "only the invitee can respond to this"})
        if row["status"] != "pending":
            return JSONResponse(status_code=400, content={"error": "already responded to"})
        db.execute("UPDATE calendar_events SET status=? WHERE id=?", (status, event_id))
        db.commit()
        row = db.execute("SELECT * FROM calendar_events WHERE id=?", (event_id,)).fetchone()
    return _row_to_event(row)


@router.delete("/api/calendar/{event_id}")
def delete_event(event_id: int, request: Request):
    """Either side can cancel — a declined/cancelled booking just disappears
    for both of them, there's no need to keep a dead request around."""
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    with get_db() as db:
        row = db.execute("SELECT * FROM calendar_events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "not found"})
        if username not in (row["requester_username"], row["invitee_username"]):
            return JSONResponse(status_code=403, content={"error": "not your event"})
        db.execute("DELETE FROM calendar_events WHERE id=?", (event_id,))
        db.commit()
    return {"ok": True}
