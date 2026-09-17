"""Bug reports / feature requests, filed straight into Gitea.

People were never going to open git.klab.gg to report that a button was
broken, so this puts the report box in the app and does the filing for
them. Issues are created by a bot account (GITEA_TOKEN) with the reporter's
klabnet username recorded in the body — posting *as* the user would need a
Gitea admin token plus klabnet usernames matching Gitea ones exactly, which
they don't.

The Gitea calls happen here rather than from the browser for the same
reasons the MusicBrainz ones do (see music_requests.py): the token must
never reach a browser, and it keeps CORS out of the picture entirely.
"""

import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_username
from ..config import (
    FEEDBACK_BODY_MAX_CHARS,
    FEEDBACK_TITLE_MAX_CHARS,
    GITEA_REPO,
    GITEA_TOKEN,
    GITEA_URL,
)

router = APIRouter()

# Maps the form's type onto a Gitea label. Labels must already exist on the
# repo; an unknown label name is silently dropped by Gitea rather than
# erroring, so a missing one degrades to "issue filed, just untagged".
TYPE_LABELS = {"bug": "bug", "feature": "enhancement"}

_TIMEOUT = 10.0

# Listing is a plain proxy of a public endpoint, so it's cheap to cache and
# rude not to — the frontend polls it whenever the panel opens.
_LIST_TTL = 60.0
_list_cache: dict = {"at": 0.0, "data": None}


def _unauthenticated() -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": "not authenticated"})


def _not_configured() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error": "issue tracker not configured"},
    )


def _issues_url() -> str:
    return f"{GITEA_URL.rstrip('/')}/api/v1/repos/{GITEA_REPO}/issues"


def _shape(issue: dict) -> dict:
    """Only the fields the frontend actually renders — the raw Gitea issue
    object carries a full user record, labels, milestones and timestamps we
    have no use for, and forwarding all of it leaks more than we need to."""
    return {
        "number": issue.get("number"),
        "title": issue.get("title") or "",
        "state": issue.get("state") or "open",
        "url": issue.get("html_url") or "",
        "labels": [lbl.get("name") for lbl in (issue.get("labels") or []) if lbl.get("name")],
        "created": issue.get("created_at") or "",
        "comments": issue.get("comments") or 0,
    }


@router.get("/api/feedback")
async def list_feedback(request: Request):
    """Open + recently-closed issues, so someone can see their report landed
    and whether it's been dealt with before filing a duplicate."""
    if get_username(request) == "anonymous":
        return _unauthenticated()
    if not GITEA_URL or not GITEA_REPO:
        return _not_configured()

    now = time.monotonic()
    if _list_cache["data"] is not None and now - _list_cache["at"] < _LIST_TTL:
        return {"issues": _list_cache["data"], "cached": True}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            res = await client.get(
                _issues_url(),
                params={"state": "all", "limit": 50, "type": "issues"},
                headers={"Accept": "application/json"},
            )
        if res.status_code != 200:
            return JSONResponse(status_code=502, content={"error": "issue tracker unavailable"})
        raw = res.json()
        if not isinstance(raw, list):
            return JSONResponse(status_code=502, content={"error": "unexpected response"})
    except httpx.HTTPError:
        return JSONResponse(status_code=502, content={"error": "issue tracker unreachable"})

    issues = [_shape(i) for i in raw if isinstance(i, dict)]
    _list_cache["at"] = now
    _list_cache["data"] = issues
    return {"issues": issues, "cached": False}


@router.post("/api/feedback")
async def create_feedback(request: Request):
    username = get_username(request)
    if username == "anonymous":
        return _unauthenticated()
    if not GITEA_URL or not GITEA_REPO or not GITEA_TOKEN:
        return _not_configured()

    try:
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"error": "invalid body"})
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid body"})

    kind = str(body.get("type") or "bug").strip().lower()
    if kind not in TYPE_LABELS:
        kind = "bug"
    title = str(body.get("title") or "").strip()[:FEEDBACK_TITLE_MAX_CHARS]
    detail = str(body.get("body") or "").strip()[:FEEDBACK_BODY_MAX_CHARS]
    if not title:
        return JSONResponse(status_code=400, content={"error": "title required"})

    # Context the reporter can't be expected to supply but that makes a
    # report actionable. Kept to what the browser already sent us.
    ua = (request.headers.get("user-agent") or "")[:300]
    page = str(body.get("page") or "").strip()[:200]

    issue_body = (
        f"{detail}\n\n"
        f"---\n"
        f"Reported from klabnet by **{username}**\n"
        + (f"Page: `{page}`\n" if page else "")
        + (f"User agent: `{ua}`\n" if ua else "")
    )

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            res = await client.post(
                _issues_url(),
                json={"title": title, "body": issue_body},
                headers={
                    "Authorization": f"token {GITEA_TOKEN}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError:
        return JSONResponse(status_code=502, content={"error": "issue tracker unreachable"})

    if res.status_code not in (200, 201):
        return JSONResponse(status_code=502, content={"error": "could not file issue"})

    created = res.json() if res.headers.get("content-type", "").startswith("application/json") else {}
    number = created.get("number")

    # Labels are applied as a follow-up rather than inline: Gitea's create
    # call takes label IDs, not names, so posting names inline silently
    # drops them. This endpoint resolves the name once and is allowed to
    # fail without failing the report — an untagged issue still got filed.
    label = TYPE_LABELS[kind]
    if number:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                labels_res = await client.get(
                    f"{GITEA_URL.rstrip('/')}/api/v1/repos/{GITEA_REPO}/labels",
                    headers={"Authorization": f"token {GITEA_TOKEN}", "Accept": "application/json"},
                )
                if labels_res.status_code == 200:
                    match = next(
                        (l["id"] for l in labels_res.json()
                         if isinstance(l, dict) and str(l.get("name", "")).lower() == label),
                        None,
                    )
                    if match is not None:
                        await client.post(
                            f"{_issues_url()}/{number}/labels",
                            json={"labels": [match]},
                            headers={"Authorization": f"token {GITEA_TOKEN}", "Accept": "application/json"},
                        )
        except httpx.HTTPError:
            pass

    _list_cache["data"] = None  # the new issue should show up immediately
    return {
        "ok": True,
        "number": number,
        "url": created.get("html_url") or "",
    }
