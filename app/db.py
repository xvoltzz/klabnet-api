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
-- song_json (when set) is a JSON object describing a shared track —
-- {song_id, title, artist, album, cover_art, lyric} — cover_art is a
-- Navidrome coverArt id, not a URL (the frontend reconstructs the URL the
-- same way it does everywhere else); lyric is an optional quoted excerpt
-- captured from whatever line was on screen in the synced-lyrics view at
-- share time. Client-provided, not verified against the actual library —
-- same trust model as image_mxc.
CREATE TABLE IF NOT EXISTS posts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    username  TEXT NOT NULL,
    text      TEXT NOT NULL DEFAULT '',
    image_mxc TEXT NOT NULL DEFAULT '',
    song_json TEXT NOT NULL DEFAULT '',
    kind      TEXT NOT NULL DEFAULT '',
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
-- Photos tab. A photo post is a `posts` row with kind='photo'; its images
-- are rows here, ordered by position. post_id stays NULL between upload
-- and posting (the composer uploads as soon as a file is picked), and
-- unattached rows older than a day are swept on the next upload. The files
-- themselves live on the media share, not in this database (see
-- photos_store.py). exif_json holds display strings (camera, lens,
-- aperture, ...), never location.
CREATE TABLE IF NOT EXISTS photos (
    id        TEXT PRIMARY KEY,
    username  TEXT NOT NULL,
    post_id   INTEGER,
    position  INTEGER NOT NULL DEFAULT 0,
    width     INTEGER NOT NULL,
    height    INTEGER NOT NULL,
    exif_json TEXT NOT NULL DEFAULT '{}',
    created   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS photos_by_post ON photos(post_id, position);
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
        # song_json is new — same one-time migration story as party_host above.
        post_cols = [row[1] for row in conn.execute("PRAGMA table_info(posts)").fetchall()]
        if "song_json" not in post_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN song_json TEXT NOT NULL DEFAULT ''")
        # kind is new too: '' for feed posts, 'photo' for Photos-tab posts,
        # so the feed and the Photos tab each list only their own.
        if "kind" not in post_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN kind TEXT NOT NULL DEFAULT ''")
        # The calendar / "book with" feature was removed outright (not just
        # its routes) — drops the table and whatever events/booking
        # requests were already in it on the first boot after this
        # deploys. IF EXISTS makes this a no-op on every boot after that.
        conn.execute("DROP TABLE IF EXISTS calendar_events")
        # Same again for the Apps tab (service tiles + their sections) and
        # the experimental leaderboard, all removed outright rather than
        # just unrouted. This drops whatever tile/section rows and
        # accumulated listening totals were in them on the first boot after
        # this deploys; IF EXISTS makes it a no-op every boot after that.
        conn.execute("DROP TABLE IF EXISTS apps")
        conn.execute("DROP TABLE IF EXISTS sections")
        conn.execute("DROP TABLE IF EXISTS listening_stats")
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
