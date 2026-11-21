"""klabnet-api — per-user preferences + admin app/section management + presence."""

import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import APP_VERSION, CORS_ORIGINS
from .db import init_db
from .media import media_ready
from .photos_store import backfill_derivatives
from . import encoder
from .routers import calls, changes, live, emoji, feedback, people, gifs, health, recap, identity, listens, lyrics, music_requests, notes, photos, playlists, posts, prefs, presence, profiles, radio, rooms, session, stars, studio, tv, videos, voice, week


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Photos from before medium/AVIF copies existed get them in the
    # background, so startup (and every request meanwhile) isn't held up.
    if media_ready():
        threading.Thread(target=backfill_derivatives, name="photo-backfill", daemon=True).start()
        # Which encoder videos are made with (app/encoder.py: x264, or
        # NVENC when VIDEO_ENCODER says so and it works), tested and logged
        # off the startup path.
        threading.Thread(target=encoder.choice, name="video-encoder", daemon=True).start()
        # Videos a restart caught halfway through being made.
        videos.resume_jobs()
        # Studio edits a restart caught halfway through rendering.
        studio.resume_renders()
    yield


app = FastAPI(title="klabnet-api", version=APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "PUT", "PATCH", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# photos, videos, studio, tv, rooms, week, changes, people, recap, calls and live before posts: their /api/posts/... paths must win over /api/posts/{post_id}.
# playlists, session, listens, lyrics and radio before music_requests, for the same reason (/api/music-requests/playlists/..., /activity, /radio).
for router in (health.router, identity.router, prefs.router, presence.router, listens.router, notes.router, photos.router, videos.router, studio.router, tv.router, rooms.router, week.router, stars.router, changes.router, people.router, recap.router, calls.router, live.router, emoji.router, posts.router, profiles.router, playlists.router, session.router, radio.router, lyrics.router, music_requests.router, feedback.router, gifs.router, voice.router):
    app.include_router(router)
