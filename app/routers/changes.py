"""What changed lately, for Home's "What's new": the newest commits to
klabnet's repos on Gitea (CHANGES_REPOS). Gitea doesn't let a page on
klab.gg read its API itself (no CORS headers), so this asks it and keeps
the answer a few minutes. The repos are public, so no token is needed;
GITEA_TOKEN is sent if there is one."""
import asyncio
import re
import time
from datetime import datetime

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_username
from ..config import CHANGES_REPOS, GITEA_TOKEN, GITEA_URL

router = APIRouter()

_TTL = 300
_PER_REPO = 25
_TIMEOUT = httpx.Timeout(6.0, connect=3.0)
_cache = {"at": 0.0, "data": None}
# Trailers (Co-Authored-By:, Signed-off-by: …) aren't part of what changed.
_TRAILER = re.compile(r"^[A-Za-z-]+: .+$")


def _when(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _shape(repo: str, c: dict) -> dict:
    """Only what Home shows. A commit's message is its title line, then
    the body with any trailers dropped."""
    commit = c.get("commit") or {}
    author = commit.get("author") or {}
    lines = (commit.get("message") or "").strip().splitlines()
    title = lines[0].strip() if lines else ""
    body = "\n".join(l for l in lines[1:] if not _TRAILER.match(l.strip())).strip()
    return {
        "repo": repo.split("/")[-1],
        "sha": (c.get("sha") or "")[:7],
        "title": title,
        "body": body[:1200],
        "author": author.get("name") or "",
        "date": author.get("date") or c.get("created") or "",
        "url": c.get("html_url") or "",
    }


async def _commits(client: httpx.AsyncClient, repo: str) -> list:
    headers = {"Accept": "application/json"}
    if GITEA_TOKEN:
        headers["Authorization"] = f"token {GITEA_TOKEN}"
    res = await client.get(
        f"{GITEA_URL.rstrip('/')}/api/v1/repos/{repo}/commits",
        params={"limit": _PER_REPO, "stat": "false", "verification": "false", "files": "false"},
        headers=headers,
    )
    res.raise_for_status()
    raw = res.json()
    if not isinstance(raw, list):
        return []
    # Merges say nothing of their own: what they bring in is listed already.
    return [_shape(repo, c) for c in raw if isinstance(c, dict) and len(c.get("parents") or []) < 2]


@router.get("/api/changes")
async def changes(request: Request):
    if get_username(request) == "anonymous":
        return JSONResponse(status_code=401, content={"error": "not authenticated"})
    now = time.monotonic()
    if _cache["data"] is not None and now - _cache["at"] < _TTL:
        return {"commits": _cache["data"], "repos": [r.split("/")[-1] for r in CHANGES_REPOS]}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        got = await asyncio.gather(*(_commits(client, r) for r in CHANGES_REPOS), return_exceptions=True)
    lists = [g for g in got if isinstance(g, list)]
    if not lists:
        # Gitea is down: what was known last, or say so.
        if _cache["data"] is not None:
            return {"commits": _cache["data"], "repos": [r.split("/")[-1] for r in CHANGES_REPOS]}
        return JSONResponse(status_code=502, content={"error": "git unreachable"})
    commits = sorted((c for l in lists for c in l), key=lambda c: _when(c["date"]), reverse=True)[:40]
    _cache["at"] = now
    _cache["data"] = commits
    return {"commits": commits, "repos": [r.split("/")[-1] for r in CHANGES_REPOS]}
