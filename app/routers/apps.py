import json
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_groups, get_username, require_admin
from ..db import get_db

router = APIRouter()

ALLOWED_APP_FIELDS = {"name", "url", "icon", "groups", "sort_order", "section_id"}


@router.get("/api/apps")
def get_apps(request: Request):
    user_groups = get_groups(request)
    with get_db() as db:
        rows = db.execute("SELECT * FROM apps ORDER BY section_id, sort_order ASC, created ASC").fetchall()
    result = []
    for row in rows:
        app_groups = json.loads(row["groups"])
        if not app_groups or any(g in user_groups for g in app_groups):
            result.append({
                "id":         row["id"],
                "name":       row["name"],
                "url":        row["url"],
                "icon":       row["icon"],
                "groups":     app_groups,
                "section_id": row["section_id"],
            })
    return result


@router.post("/api/apps")
async def create_app(request: Request):
    require_admin(request)
    username = get_username(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    name       = str(body.get("name", "")).strip()
    url        = str(body.get("url", "")).strip()
    icon       = str(body.get("icon", "ti-app")).strip()
    groups     = body.get("groups", [])
    section_id = str(body.get("section_id", "applications")).strip()
    if not isinstance(groups, list):
        groups = []
    if not name or not url:
        return JSONResponse(status_code=400, content={"error": "name and url required"})
    if not url.startswith("http"):
        url = "https://" + url
    app_id = str(uuid.uuid4())[:8]
    with get_db() as db:
        max_ord = db.execute("SELECT MAX(sort_order) FROM apps").fetchone()[0] or 0
        db.execute(
            "INSERT INTO apps(id,name,url,icon,groups,sort_order,section_id,created_by) VALUES(?,?,?,?,?,?,?,?)",
            (app_id, name, url, icon, json.dumps(groups), max_ord + 1, section_id, username),
        )
        db.commit()
    return {"ok": True, "id": app_id, "name": name, "url": url, "icon": icon,
            "groups": groups, "section_id": section_id}


@router.put("/api/apps/{app_id}")
async def update_app(app_id: str, request: Request):
    require_admin(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    updates = {k: v for k, v in body.items() if k in ALLOWED_APP_FIELDS}
    if not updates:
        return JSONResponse(status_code=400, content={"error": "nothing to update"})
    if "groups" in updates:
        # get_apps() does `any(g in user_groups for g in app_groups)` after
        # json.loads() — if this stored anything but a list (create_app
        # already guards this, update_app didn't), a string would iterate
        # character-by-character there instead of raising, silently
        # breaking that tile's group visibility for everyone rather than
        # erroring loudly.
        if not isinstance(updates["groups"], list):
            updates["groups"] = []
        updates["groups"] = json.dumps(updates["groups"])
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with get_db() as db:
        db.execute(f"UPDATE apps SET {set_clause} WHERE id=?", list(updates.values()) + [app_id])
        db.commit()
    return {"ok": True}


@router.delete("/api/apps/{app_id}")
def delete_app(app_id: str, request: Request):
    require_admin(request)
    with get_db() as db:
        r = db.execute("DELETE FROM apps WHERE id=?", (app_id,))
        db.commit()
    if r.rowcount == 0:
        return JSONResponse(status_code=404, content={"error": "not found"})
    return {"ok": True}
