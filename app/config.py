"""Configuration — single source of truth for env vars and app metadata.

Every other module reads settings from here rather than calling
os.environ.get() itself, so there's one place to see (and change) what this
service is configurable by.
"""

import os

APP_VERSION = "2.0.0"


def _load_local_env(path: str) -> None:
    """Populate os.environ from a simple KEY=VALUE file, for local dev.

    Existing env vars always win — this only fills in what's missing, so a
    real deployment's environment (compose.yml's `environment:` block,
    systemd, etc.) always takes precedence over a stray .env file.
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_local_env(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

DB_PATH      = os.environ.get("DB_PATH", "/data/prefs.db")
ADMIN_GROUP  = os.environ.get("ADMIN_GROUP", "klabnet-admin")
PRESENCE_TTL = int(os.environ.get("PRESENCE_TTL", "30"))  # seconds
NOTE_TTL_HOURS  = int(os.environ.get("NOTE_TTL_HOURS", "24"))
NOTE_MAX_CHARS  = int(os.environ.get("NOTE_MAX_CHARS", "60"))  # same cap Instagram Notes uses
POST_MAX_CHARS  = int(os.environ.get("POST_MAX_CHARS", "500"))
EMOJI_MAX_CHARS = int(os.environ.get("EMOJI_MAX_CHARS", "32"))  # generous enough for ZWJ/skin-tone sequences
BIO_MAX_CHARS   = int(os.environ.get("BIO_MAX_CHARS", "160"))
REQUEST_FIELD_MAX_CHARS = int(os.environ.get("REQUEST_FIELD_MAX_CHARS", "200"))
SONG_FIELD_MAX_CHARS = int(os.environ.get("SONG_FIELD_MAX_CHARS", "200"))
SONG_LYRIC_MAX_CHARS = int(os.environ.get("SONG_LYRIC_MAX_CHARS", "300"))
CALENDAR_TITLE_MAX_CHARS = int(os.environ.get("CALENDAR_TITLE_MAX_CHARS", "100"))
CALENDAR_NOTES_MAX_CHARS = int(os.environ.get("CALENDAR_NOTES_MAX_CHARS", "500"))
# Caps how much listening time one presence heartbeat can credit — the
# dashboard pings every ~8s while playing, so anything much larger than
# that means a gap (a sleeping laptop, a dead connection, a very late
# heartbeat), not real continuous listening. Without this, a stale
# "playing" row that starts getting heartbeats again after hours would
# credit the entire gap as listened time.
LISTENING_HEARTBEAT_CAP_SECONDS = int(os.environ.get("LISTENING_HEARTBEAT_CAP_SECONDS", "30"))
# MusicBrainz requires a real identifying User-Agent on every request
# ("Application/Version (contact)") — an anonymous/browser-looking one gets
# rate-limited much harder or outright blocked.
MUSICBRAINZ_USER_AGENT = os.environ.get("MUSICBRAINZ_USER_AGENT", "klabnet-web/2.0 (+https://klab.gg)")

CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ORIGINS", "https://user.klab.gg,https://staging.klab.gg").split(",")
    if origin.strip()
]
