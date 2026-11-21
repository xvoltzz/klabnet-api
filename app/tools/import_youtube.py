"""Bring YouTube videos into klabnet's Videos, keeping their dates,
titles, descriptions and thumbnails, and their playlists.

    python -m app.tools.import_youtube <folder> --user <username>
        [--dry-run] [--limit N] [--include-private] [--feature-public]

<folder> can hold either (or both):
  * yt-dlp downloads: <name>.<mp4|mkv|webm…> beside <name>.info.json
    (yt-dlp --write-info-json --write-thumbnail) and <name>.jpg/.webp/.png.
    A playlist's own .info.json (_type "playlist", with entries) becomes
    a playlist; so do videos whose info names a playlist_id.
  * A Google Takeout: "YouTube and YouTube Music/videos/*.mp4",
    ".../video metadata/videos.csv" and ".../playlists/*.csv".

Each video is made through Videos' own pipeline (routers/videos.py create)
and left queued: the API's worker makes them one at a time (it looks for
new ones every 20s), so a big import takes a while; each shows "Processing
N%" in Videos meanwhile. Nothing is posted to the Feed, and videos that came
out more than two days before they arrived are left out of the recap's and
This week's "new videos". Idempotent: each video remembers its YouTube id
(videos.source_id) and each playlist its playlist id, so a re-run skips what's
done. Private videos are skipped unless --include-private; unlisted ones
come in unlisted (a link plays them; they're out of the lists). Playlists
come in private; public ones are featured only with --feature-public."""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

from ..config import VIDEO_DESC_MAX_CHARS
from ..db import get_db, init_db
from ..media import media_ready
from ..routers import videos
from ..routers.video_playlists import create_playlist_row

VIDEO_EXTS = tuple("." + e for e in sorted(videos._EXTS))
THUMB_EXTS = (".jpg", ".jpeg", ".webp", ".png")


def _norm(s: str) -> str:
    """For matching a title to a Takeout file name (which drops / : ? etc.)."""
    return re.sub(r"[^0-9a-z]+", "", (s or "").lower())


