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
| `/api/presence` | GET, POST, DELETE | any | who's active right now + what they're listening to |
| `/api/notes` | GET, PUT, DELETE | any | Instagram-Notes-style ephemeral status text, one per user, expires after `NOTE_TTL_HOURS` |
| `/api/posts` | GET, POST | any | feed posts (text + optional `image_mxc` — an `mxc://` URI the browser uploads to Matrix's content repo directly, never routed through this service). GET responses include each post's `reactions` and `reply_count`. |
| `/api/posts/{id}` | DELETE | own post or admin | delete a post (cascades its reactions/replies) |
| `/api/posts/{id}/reactions` | PUT | any | toggle the caller's reaction (body `{emoji}`) — adds it, or removes it if already set |
| `/api/posts/{id}/replies` | GET, POST | any | flat (one-level) replies on a post |
| `/api/posts/{id}/replies/{reply_id}` | DELETE | own reply or admin | delete a reply |
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
| `/api/voice` | GET | any | voice channels and who's in each (asked of LiveKit, cached 2s). `enabled: false` when LiveKit isn't configured |
| `/api/voice/token` | POST | any | body `{channel}`: where LiveKit is and a 10-minute token for that channel's room, in the caller's name |
| `/api/voice/channels` | POST | admin | create a channel (body `{name}`) |
| `/api/voice/channels/{id}` | PATCH, DELETE | admin | rename, or delete (which disconnects whoever's in it; the last channel can't be deleted) |

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

## Environment variables

| Var | Default | Notes |
|---|---|---|
| `DB_PATH` | `/data/prefs.db` | set to `./data/prefs.db` for local dev |
| `ADMIN_GROUP` | `klabnet-admin` | Authentik group required for admin writes |
| `PRESENCE_TTL` | `30` | seconds a presence row stays "active" |
| `NOTE_TTL_HOURS` | `24` | hours a note stays visible before expiring |
| `NOTE_MAX_CHARS` | `60` | max note length |
| `POST_MAX_CHARS` | `500` | max reply / photo caption length |
| `FEED_POST_MAX_CHARS` | `10000` | max feed post length (posts are markdown) |
| `LIVEKIT_URL` | *(unset)* | voice: the `wss://` address browsers connect to. Voice is off unless this, the key and the secret are all set |
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | *(unset)* | voice: LiveKit's key pair (the same values `compose.yml` hands LiveKit as `LIVEKIT_KEYS`) |
| `LIVEKIT_API_URL` | `http://host.docker.internal:7880` in compose | voice: how this service reaches LiveKit to ask who's in a room |
| `VOICE_MAX_CHANNELS` | `20` | most voice channels there can be |
| `PLAYLIST_MAX_SONGS` | `2000` | most songs in one playlist |
| `PLAYLISTS_PER_USER` | `200` | most playlists one person can have |
| `PLAYLIST_NAME_MAX_CHARS` / `PLAYLIST_DESC_MAX_CHARS` | `80` / `600` | playlist name and description lengths |
| `PLAYLIST_COVER_MAX_MB` | `20` | largest cover upload |
| `CORS_ORIGINS` | `https://user.klab.gg,https://staging.klab.gg` | comma-separated; only matters for a browser hitting this API cross-origin, which the current frontend doesn't do (it calls relative `/api/...` paths, proxied same-origin through Caddy) |
