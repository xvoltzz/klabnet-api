import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import require_admin
from ..db import get_db

router = APIRouter()

ALLOWED_SECTION_FIELDS = {"name", "sort_order"}


@router.get("/api/sections")
def get_sections():
    with get_db() as db:
        rows = db.execute("SELECT * FROM sections ORDER BY sort_order ASC, created ASC").fetchall()
    return [{"id": r["id"], "name": r["name"], "sort_order": r["sort_order"]} for r in rows]


@router.post("/api/sections")
async def create_section(request: Request):
    require_admin(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    name = str(body.get("name", "")).strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "name required"})
    sec_id = str(uuid.uuid4())[:8]
    with get_db() as db:
        max_ord = db.execute("SELECT MAX(sort_order) FROM sections").fetchone()[0] or 0
        db.execute("INSERT INTO sections(id,name,sort_order) VALUES(?,?,?)", (sec_id, name, max_ord + 1))
        db.commit()
    return {"ok": True, "id": sec_id, "name": name}


@router.put("/api/sections/{sec_id}")
async def update_section(sec_id: str, request: Request):
    require_admin(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    updates = {k: v for k, v in body.items() if k in ALLOWED_SECTION_FIELDS}
    if not updates:
        return JSONResponse(status_code=400, content={"error": "nothing to update"})
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with get_db() as db:
        db.execute(f"UPDATE sections SET {set_clause} WHERE id=?", list(updates.values()) + [sec_id])
        db.commit()
    return {"ok": True}


@router.delete("/api/sections/{sec_id}")
def delete_section(sec_id: str, request: Request):
    require_admin(request)
    with get_db() as db:
        db.execute("UPDATE apps SET section_id='applications' WHERE section_id=?", (sec_id,))
        r = db.execute("DELETE FROM sections WHERE id=?", (sec_id,))
        db.commit()
    if r.rowcount == 0:
        return JSONResponse(status_code=404, content={"error": "not found"})
    return {"ok": True}
