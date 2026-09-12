"""klabnet-api — per-user preferences + admin app/section management + presence."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import APP_VERSION, CORS_ORIGINS
from .db import init_db
from .routers import apps, health, identity, leaderboard, music_requests, notes, posts, prefs, presence, profiles, sections


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="klabnet-api", version=APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "PUT", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

for router in (health.router, identity.router, prefs.router, presence.router, notes.router, posts.router, apps.router, sections.router, profiles.router, music_requests.router, leaderboard.router):
    app.include_router(router)
