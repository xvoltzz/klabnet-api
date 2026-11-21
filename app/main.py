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
from .routers import calls, changes, chat_admin, live, emoji, feedback, people, gifs, health, recap, identity, listens, lyrics, music_requests, notes, photos, playlists, posts, prefs, presence, profiles, radio, rooms, session, stars, studio, tv, videos, voice, week


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

# Every JSON route reads its whole body into memory (request.json() /
# request.body()) before looking at it, and nothing capped that: one
# request could send gigabytes. Uploads (multipart, and Videos' parts,
# which check their own size) are left alone; everything else gets 8MB,
# four times the largest legitimate body (prefs, 2MB).
_BODY_MAX = 8 * 1024 * 1024


class _BodyLimit:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.inner(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        ctype = headers.get(b"content-type", b"").lower()
        if ctype.startswith(b"multipart/form-data") or (
                scope["method"] == "PUT" and scope["path"].startswith("/api/posts/videos/uploads/")):
            return await self.inner(scope, receive, send)
        try:
            length = int(headers.get(b"content-length") or 0)
        except ValueError:
            length = 0
        if length > _BODY_MAX:
            from starlette.responses import JSONResponse as _J
            return await _J({"error": "request body too large"}, status_code=413)(scope, receive, send)
        seen = 0

        async def counted():
            nonlocal seen
            msg = await receive()
            if msg.get("type") == "http.request":
                seen += len(msg.get("body") or b"")
                if seen > _BODY_MAX:
                    # A chunked body with no length that keeps going.
                    raise RuntimeError("request body too large")
            return msg

        return await self.inner(scope, counted, send)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "PUT", "PATCH", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.add_middleware(_BodyLimit)

# photos, videos, studio, tv, rooms, week, changes, people, recap, calls and live before posts: their /api/posts/... paths must win over /api/posts/{post_id}.
# playlists, session, listens, lyrics and radio before music_requests, for the same reason (/api/music-requests/playlists/..., /activity, /radio).
for router in (health.router, identity.router, prefs.router, presence.router, listens.router, notes.router, photos.router, videos.router, studio.router, tv.router, rooms.router, week.router, stars.router, changes.router, people.router, recap.router, chat_admin.router, calls.router, live.router, emoji.router, posts.router, profiles.router, playlists.router, session.router, radio.router, lyrics.router, music_requests.router, feedback.router, gifs.router, voice.router):
    app.include_router(router)
