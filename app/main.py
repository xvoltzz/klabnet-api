"""klabnet-api — per-user preferences + admin app/section management + presence."""

import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import APP_VERSION, CORS_ORIGINS
from .db import init_db
from .media import media_ready
from .photos_store import backfill_derivatives
from .routers import feedback, health, identity, music_requests, notes, photos, posts, prefs, presence, profiles


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Photos from before medium/AVIF copies existed get them in the
    # background, so startup (and every request meanwhile) isn't held up.
    if media_ready():
        threading.Thread(target=backfill_derivatives, name="photo-backfill", daemon=True).start()
    yield


app = FastAPI(title="klabnet-api", version=APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "PUT", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# photos before posts: its /api/posts/photos/... paths must win over /api/posts/{post_id}.
for router in (health.router, identity.router, prefs.router, presence.router, notes.router, photos.router, posts.router, profiles.router, music_requests.router, feedback.router):
    app.include_router(router)
