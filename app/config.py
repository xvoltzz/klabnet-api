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

CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ORIGINS", "https://user.klab.gg,https://staging.klab.gg").split(",")
    if origin.strip()
]