def _col(row: dict, *names: str) -> str:
    """A CSV column by any of its spellings (Takeout's headers change)."""
    low = {(k or "").strip().lower(): v for k, v in row.items()}
    for n in names:
        v = low.get(n.lower())
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _when(raw: str) -> str | None:
    """A timestamp in any of the shapes YouTube exports use, for parse_published."""
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return videos.parse_published(s)
    except ValueError:
        pass
    for fmt in ("%b %d, %Y, %I:%M:%S %p %Z", "%b %d, %Y, %I:%M:%S %p", "%Y-%m-%d %H:%M:%S %Z", "%d %b %Y, %H:%M:%S %Z", "%b %d, %Y"):
        try:
            return datetime.strptime(s.replace(" ", " "), fmt).replace(tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def _read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# ── Finding things ──

def find_ytdlp(root: str) -> tuple[list[dict], list[dict]]:
    """yt-dlp downloads: (videos, playlists)."""
    found, lists = [], []
    for dirpath, _, files in os.walk(root):
        names = set(files)
        for name in sorted(files):
            if not name.endswith(".info.json"):
                continue
            stem = name[: -len(".info.json")]
            try:
                info = json.load(open(os.path.join(dirpath, name), encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if info.get("_type") == "playlist":
                ids = [e.get("id") for e in info.get("entries") or [] if isinstance(e, dict) and e.get("id")]
                lists.append({"id": info.get("id") or stem, "title": info.get("title") or stem,
                              "description": info.get("description") or "",
                              "public": (info.get("availability") or "public") == "public", "videos": ids})
                continue
            media = next((stem + e for e in VIDEO_EXTS if stem + e in names), None)
            if media is None:
                # yt-dlp sometimes names the merged file differently in case.
                media = next((n for n in files if n.lower().startswith(stem.lower() + ".") and n.lower().endswith(VIDEO_EXTS)), None)
            if media is None:
                continue
            thumb = next((stem + e for e in THUMB_EXTS if stem + e in names), None)
            when = None
            if info.get("timestamp"):
                when = _when(str(int(info["timestamp"])))
            if not when and info.get("upload_date"):
                when = _when(str(info["upload_date"]))
            avail = (info.get("availability") or "public").lower()
            found.append({
                "id": info.get("id") or stem, "path": os.path.join(dirpath, media),
                "thumb": os.path.join(dirpath, thumb) if thumb else None,
                "title": info.get("title") or stem, "description": info.get("description") or "",
                "published": when, "privacy": "private" if avail == "private" else ("unlisted" if avail == "unlisted" else "public"),
                "playlist": (info.get("playlist_id"), info.get("playlist_title"), info.get("playlist_index")) if info.get("playlist_id") else None,
            })
    # Videos that name their playlist, when there's no playlist file for it.
    have = {p["id"] for p in lists}
    groups: dict[str, dict] = {}
    for v in found:
        if v["playlist"] and v["playlist"][0] not in have:
            pid, title, idx = v["playlist"]
            g = groups.setdefault(pid, {"id": pid, "title": title or pid, "description": "", "public": False, "items": []})
            g["items"].append((idx if isinstance(idx, int) else 10**6, v["id"]))
    for g in groups.values():
        lists.append({**{k: g[k] for k in ("id", "title", "description", "public")}, "videos": [i for _, i in sorted(g["items"])]})
    return found, lists


def find_takeout(root: str) -> tuple[list[dict], list[dict], list[str]]:
    """A Google Takeout: (videos, playlists, rows it couldn't match)."""
    found, lists, unmatched = [], [], []
    for dirpath, dirs, files in os.walk(root):
        low = {f.lower(): f for f in files}
        if "videos.csv" not in low or os.path.basename(dirpath).lower() not in ("video metadata", "video-metadata"):
            continue
        base = os.path.dirname(dirpath)
        vdir = next((os.path.join(base, d) for d in os.listdir(base) if d.lower() == "videos"), None)
        media = []
        if vdir:
            media = [os.path.join(vdir, n) for n in sorted(os.listdir(vdir)) if n.lower().endswith(VIDEO_EXTS)]
        by_norm: dict[str, list[str]] = {}
        for m in media:
            stem = re.sub(r"\(\d+\)$", "", os.path.splitext(os.path.basename(m))[0]).strip()
            by_norm.setdefault(_norm(stem), []).append(m)
        used = set()
        for row in _read_csv(os.path.join(dirpath, low["videos.csv"])):
            vid = _col(row, "Video ID", "Video Id", "video_id")
            title = _col(row, "Video title (original)", "Video Title (Original)", "Video title", "Title")
            desc = _col(row, "Video description (original)", "Video Description (Original)", "Video description", "Description")
            when = _when(_col(row, "Video publish timestamp", "Video Publish Timestamp", "Video publish time",
                              "Video create timestamp", "Video Create Timestamp", "Time Published", "Created"))
            privacy = (_col(row, "Privacy", "Video privacy", "Visibility") or "public").lower()
            # The file: its YouTube id in the name, else its title.
            m = next((x for x in media if vid and vid in os.path.basename(x) and x not in used), None)
            if m is None:
                m = next((x for x in by_norm.get(_norm(title), []) if x not in used), None)
            if m is None:
                unmatched.append(f"{title or vid} ({vid or 'no id'})")
                continue
            used.add(m)
            found.append({"id": vid or _norm(title), "path": m, "thumb": None, "title": title or os.path.splitext(os.path.basename(m))[0],
                          "description": desc, "published": when,
                          "privacy": "private" if "private" in privacy else ("unlisted" if "unlisted" in privacy else "public"),
                          "playlist": None})
        pdir = next((os.path.join(base, d) for d in os.listdir(base) if d.lower() == "playlists"), None)
        if pdir:
            lists += _takeout_playlists(pdir)
    return found, lists, unmatched


def _takeout_playlists(pdir: str) -> list[dict]:
    """Takeout's playlists: either one CSV a playlist with its details on
    top (older exports), or playlists.csv with the details and
    "<title>-videos.csv" with each one's videos (newer ones)."""
    out, meta = [], {}
    files = sorted(os.listdir(pdir))
    if "playlists.csv" in [f.lower() for f in files]:
        name = next(f for f in files if f.lower() == "playlists.csv")
        for row in _read_csv(os.path.join(pdir, name)):
            title = _col(row, "Playlist title (original)", "Playlist Title (Original)", "Playlist title", "Title")
            meta[_norm(title)] = {
                "id": _col(row, "Playlist ID", "Playlist Id") or _norm(title), "title": title,
                "description": _col(row, "Playlist description (original)", "Playlist Description (Original)", "Description"),
                "public": "public" in (_col(row, "Playlist visibility", "Visibility", "Privacy") or "").lower(),
            }
    for name in files:
        if not name.lower().endswith(".csv") or name.lower() == "playlists.csv":
            continue
        path = os.path.join(pdir, name)
        lines = open(path, encoding="utf-8-sig").read().splitlines()
        info = {"id": "", "title": re.sub(r"(-videos)?\.csv$", "", name, flags=re.I), "description": "", "public": False}
        # Older: "Playlist Id,Channel Id,Time Created,Visibility,Title,Description" then a blank line, then the videos.
        if lines and lines[0].lower().startswith("playlist id") and "" in lines:
            head = list(csv.DictReader(lines[: lines.index("")]))
            if head:
                info.update(id=_col(head[0], "Playlist Id", "Playlist ID"), title=_col(head[0], "Title") or info["title"],
                            description=_col(head[0], "Description"),
                            public="public" in _col(head[0], "Visibility", "Privacy").lower())
            lines = lines[lines.index("") + 1:]
        else:
            m = meta.get(_norm(info["title"]))
            if m:
                info.update(m)
        ids = [_col(r, "Video ID", "Video Id") for r in csv.DictReader([l for l in lines if l.strip()])]
        info["id"] = info["id"] or "takeout:" + _norm(info["title"])
        out.append({**info, "videos": [i for i in ids if i]})
    return out


# ── Bringing them in ──

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.tools.import_youtube",
                                 description="Import YouTube videos (yt-dlp downloads or a Google Takeout) into klabnet's Videos.")
    ap.add_argument("folder")
    ap.add_argument("--user", required=True, help="whose channel they go on (a klabnet username)")
    ap.add_argument("--dry-run", action="store_true", help="say what would happen, change nothing")
    ap.add_argument("--limit", type=int, default=0, help="import at most N videos this run")
    ap.add_argument("--include-private", action="store_true", help="private videos too (they come in unlisted)")
    ap.add_argument("--feature-public", action="store_true", help="feature playlists that were public on YouTube")
    a = ap.parse_args(argv)
    user = a.user.strip().lower()
    root = os.path.abspath(a.folder)
    if not os.path.isdir(root):
        print(f"no folder at {root}")
        return 1
    init_db()
    if not a.dry_run and not media_ready():
        print("the media share isn't there (MEDIA_DIR)")
        return 1

    yv, yl = find_ytdlp(root)
    tv, tl, unmatched = find_takeout(root)
    found = yv + [v for v in tv if v["id"] not in {x["id"] for x in yv}]
    print(f"found {len(found)} video{'s' if len(found) != 1 else ''} ({len(yv)} from yt-dlp, {len(tv)} from Takeout)"
          f" and {len(yl) + len(tl)} playlist{'s' if len(yl) + len(tl) != 1 else ''}")
    found.sort(key=lambda v: v["published"] or "")  # oldest first: post ids follow the dates

    stats = {"imported": 0, "already": 0, "private": 0, "failed": 0}
    flagged = []
    with get_db() as db:
        done = {r[0]: r[1] for r in db.execute("SELECT source_id, post_id FROM videos WHERE source_id != ''")}
    for v in found:
        if v["id"] in done:
            stats["already"] += 1
            continue
        if v["privacy"] == "private" and not a.include_private:
            stats["private"] += 1
            flagged.append(f"private, skipped: {v['title']}")
            continue
        if a.limit and stats["imported"] >= a.limit:
            break
        unlisted = v["privacy"] in ("private", "unlisted")
        desc = v["description"]
        if len(desc) > VIDEO_DESC_MAX_CHARS:
            desc = desc[: VIDEO_DESC_MAX_CHARS - 1].rstrip() + "…"
            flagged.append(f"description shortened: {v['title']}")
        if not v["published"]:
            flagged.append(f"no date found, dated now: {v['title']}")
        line = f"{(v['published'] or 'now')[:10]}  {v['title'][:70]}{'  (unlisted)' if unlisted else ''}"
        if a.dry_run:
            print("would import " + line)
            done[v["id"]] = -1
            stats["imported"] += 1
            continue
        try:
            with open(v["path"], "rb") as f:
                res = videos.create(None, user, SimpleNamespace(filename=os.path.basename(v["path"]), file=f), v["title"], desc,
                                    unlisted=unlisted, published=v["published"], source_id=v["id"], thumb=v["thumb"], queue_it=False)
        except Exception as e:  # noqa: BLE001 — one unreadable file (or a busy database) isn't the whole import
            stats["failed"] += 1
            print(f"couldn't import {v['title']}: {e}")
            continue
        if isinstance(res, int):
            done[v["id"]] = res
            stats["imported"] += 1
            print("imported " + line)
        else:
            stats["failed"] += 1
            print(f"couldn't import {v['title']}: {json.loads(res.body).get('error')}")

    pl = {"made": 0, "already": 0, "entries": 0, "missing": 0, "empty": 0}
    with get_db() as db:
        known = {r[0] for r in db.execute("SELECT source_id FROM video_playlists WHERE source_id != '' AND username=?", (user,))}
        for p in yl + tl:
            if p["id"] in known:
                pl["already"] += 1
                continue
            ids = [done[i] for i in p["videos"] if i in done]
            pl["entries"] += len(ids)
            pl["missing"] += len(p["videos"]) - len(ids)
            if not ids:
                pl["empty"] += 1
                continue
            featured = bool(p["public"] and a.feature_public)
            if a.dry_run:
                print(f"would make playlist {p['title']} ({len(ids)} of {len(p['videos'])}){' featured' if featured else ''}")
            else:
                create_playlist_row(db, user, (p["title"] or "Playlist")[:80], (p["description"] or "")[:600], featured, ids, p["id"])
                print(f"made playlist {p['title']} ({len(ids)} of {len(p['videos'])}){' featured' if featured else ''}")
            known.add(p["id"])
            pl["made"] += 1
        if not a.dry_run:
            db.commit()

    for f in flagged:
        print("  · " + f)
    for u in unmatched:
        print(f"  · no file for Takeout row: {u}")
    verb = "would import" if a.dry_run else "imported"
    print(f"\n{verb} {stats['imported']}, already here {stats['already']}, private skipped {stats['private']}, "
          f"failed {stats['failed']}, unmatched rows {len(unmatched)}")
    print(f"playlists: {'would make' if a.dry_run else 'made'} {pl['made']}, already here {pl['already']}, "
          f"with no imported videos {pl['empty']}; entries {pl['entries']} matched, {pl['missing']} not imported")
    if stats["imported"] and not a.dry_run:
        print("The API's worker makes them one at a time (it checks every 20s); each shows its progress in Videos.")
    return 0 if not stats["failed"] else 2


if __name__ == "__main__":
    sys.exit(main())
