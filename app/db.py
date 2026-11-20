"""SQLite access — one schema, created once at startup rather than probed
on every request, and a context-managed connection helper so a handler that
raises can't leak a connection.
"""

import hashlib
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
    platform   TEXT NOT NULL DEFAULT '',
    updated    TEXT NOT NULL DEFAULT (datetime('now')),
    active_at  TEXT NOT NULL DEFAULT '',
    idle_known_at TEXT NOT NULL DEFAULT '',
    locked_at  TEXT NOT NULL DEFAULT '',
    lock_seen_at TEXT NOT NULL DEFAULT ''
);
-- Each person's note: one per user, their latest. Notes used to expire
-- after NOTE_TTL_HOURS; they don't now, and clearing keeps the last one
-- (routers/notes.py).
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
    shot_at   TEXT NOT NULL DEFAULT '',
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
-- Stars on music (songs and albums): one row per (kind, item, user). Posts
-- and photos star with a ⭐ post_reaction and chat with a ⭐ Matrix
-- reaction; this is for things that live in Navidrome, not here. item_id is
-- the Navidrome id. Five people starring something turns it gold.
CREATE TABLE IF NOT EXISTS stars (
    kind     TEXT NOT NULL,
    item_id  TEXT NOT NULL,
    username TEXT NOT NULL,
    created  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (kind, item_id, username)
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
-- Every feed and photos page counts each post's replies.
CREATE INDEX IF NOT EXISTS post_replies_by_post ON post_replies(post_id, id);
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
-- People tagged in a post ("we were on this shoot together"). Kept as rows
-- rather than a JSON column so a profile can later list photos someone
-- appears in.
CREATE TABLE IF NOT EXISTS post_tags (
    post_id  INTEGER NOT NULL,
    username TEXT NOT NULL,
    PRIMARY KEY (post_id, username)
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
-- The GIF stash: klabnet's own GIFs, added by pasting a link or uploading.
-- id is a hash of the file, so the same GIF added twice is one row.
CREATE TABLE IF NOT EXISTS gifs (
    id        TEXT PRIMARY KEY,
    username  TEXT NOT NULL,
    name      TEXT NOT NULL DEFAULT '',
    width     INTEGER NOT NULL DEFAULT 0,
    height    INTEGER NOT NULL DEFAULT 0,
    bytes     INTEGER NOT NULL DEFAULT 0,
    uses      INTEGER NOT NULL DEFAULT 0,
    created   TEXT NOT NULL DEFAULT (datetime('now')),
    last_used TEXT
);

-- Voice channels (routers/voice.py): just the names. Who's in one is
-- LiveKit's to say, not this table's; room "klabnet-voice-<id>" there.
CREATE TABLE IF NOT EXISTS voice_channels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT '',
    created    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Playlists (routers/playlists.py). Private to `username` until featured;
-- featured ones are listed for everyone, and with collab other people can
-- add songs. cover names its files on the media share ('' = none; the
-- site then shows the first songs' album art). legacy_id is the id it had
-- in the owner's prefs, for playlists brought over from there.
CREATE TABLE IF NOT EXISTS playlists (
    id          TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    color       TEXT NOT NULL DEFAULT '',
    cover       TEXT NOT NULL DEFAULT '',
    featured    INTEGER NOT NULL DEFAULT 0,
    collab      INTEGER NOT NULL DEFAULT 0,
    legacy_id   TEXT NOT NULL DEFAULT '',
    created     TEXT NOT NULL DEFAULT (datetime('now')),
    updated     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS playlists_by_user ON playlists(username);
CREATE INDEX IF NOT EXISTS playlists_featured ON playlists(featured, updated);
-- A song is in a playlist once. song_json is the client's record (what
-- favourites keep: ids, names, length); cover_art and duration are copied
-- out of it so lists can count and total without parsing every song.
CREATE TABLE IF NOT EXISTS playlist_songs (
    playlist_id TEXT NOT NULL,
    song_id     TEXT NOT NULL,
    position    INTEGER NOT NULL,
    song_json   TEXT NOT NULL,
    cover_art   TEXT NOT NULL DEFAULT '',
    duration    INTEGER NOT NULL DEFAULT 0,
    added_by    TEXT NOT NULL DEFAULT '',
    added       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (playlist_id, song_id)
);
CREATE INDEX IF NOT EXISTS playlist_songs_order ON playlist_songs(playlist_id, position);
-- Someone else's featured playlist, kept in your own sidebar.
CREATE TABLE IF NOT EXISTS playlist_saves (
    playlist_id TEXT NOT NULL,
    username    TEXT NOT NULL,
    created     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (playlist_id, username)
);
-- Every prefs-era playlist id ever brought over, kept after the playlist
-- itself is deleted, so a device still holding the old list can't bring it
-- back.
CREATE TABLE IF NOT EXISTS playlist_imports (
    username  TEXT NOT NULL,
    legacy_id TEXT NOT NULL,
    PRIMARY KEY (username, legacy_id)
);

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
-- High-resolution album artwork found in an outside catalogue, for display
-- only (Navidrome's own art and files are never touched). One row per
-- album, including albums nothing was found for (large = '') so they
-- aren't looked up again every time; those are retried after a while.
-- The images themselves are kept on the media share (art/<id>-l, -f) the
-- first time anyone asks for them; id names them.
CREATE TABLE IF NOT EXISTS art_cache (
    key     TEXT PRIMARY KEY,
    large   TEXT NOT NULL DEFAULT '',
    full    TEXT NOT NULL DEFAULT '',
    source  TEXT NOT NULL DEFAULT '',
    checked TEXT NOT NULL DEFAULT (datetime('now')),
    id      TEXT NOT NULL DEFAULT ''
);
-- One play of a song: who, what, and how long it's played (adds up from
-- presence's beats; routers/listens.py). For Music's Activity page.
CREATE TABLE IF NOT EXISTS listens (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    username  TEXT NOT NULL,
    song_id   TEXT NOT NULL,
    song      TEXT NOT NULL DEFAULT '',
    artist    TEXT NOT NULL DEFAULT '',
    album     TEXT NOT NULL DEFAULT '',
    cover_art TEXT NOT NULL DEFAULT '',
    started   TEXT NOT NULL DEFAULT (datetime('now')),
    last      TEXT NOT NULL DEFAULT (datetime('now')),
    seconds   REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS listens_user_last ON listens(username, last);
CREATE INDEX IF NOT EXISTS listens_started ON listens(started);
-- Radio (routers/radio.py): a station is one long file on a loop, at the
-- same point for everyone: (now - epoch) mod duration.
CREATE TABLE IF NOT EXISTS radio_stations (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    color       TEXT NOT NULL DEFAULT '',
    owner       TEXT NOT NULL,
    file_id     TEXT NOT NULL,
    ext         TEXT NOT NULL,
    duration    REAL NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0,
    epoch       REAL NOT NULL,
    cover       TEXT NOT NULL DEFAULT '',
    tracklist   TEXT NOT NULL DEFAULT '[]',
    position    INTEGER NOT NULL DEFAULT 0,
    frequency   REAL NOT NULL DEFAULT 0,
    author      TEXT NOT NULL DEFAULT '',
    created     TEXT NOT NULL DEFAULT (datetime('now')),
    updated     TEXT NOT NULL DEFAULT (datetime('now'))
);
-- A station's chat (routers/radio.py): what was said on it, and what was
-- on when it was said. The newest 300 per station are kept.
CREATE TABLE IF NOT EXISTS radio_chat (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    station  TEXT NOT NULL,
    username TEXT NOT NULL,
    body     TEXT NOT NULL,
    ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS radio_chat_station ON radio_chat(station, id);
-- Bug reports and ideas from the in-app button (routers/feedback.py).
-- state: open, done or wontfix; reply: what an admin said about it.
CREATE TABLE IF NOT EXISTS feedback (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    kind     TEXT NOT NULL DEFAULT 'bug',
    title    TEXT NOT NULL,
    body     TEXT NOT NULL DEFAULT '',
    page     TEXT NOT NULL DEFAULT '',
    version  TEXT NOT NULL DEFAULT '',
    ua       TEXT NOT NULL DEFAULT '',
    state    TEXT NOT NULL DEFAULT 'open',
    reply    TEXT NOT NULL DEFAULT '',
    created  TEXT NOT NULL DEFAULT (datetime('now')),
    updated  TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Custom emoji (routers/emoji.py): :name: in posts, chat and reactions.
-- file: its file on the media share, emoji/<file>; a new upload is a new file.
CREATE TABLE IF NOT EXISTS custom_emoji (
    name     TEXT PRIMARY KEY,
    file     TEXT NOT NULL,
    username TEXT NOT NULL,
    animated INTEGER NOT NULL DEFAULT 0,
    created  TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Videos (routers/videos.py). A video is a `posts` row with kind='video'
-- (its text is the description), so reactions and replies are the posts'
-- own; this row is the rest, keyed by that post id. Files live on the media
-- share under videos/<file_id[:2]>/<file_id>/: the original as source.<ext>,
-- the HLS ladder (master.m3u8, v<n>/…), posters and a storyboard. status is
-- processing (queued or being made), ready or failed. rungs: the ladder as
-- JSON; storyboard: its tile grid as JSON; poster: the stem of the poster
-- files (a new one per custom upload, so URLs never change underneath).
CREATE TABLE IF NOT EXISTS videos (
    post_id    INTEGER PRIMARY KEY,
    title      TEXT NOT NULL,
    file_id    TEXT NOT NULL,
    ext        TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'processing',
    progress   REAL NOT NULL DEFAULT 0,
    error      TEXT NOT NULL DEFAULT '',
    duration   REAL,
    width      INTEGER NOT NULL DEFAULT 0,
    height     INTEGER NOT NULL DEFAULT 0,
    size       INTEGER NOT NULL DEFAULT 0,
    rungs      TEXT NOT NULL DEFAULT '[]',
    storyboard TEXT NOT NULL DEFAULT '',
    poster     TEXT NOT NULL DEFAULT '',
    unlisted   INTEGER NOT NULL DEFAULT 0,
    updated    TEXT NOT NULL DEFAULT (datetime('now'))
);
-- TV channels (routers/tv.py): a lineup of videos on a loop, at the same
-- point for everyone, like a radio station: (now - epoch) mod the lineup's
-- length. number is the channel's place on the dial (1-99). A video
-- uploaded straight into a channel is unlisted (videos.unlisted): it plays
-- there and stays out of Videos' lists.
CREATE TABLE IF NOT EXISTS tv_channels (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    number      INTEGER NOT NULL UNIQUE,
    color       TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    cover       TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL,
    epoch       REAL NOT NULL,
    created     TEXT NOT NULL DEFAULT (datetime('now')),
    updated     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS tv_items (
    channel_id TEXT NOT NULL,
    pos        INTEGER NOT NULL,
    video_id   INTEGER NOT NULL,
    PRIMARY KEY (channel_id, pos)
);
CREATE INDEX IF NOT EXISTS tv_items_video ON tv_items(video_id);
-- TV programs (routers/tv.py, the guide): a block scheduled on a channel
-- at a set start (unix seconds, UTC), played from its start for everyone;
-- outside programs the channel's loop runs on as if they weren't there.
-- aired is decided once the program starts: 1 the item plays, 0 it wasn't
-- ready and is skipped, NULL not started yet.
CREATE TABLE IF NOT EXISTS tv_programs (
    id         TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    start      REAL NOT NULL,
    title      TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS tv_programs_start ON tv_programs(channel_id, start);
CREATE TABLE IF NOT EXISTS tv_program_items (
    program_id TEXT NOT NULL,
    pos        INTEGER NOT NULL,
    video_id   INTEGER NOT NULL,
    aired      INTEGER,
    PRIMARY KEY (program_id, pos)
);
-- Where each person stopped in a video; cleared once they finish it.
CREATE TABLE IF NOT EXISTS video_progress (
    username TEXT NOT NULL,
    post_id  INTEGER NOT NULL,
    position REAL NOT NULL,
    updated  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (username, post_id)
);
CREATE INDEX IF NOT EXISTS video_progress_recent ON video_progress(username, updated);
-- Who's watched a video: one row a person (views are people), `at` the
-- last time it counted.
CREATE TABLE IF NOT EXISTS video_views (
    post_id  INTEGER NOT NULL,
    username TEXT NOT NULL,
    at       REAL NOT NULL,
    PRIMARY KEY (post_id, username)
);
-- Video playlists (routers/video_playlists.py): Music's playlists for
-- videos. Private to `username` until featured; a featured one is listed
-- for everyone. cover is a cover id of Music's playlist cover pipeline
-- (playlists/<id[:2]>/<id>-l|s.jpg on the media share, '' = the first
-- videos' posters as a 2x2). source_id: the YouTube playlist it was
-- imported from (app/tools/import_youtube.py), so a re-run skips it.
CREATE TABLE IF NOT EXISTS video_playlists (
    id          TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    cover       TEXT NOT NULL DEFAULT '',
    featured    INTEGER NOT NULL DEFAULT 0,
    source_id   TEXT NOT NULL DEFAULT '',
    created     TEXT NOT NULL DEFAULT (datetime('now')),
    updated     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS video_playlists_by_user ON video_playlists(username);
CREATE INDEX IF NOT EXISTS video_playlists_featured ON video_playlists(featured, updated);
CREATE TABLE IF NOT EXISTS video_playlist_items (
    playlist_id TEXT NOT NULL,
    pos         INTEGER NOT NULL,
    video_id    INTEGER NOT NULL,
    added_by    TEXT NOT NULL DEFAULT '',
    added       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (playlist_id, video_id)
);
CREATE INDEX IF NOT EXISTS video_playlist_items_order ON video_playlist_items(playlist_id, pos);
CREATE INDEX IF NOT EXISTS video_playlist_items_video ON video_playlist_items(video_id);
-- Lyrics from LRCLIB (routers/lyrics.py), one row a song as the site
-- names it (title, artist, album, length). body is LRCLIB's answer as
-- JSON, or '' for "none there": kept a few days, then asked again.
CREATE TABLE IF NOT EXISTS lyrics_cache (
    key     TEXT PRIMARY KEY,
    body    TEXT NOT NULL DEFAULT '',
    checked TEXT NOT NULL DEFAULT (datetime('now'))
);
-- This week on klabnet (routers/week.py): reactions to a line of a week
-- and replies under it. week is that week's Monday (YYYY-MM-DD), line the
-- line's key ("song", "found", "videos"...). about: the people the line
-- was about when this was added (",alice,bob,"), and label what the line
-- is ("most played song"), for the recap.
CREATE TABLE IF NOT EXISTS week_reactions (
    week     TEXT NOT NULL,
    line     TEXT NOT NULL,
    username TEXT NOT NULL,
    emoji    TEXT NOT NULL,
    about    TEXT NOT NULL DEFAULT '',
    label    TEXT NOT NULL DEFAULT '',
    created  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (week, line, username, emoji)
);
CREATE TABLE IF NOT EXISTS week_replies (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    week     TEXT NOT NULL,
    line     TEXT NOT NULL,
    username TEXT NOT NULL,
    text     TEXT NOT NULL,
    about    TEXT NOT NULL DEFAULT '',
    label    TEXT NOT NULL DEFAULT '',
    created  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS week_replies_by_line ON week_replies(week, line, id);
-- The video studio (routers/studio.py): annotations, cards and end
-- screens, all in the edited timeline (seconds), positions as % of the
-- frame. style/link/body/elements are JSON. A poll is a card (body:
-- question, options); its votes are one row a person.
CREATE TABLE IF NOT EXISTS video_annotations (
    video_id INTEGER NOT NULL,
    id       TEXT NOT NULL,
    type     TEXT NOT NULL,
    start    REAL NOT NULL,
    "end"    REAL NOT NULL,
    x        REAL NOT NULL,
    y        REAL NOT NULL,
    w        REAL NOT NULL,
    h        REAL NOT NULL,
    text     TEXT NOT NULL DEFAULT '',
    style    TEXT NOT NULL DEFAULT '',
    link     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (video_id, id)
);
CREATE TABLE IF NOT EXISTS video_cards (
    video_id INTEGER NOT NULL,
    id       TEXT NOT NULL,
    kind     TEXT NOT NULL,
    at       REAL NOT NULL,
    teaser   TEXT NOT NULL DEFAULT '',
    body     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (video_id, id)
);
CREATE TABLE IF NOT EXISTS video_card_votes (
    video_id INTEGER NOT NULL,
    card_id  TEXT NOT NULL,
    username TEXT NOT NULL,
    option   INTEGER NOT NULL,
    at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (video_id, card_id, username)
);
CREATE TABLE IF NOT EXISTS video_endscreen (
    video_id     INTEGER PRIMARY KEY,
    start_offset REAL NOT NULL,
    elements     TEXT NOT NULL DEFAULT '[]'
);
"""


def art_id(key: str) -> str:
    """The name an album's artwork files go by on the media share."""
    return hashlib.sha1(key.encode()).hexdigest()[:20]


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
        # platform (what someone's on: "app:windows", "web:macos:firefox")
        # came later still; same migration.
        if "platform" not in cols:
            conn.execute("ALTER TABLE presence ADD COLUMN platform TEXT NOT NULL DEFAULT ''")
        # Away: when they last touched a keyboard/mouse (any device), and
        # when a device that can see the whole computer last said so.
        if "active_at" not in cols:
            conn.execute("ALTER TABLE presence ADD COLUMN active_at TEXT NOT NULL DEFAULT ''")
        if "idle_known_at" not in cols:
            conn.execute("ALTER TABLE presence ADD COLUMN idle_known_at TEXT NOT NULL DEFAULT ''")
        # ...and when their screen was locked (lock_seen_at: last told so).
        for col in ("locked_at", "lock_seen_at"):
            if col not in cols:
                conn.execute(f"ALTER TABLE presence ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        # Radio stations got a frequency (MHz, on the Radio page's dial)
        # after the first ones were made: those get a free one each.
        radio_cols = [row[1] for row in conn.execute("PRAGMA table_info(radio_stations)").fetchall()]
        if "frequency" not in radio_cols:
            conn.execute("ALTER TABLE radio_stations ADD COLUMN frequency REAL NOT NULL DEFAULT 0")
        # author: who the station's by, when it was uploaded for them.
        if "author" not in radio_cols:
            conn.execute("ALTER TABLE radio_stations ADD COLUMN author TEXT NOT NULL DEFAULT ''")
        taken = [r[0] for r in conn.execute("SELECT frequency FROM radio_stations WHERE frequency > 0").fetchall()]
        for (sid,) in conn.execute("SELECT id FROM radio_stations WHERE frequency <= 0 ORDER BY created").fetchall():
            f = next(x / 10 for x in range(881, 1080, 20) if all(abs(x / 10 - t) >= 0.5 for t in taken))
            taken.append(f)
            conn.execute("UPDATE radio_stations SET frequency=? WHERE id=?", (f, sid))
        # song_json is new — same one-time migration story as party_host above.
        post_cols = [row[1] for row in conn.execute("PRAGMA table_info(posts)").fetchall()]
        if "song_json" not in post_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN song_json TEXT NOT NULL DEFAULT ''")
        # video_json: a video shared in a feed post ({id, start}), routers/videos.py.
        if "video_json" not in post_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN video_json TEXT NOT NULL DEFAULT ''")
        # kind is new too: '' for feed posts, 'photo' for Photos-tab posts,
        # so the feed and the Photos tab each list only their own.
        if "kind" not in post_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN kind TEXT NOT NULL DEFAULT ''")
        # shot_at: when a photo post was taken ("YYYY-MM-DD HH:MM:SS", local
        # wall time), so old photos uploaded today sort where they belong.
        # '' means unknown, and the post sorts by when it was posted.
        if "shot_at" not in post_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN shot_at TEXT NOT NULL DEFAULT ''")
        # edited: when a feed post's text was last changed (PATCH /api/posts/{id}),
        # '' if never. Shown as "edited" beside the time.
        if "edited" not in post_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN edited TEXT NOT NULL DEFAULT ''")
        # videos.unlisted: a video uploaded straight into a TV channel
        # (routers/tv.py), kept out of Videos' lists.
        video_cols = [row[1] for row in conn.execute("PRAGMA table_info(videos)").fetchall()]
        if "unlisted" not in video_cols:
            conn.execute("ALTER TABLE videos ADD COLUMN unlisted INTEGER NOT NULL DEFAULT 0")
        # videos.published: when it came out (settable, e.g. a YouTube
        # video's own date when imported), which every list orders by;
        # posts.created stays when it arrived on klabnet. Rows from before
        # get their created time. source_id: the YouTube id it was imported
        # from (app/tools/import_youtube.py), so a re-run skips it.
        # original: the upload remuxed for direct play (source.mp4) as JSON,
        # '' = none (routers/videos.py).
        if "published" not in video_cols:
            conn.execute("ALTER TABLE videos ADD COLUMN published TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE videos SET published = COALESCE((SELECT created FROM posts WHERE posts.id = videos.post_id), datetime('now')) WHERE published = ''")
        if "source_id" not in video_cols:
            conn.execute("ALTER TABLE videos ADD COLUMN source_id TEXT NOT NULL DEFAULT ''")
        if "original" not in video_cols:
            conn.execute("ALTER TABLE videos ADD COLUMN original TEXT NOT NULL DEFAULT ''")
        # The studio's editor (routers/studio.py): edit is the edit list
        # wanted (JSON, '' none), edit_live the one the files are made from,
        # render ''/queued/rendering/failed with its progress and error, and
        # base what the unedited ladder kept in base/ is (JSON).
        for col, kind in (("edit", "TEXT NOT NULL DEFAULT ''"), ("edit_live", "TEXT NOT NULL DEFAULT ''"),
                          ("render", "TEXT NOT NULL DEFAULT ''"), ("render_progress", "REAL NOT NULL DEFAULT 0"),
                          ("render_error", "TEXT NOT NULL DEFAULT ''"), ("base", "TEXT NOT NULL DEFAULT ''")):
            if col not in video_cols:
                conn.execute(f"ALTER TABLE videos ADD COLUMN {col} {kind}")
        conn.execute("CREATE INDEX IF NOT EXISTS videos_published ON videos(published, post_id)")
        # Clips (routers/clips.py): kind '' is a video, 'clip' a clip (kept
        # out of Videos' own lists). game: free text, merged by case. A clip
        # made from a range of another video: clip_of (that video's id) and
        # clip_start/clip_end (seconds in it); its source.mkv is cut from
        # that video's upload by the worker before the ladder is made.
        if "kind" not in video_cols:
            conn.execute("ALTER TABLE videos ADD COLUMN kind TEXT NOT NULL DEFAULT ''")
        if "game" not in video_cols:
            conn.execute("ALTER TABLE videos ADD COLUMN game TEXT NOT NULL DEFAULT ''")
        if "clip_of" not in video_cols:
            conn.execute("ALTER TABLE videos ADD COLUMN clip_of INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE videos ADD COLUMN clip_start REAL NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE videos ADD COLUMN clip_end REAL NOT NULL DEFAULT 0")
        # clip_cut: what the worker cuts, decided when the clip was asked
        # for (JSON: the upload's ranges, with the edit's mutes, gain, crop
        # and rotation), so a later edit of the video can't change it.
        if "clip_cut" not in video_cols:
            conn.execute("ALTER TABLE videos ADD COLUMN clip_cut TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS videos_kind ON videos(kind, published, post_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS videos_source ON videos(source_id) WHERE source_id != ''")
        # art_cache.id (which file on the media share an album's art is)
        # came after the table did; same migration, and rows from before
        # get theirs from their key.
        art_cols = [row[1] for row in conn.execute("PRAGMA table_info(art_cache)").fetchall()]
        if "id" not in art_cols:
            conn.execute("ALTER TABLE art_cache ADD COLUMN id TEXT NOT NULL DEFAULT ''")
        for (key,) in conn.execute("SELECT key FROM art_cache WHERE id=''").fetchall():
            conn.execute("UPDATE art_cache SET id=? WHERE key=?", (art_id(key), key))
        conn.execute("CREATE INDEX IF NOT EXISTS art_cache_id ON art_cache(id)")
        # Photo posts made before shot_at existed have none, so they sorted by
        # when they were posted. Fill each from the earliest capture time in
        # its photos' EXIF, the same default a new post gets. Only touches
        # undated photo posts that have one, so it's a no-op once they're filled.
        conn.execute(
            """UPDATE posts SET shot_at = (
                   SELECT MIN(json_extract(exif_json, '$.taken')) FROM photos
                   WHERE photos.post_id = posts.id AND COALESCE(json_extract(exif_json, '$.taken'), '') != '')
               WHERE kind = 'photo' AND shot_at = ''
                 AND EXISTS (SELECT 1 FROM photos WHERE photos.post_id = posts.id
                             AND COALESCE(json_extract(exif_json, '$.taken'), '') != '')"""
        )
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
        # Voice starts with one channel to drop into. The last channel can't
        # be deleted (routers/voice.py), so this only ever runs once.
        if not conn.execute("SELECT 1 FROM voice_channels LIMIT 1").fetchone():
            conn.execute("INSERT INTO voice_channels(name) VALUES ('Lounge')")
        conn.commit()
    finally:
        conn.close()


@contextmanager
def get_db():
    """Yields a request-scoped connection that's always closed afterward,
    including when the handler raises. Usage: `with get_db() as db: ...`"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # With WAL (init_db), NORMAL only risks the last moments of writes on a
    # power cut, not corruption, and saves an fsync on every commit.
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
    finally:
        conn.close()
