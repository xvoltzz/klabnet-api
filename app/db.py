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
