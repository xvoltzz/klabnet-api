from fastapi import APIRouter, Request

from ..auth import get_groups, get_username, is_admin, touch_user

router = APIRouter()


@router.get("/api/me")
def me(request: Request):
    username = get_username(request)
    groups = get_groups(request)
    touch_user(username)  # klabnet-web calls this once on every page load — a real liveness signal
    return {"username": username, "groups": groups, "is_admin": is_admin(groups)}
