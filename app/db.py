"""SQLite access — one schema, created once at startup rather than probed
on every request, and a context-managed connection helper so a handler that
raises can't leak a connection.
"""

import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

# `users` is the one real identity anchor in the DB — prefs/presence/apps
# each just carry a bare username string independently today. Anything
# built on top of accounts going forward (profiles, linking a Matrix ID,
# friend/DM state, whatever "deep social" ends up meaning) should reference
# this table instead of adding yet another parallel string column.
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username   TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS prefs (
    username TEXT PRIMARY KEY,
    data     TEXT NOT NULL DEFAULT '{}',
    updated  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS apps (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    url        TEXT NOT NULL,
    icon       TEXT NOT NULL DEFAULT 'ti-app',
    groups     TEXT NOT NULL DEFAULT '[]',
    sort_order INTEGER NOT NULL DEFAULT 0,
    section_id TEXT NOT NULL DEFAULT 'applications',
    created    TEXT NOT NULL DEFAULT (datetime('now')),
    created_by TEXT NOT NULL DEFAULT 'admin'
);
CREATE TABLE IF NOT EXISTS sections (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS presence (
    username   TEXT PRIMARY KEY,
    song       TEXT NOT NULL DEFAULT '',
    artist     TEXT NOT NULL DEFAULT '',
    song_id    TEXT NOT NULL DEFAULT '',
    playing    INTEGER NOT NULL DEFAULT 0,
    party_host TEXT NOT NULL DEFAULT '',
    updated    TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Instagram-Notes-style ephemeral status text — one per user, expires on
-- its own after NOTE_TTL_HOURS (filtered at query time in GET /api/notes,
-- same pattern PRESENCE_TTL already uses for presence — no cleanup job,
-- an expired row just stops being returned).
CREATE TABLE IF NOT EXISTS notes (
    username TEXT PRIMARY KEY,
    text     TEXT NOT NULL,
    created  TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Feed posts. image_mxc (when set) is an mxc:// URI pointing at Matrix's
-- content repo, not image bytes — the browser uploads straight to Matrix
-- (same flow the profile-avatar picker already uses) and only hands this
-- service the resulting URI, so this table never touches file storage.
CREATE TABLE IF NOT EXISTS posts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    username  TEXT NOT NULL,
    text      TEXT NOT NULL DEFAULT '',
    image_mxc TEXT NOT NULL DEFAULT '',
    created   TEXT NOT NULL DEFAULT (datetime('now'))
);
-- One row per (post, user, emoji) — a user can react to the same post with
-- several different emoji, but only once each (re-toggling removes it).
-- No FK/cascade: this app doesn't otherwise rely on SQLite foreign keys, so
-- DELETE /api/posts/{id} explicitly cleans up matching rows here instead.
CREATE TABLE IF NOT EXISTS post_reactions (
    post_id  INTEGER NOT NULL,
    username TEXT NOT NULL,
    emoji    TEXT NOT NULL,
    created  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (post_id, username, emoji)
);
-- Flat replies (one level, no reply-to-reply nesting — keeps the feed's
-- reply UI to a simple list under each post rather than real threading).
CREATE TABLE IF NOT EXISTS post_replies (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id  INTEGER NOT NULL,
    username TEXT NOT NULL,
    text     TEXT NOT NULL,
    created  TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Public-facing profile data (chat name color, bio, banner image) — unlike
-- `prefs`, this is meant to be readable by anyone, not just its owner, so it
-- gets its own table rather than living in that private per-user blob. Row
-- only exists once a user has saved a profile at least once.
CREATE TABLE IF NOT EXISTS profiles (
    username   TEXT PRIMARY KEY,
    chat_color TEXT NOT NULL DEFAULT '',
    bio        TEXT NOT NULL DEFAULT '',
    banner_mxc TEXT NOT NULL DEFAULT '',
    updated    TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Music library requests ("add this album/song") — req_type distinguishes
-- an album (mbid is a release-group) from a song (mbid is a recording);
-- cover_art_url is a Cover Art Archive URL predicted client/server-side at
-- search time (see routers/music_requests.py), not verified to exist here —
-- the frontend falls back to a placeholder icon on image load failure.
CREATE TABLE IF NOT EXISTS music_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL,
    req_type      TEXT NOT NULL DEFAULT 'album',
    title         TEXT NOT NULL,
    artist        TEXT NOT NULL DEFAULT '',
    mbid          TEXT NOT NULL DEFAULT '',
    cover_art_url TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',
    created       TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db() -> None:
    """Create tables if they don't exist yet and enable WAL mode. Called
    once from the app's lifespan startup hook (see main.py) — not on every
    request, unlike the version this replaced."""
    conn = sqlite3.connect(DB_PATH)
    try:
        # WAL lets reads and writes proceed concurrently instead of the
        # default journal mode's whole-database write lock — the
        # realistic "database is locked" failure mode under concurrent
        # requests, avoided for the cost of one pragma.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        # party_host is new — CREATE TABLE IF NOT EXISTS is a no-op against
        # an already-existing presence table (the real deployed one
        # predates this column), so it needs an explicit one-time migration
        # rather than just being added to the schema above.
        cols = [row[1] for row in conn.execute("PRAGMA table_info(presence)").fetchall()]
        if "party_host" not in cols:
            conn.execute("ALTER TABLE presence ADD COLUMN party_host TEXT NOT NULL DEFAULT ''")
        conn.commit()
    finally:
        conn.close()


@contextmanager
def get_db():
    """Yields a request-scoped connection that's always closed afterward,
    including when the handler raises. Usage: `with get_db() as db: ...`"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
