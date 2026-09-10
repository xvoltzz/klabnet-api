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

## Environment variables

| Var | Default | Notes |
|---|---|---|
| `DB_PATH` | `/data/prefs.db` | set to `./data/prefs.db` for local dev |
| `ADMIN_GROUP` | `klabnet-admin` | Authentik group required for admin writes |
| `PRESENCE_TTL` | `30` | seconds a presence row stays "active" |
| `NOTE_TTL_HOURS` | `24` | hours a note stays visible before expiring |
| `NOTE_MAX_CHARS` | `60` | max note length |
| `POST_MAX_CHARS` | `500` | max feed post text length |
| `CORS_ORIGINS` | `https://user.klab.gg,https://staging.klab.gg` | comma-separated; only matters for a browser hitting this API cross-origin, which the current frontend doesn't do (it calls relative `/api/...` paths, proxied same-origin through Caddy) |
