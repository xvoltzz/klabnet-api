"""Identity — derived from the X-Authentik-* headers Caddy injects after a
successful forward_auth check (see klabnet-web's Caddyfile config).

This service has no way to independently verify a request actually came
through that proxy — it trusts these headers unconditionally. It must only
ever be reachable via Caddy, never exposed directly.
"""

from fastapi import HTTPException, Request

from .config import ADMIN_GROUP
from .db import get_db


def get_username(request: Request) -> str:
    return request.headers.get("x-authentik-username", "anonymous").strip().lower()


def get_groups(request: Request) -> list[str]:
    raw = request.headers.get("x-authentik-groups", "")
    return [g.strip() for g in raw.split(",") if g.strip()]


def is_admin(groups: list[str]) -> bool:
    return ADMIN_GROUP in groups


def require_admin(request: Request) -> None:
    if not is_admin(get_groups(request)):
        raise HTTPException(status_code=403, detail="Admin access required")


def touch_user(username: str) -> None:
    """Upserts the `users` table's last_seen. Called from routes that are
    themselves a real "this person is here right now" signal (/api/me on
    load, presence posts) rather than on every single request — polling
    endpoints like GET /api/presence don't need to pay for a write."""
    if username == "anonymous":
        return
    with get_db() as db:
        db.execute(
            """INSERT INTO users(username) VALUES(?)
               ON CONFLICT(username) DO UPDATE SET last_seen=datetime('now')""",
            (username,),
        )
        db.commit()
