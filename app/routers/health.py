from fastapi import APIRouter

from ..config import APP_VERSION
from ..media import media_ready

router = APIRouter()


@router.get("/api/health")
def health():
    return {"status": "ok", "version": APP_VERSION, "media": media_ready()}
