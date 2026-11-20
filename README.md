# klabnet-api

Backend for [klabnet-web](https://git.klab.gg/xvoltzz/klabnet-web) — per-user
preferences, admin-managed app tiles/sections, and live presence ("who's on
the site, what are they playing"). FastAPI + SQLite, no dependencies on any
other service.

---

## Identity

Every route trusts `X-Authentik-Username` / `X-Authentik-Groups`, headers
Caddy injects after a successful `forward_auth` check against Authentik (see
klabnet-web's README for the Caddy config this depends on). **This service
has no way to verify a request actually came through that proxy** — it must
only ever be reachable via Caddy, never exposed directly on its own port to
anything else. Admin-only writes (`POST`/`PUT`/`DELETE` on `/api/apps` and
`/api/sections`) additionally require `ADMIN_GROUP` (default
`klabnet-admin`) in the caller's groups.

## Routes

| Route | Methods | Auth | Purpose |
|---|---|---|---|
| `/api/health` | GET | none | liveness check |
| `/api/me` | GET | any | echoes back derived identity |
| `/api/prefs` | GET, PUT | any | per-user JSON blob (theme, tile order, favorites, playlists, ...) |
| `/api/presence` | GET, POST, DELETE | any | who's active right now + what they're listening to. A beat carries `device` and `remote` (and `position`, `duration`, `album`, `coverArt`, for Activity); with klabnet open in several places, the card is written from the device that's really playing. DELETE takes away one device (`?device=`, or the phone apps' shared one without it), and the card with it only if that was the last |
| `/api/notes` | GET, PUT, DELETE | any | Each person's note, one per user: their latest, kept until they write another (clearing and DELETE keep it) |
| `/api/posts` | GET, POST | any | feed posts (text + optional `image_mxc` — an `mxc://` URI the browser uploads to Matrix's content repo directly, never routed through this service). GET responses include each post's `reactions` and `reply_count`. |
| `/api/posts/{id}` | DELETE | own post or admin | delete a post (cascades its reactions/replies) |
| `/api/posts/{id}/reactions` | PUT | any | toggle the caller's reaction (body `{emoji}`) — adds it, or removes it if already set |
| `/api/posts/{id}/replies` | GET, POST | any | flat (one-level) replies on a post |
| `/api/posts/{id}/replies/{reply_id}` | DELETE | own reply or admin | delete a reply |
| `/api/posts/emoji` | GET, POST | any | custom emoji (`:name:` in posts, chat and reactions). POST is multipart `name` + `file`: a still picture is shrunk to 128px PNG, an animated GIF/WebP kept as sent (≤512KB). Files on the media share under `emoji/`, served immutable from `/api/posts/emoji/files/<file>` |
| `/api/posts/emoji/{name}` | DELETE | who added it, or admin | delete a custom emoji |
| `/api/music-requests/lyrics?title=&artist=&album=&duration=` | GET | any | lyrics from LRCLIB, asked once for everyone and kept (`lyrics_cache`; none found is asked again after 3 days), one request at a time with LRCLIB's rate limit waited out; 503 `busy` when it couldn't be reached |
| `/api/posts/people` | GET | any | everyone klabnet knows, with first/last seen, posts, replies and time listened (Home's lists); kept a minute |
| `/api/posts/people/{username}` | GET | any | one person's profile page: counts, their posts and photos, gold (5 ⭐) posts, top artists, songs on repeat and recent listening |
| `/api/posts/recap?since=` | GET | any | the bell's recap since a Unix time (held to 7 days): replies to your posts, @mentions, reactions to your posts and photo tags (`forYou`), and new posts, photos, songs shared, featured playlists and new stations by others (`around`) |
| `/api/apps` | GET, POST, PUT, DELETE | GET: any · writes: admin | admin-managed app tiles |
| `/api/sections` | GET, POST, PUT, DELETE | GET: any · writes: admin | tile groupings |
| `/api/music-requests/playlists` | GET, POST | any | playlists (`app/routers/playlists.py`). GET: the caller's own and everyone's featured ones, as summaries (no songs). POST `{name, description?, color?, featured?, collab?, songs?}` makes one |
| `/api/music-requests/playlists/import` | POST | any | `{playlists: [{id, name, songs}]}`: playlists from when they lived in prefs. Each old id is taken once per person, ever |
| `/api/music-requests/playlists/{id}` | GET, PATCH, DELETE | GET: owner, or anyone if featured · writes: owner or admin | one playlist with its songs; edit its details; delete it |
| `/api/music-requests/playlists/{id}/songs` | POST | owner, or anyone if featured and `collab` | `{songs: [...]}`: append, skipping ones already in it |
| `/api/music-requests/playlists/{id}/songs/{song_id}` | DELETE | owner, or whoever added it on a `collab` playlist | take a song out |
| `/api/music-requests/playlists/{id}/order` | PUT | owner or admin | `{ids: [...]}`: the songs' new order |
| `/api/music-requests/playlists/{id}/save` | PUT | anyone but the owner | `{saved}`: keep someone's featured playlist in your own sidebar |
| `/api/music-requests/playlists/{id}/cover` | POST, DELETE | owner or admin | upload a cover (any image, cropped square; kept on the media share at 1000px and 300px), or remove it |
| `/api/music-requests/playlists/covers/{cover_id}/{l\|s}` | GET | any | a cover file. A new upload is a new id, so these are served as immutable |
| `/api/music-requests/activity` | GET | any | `?tz=` (the page's `getTimezoneOffset()`): Music's Activity page (`app/routers/listens.py`). Each person: `now` (what they're playing and how far in, from presence's beats), `last` (the last play of 30s or more), and `stats` (seconds today / this week / ever, plays, favourite artist, most-repeated song, seconds a day for the last seven). Plus the week's total and top five artists. Plays are counted from the presence beat of the device a person's card shows, into the `listens` table. Under `/api/music-requests/` so it rides a prefix Caddy already forwards |
| `/api/music-requests/radio` | GET, POST | any | radio stations (`app/routers/radio.py`): one long audio file each, on a loop, at `(now − epoch) mod duration` for everyone. GET: `{now, stations}`, by frequency (each with who's tuned in, from presence's `radio`). POST (multipart): `file`, `name`, `frequency` (87.5–108, at least 0.5 from any other; 409 if too close), `author?` (who it's by, if uploaded for someone; their username lets them edit it), `description?`, `color?`, `tracklist?` (`0:00 Title` a line). Lengths are read with mutagen; files up to `RADIO_MAX_MB` (1500) |
| `/api/music-requests/radio/time` | GET | any | `{now}`: this server's clock (unix seconds), which pages keep their stations against |
| `/api/music-requests/radio/{id}` | PATCH, DELETE | owner, author or admin | edit name / frequency / author / description / color / tracklist; delete it and its files |
| `/api/music-requests/radio/{id}/audio` | PUT | owner, author or admin | a new mix; the station keeps its start time |
| `/api/music-requests/radio/{id}/cover` | POST | owner, author or admin | a cover image (the playlists' cover pipeline; served at `…/playlists/covers/{cover}/{size}`) |
| `/api/music-requests/radio/{id}/audio/{file}` | GET | any | the mix, with Range; the name is per upload, so it's cached as immutable |
| `/api/music-requests/session` | GET | any | `{session: true}`: this API has the shared player (`app/routers/session.py`). An older one answers 404 or 405 |
| `/api/music-requests/session/stream` | GET | any | `?device=&name=&platform=`: a device's end of its person's session, as server-sent events. `hello` (everything as it stands), then `sync` (what changed: `now`, `queue`, `playlist`, `active`, `devices`) and `cmd` (something another device asks this one to do) |
| `/api/music-requests/session/state` | PUT | a device with its stream open | `{device, now?, queue?, playlist?, claim?}`: what the active device is playing. With `claim` the sender becomes the active device; without it, only if nobody is (otherwise 409) |
| `/api/music-requests/session/cmd` | POST | a device with its stream open | `{device, cmd, to?}`: handed to the active device (or the one named in `to`): `play`, `resume`, `pause`, `seek`, `next`, `prev`, `queue`, `set`, `take` |
| `/api/music-requests/session/release` | POST | a device with its stream open | `{device}`: the active device's page is closing |
| `/api/posts/videos` | GET, POST | any | videos (`app/routers/videos.py`), each a `posts` row with kind `video`, so reactions and replies are the posts' own routes. GET `?limit=&before_id=&offset=&user=&sort=new\|old\|popular&q=`: `{videos, total}`, ordered by `published` (`new` newest first, `old` oldest first; `before_id` pages on from that video), others' only once ready, yours always. POST (multipart): `file` (mp4, mov, mkv, webm, avi, m4v; up to `VIDEO_MAX_MB`), `title`, `description`, `published` (optional: a date or an ISO date-time, not in the future; blank is now); answers at once with `status: processing`, and ffmpeg makes the HLS ladder in the background |
| `/api/posts/videos/continue` | GET | any | your videos to carry on with (watched past 5s and short of 95%), newest first |
| `/api/posts/videos/playlists` | GET, POST | any | video playlists (`app/routers/video_playlists.py`): yours and everyone's featured ones; POST `{name, description?, featured?, videos?: [id…]}` |
| `/api/posts/videos/playlists/{id}` | GET, PATCH, DELETE | any (private: owner) / owner or admin | one with its videos in order; PATCH `name`, `description`, `featured`, `videos` (the whole order) |
| `/api/posts/videos/playlists/{id}/videos` | POST | owner or admin | `{videos: [id…]}` added at the end (`added`: how many were new) |
| `/api/posts/videos/playlists/{id}/videos/{video}` | DELETE | owner or admin | takes one out |
| `/api/posts/videos/playlists/{id}/cover` | POST, DELETE | owner or admin | a cover (multipart `file`) through Music's playlist cover pipeline, served from `/api/music-requests/playlists/covers/…` |
| `/api/posts/videos/{id}` | GET, PATCH, DELETE | any / owner or admin | one video (with `published` and `original`); edit `title`, `description`, `published`; delete it, its comments, reactions, progress, views and files |
| `/api/posts/videos/{id}/poster` | PUT | owner or admin | a thumbnail of your own (multipart `file`), in place of the frame at 10% |
| `/api/posts/videos/{id}/view` | POST | any | counts you as a viewer (views are people; once each 6 hours refreshes it) |
| `/api/posts/videos/{id}/progress` | PUT | any | `{position}`: where you stopped; 95% in or more clears it |
| `/api/posts/videos/{id}/files/{file}/{path}` | GET | any | the ladder (`master.m3u8`, `v<n>/…`), posters, storyboard and `original.mp4` (with Range requests); immutable, the file id is per upload (and per rebuild) |
| `/api/voice` | GET | any | voice channels and who's in each (asked of LiveKit, cached 2s). `enabled: false` when LiveKit isn't configured |
| `/api/voice/token` | POST | any | body `{channel}`: where LiveKit is and a 10-minute token for that channel's room, in the caller's name |
| `/api/posts/changes` | GET | any | the newest commits to klabnet's repos on Gitea (`CHANGES_REPOS`), for Home's "What's new"; kept 5 minutes. Under `/api/posts/` so it rides a prefix Caddy already forwards; `/api/changes` answers too |
| `/api/voice/channels` | POST | admin | create a channel (body `{name}`) |
| `/api/voice/channels/{id}` | PATCH, DELETE | admin | rename, or delete (which disconnects whoever's in it; the last channel can't be deleted) |
| `/api/posts/live` | GET | any | who's live (`live`: username, title, game, started, via, video, viewers, people) and the streams before (`recent`). `enabled: false` when streaming isn't set up |
| `/api/posts/live/me` | GET, PATCH | any | your WHIP and RTMP keys (made on first GET), title/game/description, and whether you're live now; PATCH title/game/description |
| `/api/posts/live/me/rotate` | POST | any | body `{which}` (`whip`, `rtmp` or `both`): new keys; the old ones stop at once |
| `/api/posts/live/{user}` | GET | any | that person's stream now (or `null`), their last one, and the next one's title |
| `/api/posts/live/{user}/token` | POST | any | a 10-minute subscribe-only LiveKit token for their stream's room |
| `/api/posts/live/{user}/chat` | GET, POST | any | the stream's chat (`after=` the last id); posting only while they're live |
| `/api/posts/live/chat/{id}` | DELETE | writer, streamer or admin | delete a chat message |

## Repository layout

```
app/
  main.py          FastAPI app, CORS, startup, router registration
  config.py        env vars + app metadata — the only place that reads os.environ
  db.py            SQLite schema + connection helper
  auth.py          identity headers, admin gate, users-table upkeep
  routers/         one file per resource — add a new one + one include_router() call to extend
requirements.txt
Dockerfile
compose.yml
```

## Local development

```bash
cp .env.example .env      # optional — only used if these env vars aren't already set
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8765
```

There's no auth locally unless you supply the identity headers yourself:

```bash
curl -H "X-Authentik-Username: you" -H "X-Authentik-Groups: klabnet-admin" \
  http://localhost:8765/api/me
```

## Deployment

Push-to-deploy, same pattern as klabnet-web: `~/docker/klabnet-api` on
192.168.0.37 is a plain directory (not its own git clone) that a bare repo's
`post-receive` hook checks out into on every push, then rebuilds/restarts:

```bash
git push origin main    # archive to Gitea (source of truth)
git push github main    # mirror to GitHub
git push prod main      # deploy — checks out + docker compose up -d --build
```

In VS Code, `.vscode/tasks.json` wires these up as tasks (`Push All` is the
default build task, `Ctrl+Shift+B`; `Deploy: Prod` pushes just `prod`).

Unlike klabnet-web there's only one deployed instance — staging.klab.gg and
user.klab.gg both proxy to the same API (see the Caddyfile), so there's no
separate `staging` remote here.

`compose.yml` builds from this directory and stores the SQLite DB at
`./data/prefs.db` (bind-mounted, gitignored) — the whole service is
self-contained in this repo; nothing outside it needs to exist for a build
to work. `data/` survives every deploy: `git checkout -f` (what the hook
runs) only ever touches tracked files.

### One-time server setup (already done if `prod` pushes work)

```bash
# On 192.168.0.37
mkdir -p ~/repos/klabnet-api.git && cd ~/repos/klabnet-api.git
git init --bare

cat > hooks/post-receive << 'EOF'
#!/bin/bash
set -e
WORK_TREE="$HOME/docker/klabnet-api"
GIT_DIR="$HOME/repos/klabnet-api.git"
git --work-tree="$WORK_TREE" --git-dir="$GIT_DIR" checkout -f main
cd "$WORK_TREE"
docker compose up -d --build
EOF
chmod +x hooks/post-receive
```

`~/docker/klabnet-api` (the work-tree) needs to already exist with `data/`
populated before the first push — it's not something the hook bootstraps.

## Videos

klabnet's own YouTube (`app/routers/videos.py`, klabnet-web's Videos page).
An upload is kept on the media share as it came (`videos/<id[:2]>/<id>/source.<ext>`)
and queued for ffmpeg, which the Docker image installs. One video is made at a
time, on one thread (which also looks every 20s for videos another process
queued, such as the importer below).

**The ladder.** HLS fMP4 rungs at 2160p, 1440p, 1080p, 720p, 480p and 360p,
each only when the source is that big, measured on the short side so portrait
phone videos count the same. x264 High at constant quality (`VIDEO_CRF`, 18)
and `VIDEO_PRESET` (`medium`; `slow` is better still if the CPU keeps up), each
rung with a bitrate ceiling (`-maxrate`, `-bufsize` twice that), so a simple
picture stays small and a busy one gets bits: 2160p 30/45 Mbps (up to 30 fps /
above), 1440p 16/24, 1080p 10/15, 720p 5/7.5, 480p 2.5/3.5, 360p 1.2/1.8.
The frame rate is the source's on every rung (60 fps stays 60; above 60 is
brought to 60; a variable-rate phone video comes out constant at its nominal
rate), and the master playlist carries `FRAME-RATE`, so players say "1080p60".
Keyframes every 4s, aligned across rungs. HDR (PQ or HLG) is tone-mapped to SDR
BT.709 for the ladder (zscale + hable; Debian's ffmpeg has zimg). Audio is
copied when it's already AAC stereo at 256k or less, otherwise AAC
`VIDEO_AUDIO_KBPS` (192k) stereo. Then two posters at 10% in (or from a
`thumb.*` beside the upload: the importer's YouTube thumbnail) and a
storyboard for the seek bar. Rotation from phones is respected. A job a
restart cut short starts again at the next startup.

Encoding is CPU-heavy: every rung at once. Measured on a 12-thread desktop at
`medium`: a minute of 1080p60 (fast motion and noise, the worst case) took
about 3 minutes; a minute of 4K30 about 5. Real footage is usually quicker.
GPU encoders (VAAPI, QSV, NVENC) aren't wired up yet.

**Original.** When the upload's codecs are ones browsers play (video H.264,
HEVC, AV1 or VP9; audio AAC, Opus, MP3 or none), it's also remuxed without
re-encoding into `original.mp4` (moov first, HDR kept), and the player offers
it as the "Original" quality. It's the upload's own bitrate straight off the
media share: on the LAN, the real thing; over the internet, as heavy as the
file. It costs disk (a second copy of the upload). Anything else (ProRes, a
lossless file) has no Original; the top rung is the best on offer.

**Published.** `videos.published` is when a video came out (settable on
upload and edit, the YouTube date when imported) and what every list orders
by; `posts.created` stays when it arrived. Videos are never Feed posts. One
that came out more than two days before it arrived (an import) is left out of
the recap's and This week's "new videos", so an import doesn't flood them.

**Tools** (run beside the API, on its database and media share; in Docker:
`docker compose exec api python -m app.tools.<name> …`):

- `import_youtube <folder> --user <username> [--dry-run] [--limit N]
  [--include-private] [--feature-public]` brings YouTube videos in with their
  dates, titles, descriptions and thumbnails, and their playlists. The folder
  can hold yt-dlp downloads (`yt-dlp --write-info-json --write-thumbnail`:
  `<name>.mp4` beside `<name>.info.json` and `<name>.jpg`; a playlist's own
  `.info.json` becomes a playlist) and/or a Google Takeout (`YouTube and
  YouTube Music/videos/*.mp4`, `video metadata/videos.csv`, `playlists/*.csv`).
  Put it somewhere the container sees, e.g. under the media share:
  `docker compose exec api python -m app.tools.import_youtube /media/import --user evan --dry-run`.
  It's idempotent (each video keeps its YouTube id in `videos.source_id`,
  each playlist its id), so a re-run skips what's done. Private videos are
  skipped unless `--include-private`; unlisted ones come in unlisted.
  Playlists come in private; public ones featured with `--feature-public`.
  The videos are queued, not made: the API's worker makes them one at a time
  (a big import takes a while), each showing its progress in Videos.
- `reencode_videos --all | --id N…` makes ladders again from the kept uploads
  (after encoder changes), each in a new folder while the old one keeps
  playing, then swapped in.
- `originals [--dry-run]` gives videos from before Original theirs (a quick
  stream copy).

The site plays it with hls.js (Safari natively). A video can be shared to the
Feed (`POST /api/posts` with `video: {id, start?}`).

## Voice

Drop-in voice channels (klabnet-web's `js/22-voice.js`). The audio runs
through a [LiveKit](https://livekit.io) server, which `compose.yml` starts
next to the API (the `livekit` service, config in `livekit.yaml`). Browsers
talk to LiveKit directly over WebRTC; this service keeps the channel names,
asks LiveKit who's in each room, and signs the token that lets someone in
(`app/routers/voice.py`). LiveKit trusts the signature, so only people
Authentik let through to `/api/voice/token` can join.

It's off until `.env` (next to `compose.yml` on the server) has:

```sh
COMPOSE_PROFILES=voice                 # makes `docker compose up` start the livekit service
LIVEKIT_URL=wss://voice.klab.gg        # what browsers connect to
LIVEKIT_API_KEY=klabnet                # any name
LIVEKIT_API_SECRET=<openssl rand -hex 32>
```

and three things outside this repo exist:

1. **DNS:** `voice.klab.gg` pointing where `user.klab.gg` does.
2. **Caddy:** a site for it that proxies to LiveKit's signalling port, with
   no `forward_auth` (LiveKit checks its own tokens), and `/api/voice*`
   added to the `/api` prefixes `user.klab.gg` and `staging.klab.gg` forward
   here:

   ```
   voice.klab.gg {
       reverse_proxy 192.168.0.37:7880
   }
   ```
3. **Router:** UDP 7882 and TCP 7881 forwarded to 192.168.0.37. That's the
   audio; it can't go through Caddy.

`docker compose up -d` then starts LiveKit, and `GET /api/voice` answers
`"enabled": true, "online": true`. `online: false` means this container
can't reach LiveKit on the host (`LIVEKIT_API_URL`, by default
`http://host.docker.internal:7880`): a host firewall is the usual reason.
Setting `LIVEKIT_API_URL=https://voice.klab.gg` in `.env` goes round through
Caddy instead.

Ports, all on the host's own network: 7880 signalling (LAN only, Caddy
reaches it), 7881/tcp and 7882/udp audio (forwarded from the internet).
LiveKit finds the public address by itself (STUN) and also hands out the
machine's LAN address, so people at home connect straight to it.

## Live streaming

People stream from OBS and everyone else watches in klabnet (klabnet-web's
`js/44-live.js`, Videos → Live; this side is `app/routers/live.py`). It
rides on Voice's LiveKit: [LiveKit Ingress](https://docs.livekit.io/home/ingress/overview/)
takes the stream in and publishes it to a LiveKit room of its own
(`live-<username>`), which viewers join subscribe-only.

- **WHIP** (OBS 30+: Settings → Stream, Service "WHIP"). Passed straight
  through, not re-encoded: what OBS sends (bitrate, 60fps) is what people
  get, under a second behind. OBS must send no B-frames (WebRTC can't carry
  them); Settings → Streaming says so.
- **RTMP**, for anything without WHIP. Re-encoded by Ingress (CPU) to a
  1080p60 / 720p / 360p simulcast ladder, the top rung `LIVE_RTMP_KBPS`
  (9000), a couple of seconds behind.

Each person gets one key of each, made the first time they open Settings →
Streaming (`POST …/me/rotate` replaces them). Who's live is LiveKit's word
(`ListIngress`, asked every 4s by a thread here), and each stream gets a
row in `live_streams` (title, game, start, end, peak viewers) and its own
chat (`live_chat`). The three tables are made by `live.py` on first use.

It's off until Voice works and `.env` also has:

```sh
COMPOSE_PROFILES=voice,live            # starts the ingress and redis services too
LIVEKIT_REDIS_HOST=127.0.0.1:6379      # LiveKit and Ingress talk through Redis
LIVE_INGRESS=1                         # the API's switch
```

and, outside this repo:

1. **DNS:** `live.klab.gg` pointing where `user.klab.gg` does.
2. **Caddy** (the owner's to change): a site for the WHIP endpoint, with no
   `forward_auth` (the bearer token is the key). `/api/posts/live` rides
   the `/api/posts` prefix already forwarded. Long-lived requests aren't
   needed: WHIP is one POST, then WebRTC goes straight to LiveKit.

   ```
   live.klab.gg {
       reverse_proxy 192.168.0.37:8085
   }
   ```
3. **Router:** TCP 1935 forwarded to 192.168.0.37 for RTMP. WHIP's media
   uses LiveKit's own 7881/tcp and 7882/udp, forwarded already for voice.

`livekit.yaml`'s `ingress:` block holds the addresses shown in Settings
(`rtmp://live.klab.gg:1935/x`, `https://live.klab.gg/w`): change them there
if the names differ. Ports, all on the host's own network: 1935 RTMP
(forwarded), 8085 WHIP (Caddy), 9090 Ingress's internal relay, 8086 its
health check, 6379 Redis (127.0.0.1 only), 7885/udp only for a WHIP that's
re-encoded (klabnet never asks for that).

`docker compose up -d` and then Settings → Streaming shows the keys;
streaming from OBS makes the Status line say Live with the resolution and
bitrate klabnet sees. "LiveKit Ingress isn't answering" means the ingress
container isn't up or can't reach Redis.

Desktop streaming straight from klabnet-desktop (no OBS) isn't built: it's
planned with the desktop recorder, publishing over WHIP the same way.

## Environment variables

| Var | Default | Notes |
|---|---|---|
| `DB_PATH` | `/data/prefs.db` | set to `./data/prefs.db` for local dev |
| `ADMIN_GROUP` | `klabnet-admin` | Authentik group required for admin writes |
| `PRESENCE_TTL` | `30` | seconds a presence row stays "active" |
| `NOTE_MAX_CHARS` | `60` | max note length |
| `POST_MAX_CHARS` | `500` | max reply / photo caption length |
| `FEED_POST_MAX_CHARS` | `10000` | max feed post length (posts are markdown) |
| `LIVEKIT_URL` | *(unset)* | voice: the `wss://` address browsers connect to. Voice is off unless this, the key and the secret are all set |
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | *(unset)* | voice: LiveKit's key pair (the same values `compose.yml` hands LiveKit as `LIVEKIT_KEYS`) |
| `LIVEKIT_API_URL` | `http://host.docker.internal:7880` in compose | voice: how this service reaches LiveKit to ask who's in a room |
| `VOICE_MAX_CHANNELS` | `20` | most voice channels there can be |
| `LIVE_INGRESS` | *(unset)* | live streaming: `1` turns it on (with voice's `LIVEKIT_*` set and the `live` compose profile running) |
| `LIVE_RTMP_KBPS` | `9000` | live streaming: the top rung RTMP streams are re-encoded to (1080p60) |
| `LIVEKIT_REDIS_HOST` | *(unset)* | compose only: handed to LiveKit as `REDIS_HOST`; `127.0.0.1:6379` for live streaming |
| `PLAYLIST_MAX_SONGS` | `2000` | most songs in one playlist |
| `PLAYLISTS_PER_USER` | `200` | most playlists one person can have |
| `PLAYLIST_NAME_MAX_CHARS` / `PLAYLIST_DESC_MAX_CHARS` | `80` / `600` | playlist name and description lengths |
| `PLAYLIST_COVER_MAX_MB` | `20` | largest cover upload |
| `VIDEO_MAX_MB` | `4096` | largest video upload. Caddy's request body limit (if it has one) has to allow it too |
| `VIDEO_TITLE_MAX_CHARS` / `VIDEO_DESC_MAX_CHARS` | `100` / `5000` | video title and description lengths |
| `VIDEO_PRESET` | `medium` | x264's preset for the ladder (`slow` is better if the CPU keeps up) |
| `VIDEO_CRF` | `18` | x264's constant quality for the ladder (lower is better and bigger); each rung also has a bitrate ceiling |
| `VIDEO_AUDIO_KBPS` | `192` | the ladder's AAC bitrate when the source's audio isn't already AAC stereo |
| `CORS_ORIGINS` | `https://user.klab.gg,https://staging.klab.gg` | comma-separated; only matters for a browser hitting this API cross-origin, which the current frontend doesn't do (it calls relative `/api/...` paths, proxied same-origin through Caddy) |
