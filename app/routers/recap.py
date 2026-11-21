"""The recap: what happened on klabnet since you last looked, for the site's
bell. Two parts:

  for_you   things about you: replies to your posts, @mentions of you (in
            posts and replies), reactions to your posts (one item per post),
            and photos you were tagged in.
  around    the rest, counted: new posts, photos, videos, clips, songs shared, playlists
            featured, radio stations made and streams started, by other people.

`since` is a Unix time from the client (when it last opened the recap),
held to the last 7 days. Chat isn't here: unread messages are Matrix's,
and the site has them already. Under /api/posts/ because Caddy only
forwards a fixed list of /api prefixes."""
import json
import re
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import get_groups, get_username, is_admin
from ..db import get_db
from . import calls, chat_admin, live, week

router = APIRouter()

MAX_BACK = 7 * 86400
SNIP = 140


def _sql_time(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _iso(s: str) -> str:
    return s.replace(" ", "T") + "Z" if s and "T" not in s else s


def _snip(s: str) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= SNIP else s[: SNIP - 1] + "…"


def _mentions(text: str, me: str) -> bool:
    # The site's rule (js/12-feed.js): @name not inside a word or an email,
    # and not followed by more of a name.
    return bool(re.search(r"(^|[^\w@])@" + re.escape(me) + r"(?![.-]*\w)", text or "", re.I))


@router.get("/api/posts/recap")
def recap(request: Request, since: float = 0):
    me = get_username(request)
    if me == "anonymous":
        return JSONResponse(status_code=401, content={"error": "not authenticated"})
    now = time.time()
    since = min(now, max(since or now - 86400, now - MAX_BACK))
    at = _sql_time(since)
    items: list[dict] = []
    with get_db() as db:
        # Replies to your posts.
        for r in db.execute(
            """SELECT r.id, r.post_id, r.username, r.text, r.created, p.text AS ptext, p.kind,
                      (SELECT title FROM videos WHERE post_id = p.id) AS vtitle
               FROM post_replies r JOIN posts p ON p.id = r.post_id
               WHERE p.username = ? AND r.username != ? AND r.created > ?
               ORDER BY r.id DESC LIMIT 30""", (me, me, at)):
            items.append({"kind": "reply", "at": _iso(r["created"]), "who": [r["username"]], "postId": r["post_id"],
                          "text": _snip(r["text"]), "on": _snip(r["vtitle"] or r["ptext"]), "video": r["kind"] == "video"})
        # @you in a post, or in a reply on someone else's post.
        like = f"%@{me}%"
        for r in db.execute(
            """SELECT id, username, text, created FROM posts
               WHERE username != ? AND created > ? AND text LIKE ? ORDER BY id DESC LIMIT 30""", (me, at, like)):
            if _mentions(r["text"], me):
                items.append({"kind": "mention", "at": _iso(r["created"]), "who": [r["username"]], "postId": r["id"], "text": _snip(r["text"])})
        for r in db.execute(
            """SELECT r.post_id, r.username, r.text, r.created FROM post_replies r JOIN posts p ON p.id = r.post_id
               WHERE p.username != ? AND r.username != ? AND r.created > ? AND r.text LIKE ?
               ORDER BY r.id DESC LIMIT 30""", (me, me, at, like)):
            if _mentions(r["text"], me):
                items.append({"kind": "mention", "at": _iso(r["created"]), "who": [r["username"]], "postId": r["post_id"],
                              "text": _snip(r["text"]), "reply": True})
        # Reactions to your posts: one item a post, everyone who reacted.
        rows = db.execute(
            """SELECT x.post_id, x.username, x.emoji, x.created, p.text AS ptext, p.kind,
                      (SELECT title FROM videos WHERE post_id = p.id) AS vtitle
               FROM post_reactions x JOIN posts p ON p.id = x.post_id
               WHERE p.username = ? AND x.username != ? AND x.created > ?
               ORDER BY x.created DESC LIMIT 300""", (me, me, at)).fetchall()
        by_post: dict[int, dict] = {}
        for r in rows:
            it = by_post.setdefault(r["post_id"], {"kind": "react", "at": _iso(r["created"]), "who": [], "emoji": [],
                                                   "postId": r["post_id"], "on": _snip(r["vtitle"] or r["ptext"]),
                                                   "photo": r["kind"] == "photo", "video": r["kind"] == "video"})
            if r["username"] not in it["who"]:
                it["who"].append(r["username"])
            if r["emoji"] not in it["emoji"]:
                it["emoji"].append(r["emoji"])
        items.extend(by_post.values())
        # Tagged in someone's photos.
        for r in db.execute(
            """SELECT p.id, p.username, p.text, p.created FROM post_tags t JOIN posts p ON p.id = t.post_id
               WHERE t.username = ? AND p.username != ? AND p.created > ? ORDER BY p.id DESC LIMIT 20""", (me, me, at)):
            items.append({"kind": "tag", "at": _iso(r["created"]), "who": [r["username"]], "postId": r["id"], "text": _snip(r["text"])})

        # Around klabnet.
        posts = db.execute(
            """SELECT id, username, text, song_json, kind, created FROM posts
               WHERE username != ? AND created > ?
                 AND id NOT IN (SELECT post_id FROM videos WHERE unlisted = 1)
                 -- Back-dated videos (an import of old YouTube uploads) aren't news.
                 AND id NOT IN (SELECT v.post_id FROM videos v JOIN posts q ON q.id = v.post_id
                                WHERE v.published < datetime(q.created, '-2 days'))
               ORDER BY id DESC LIMIT 500""", (me, at)).fetchall()
        photo_ids = [p["id"] for p in posts if p["kind"] == "photo"]
        photos = 0
        if photo_ids:
            marks = ",".join("?" * len(photo_ids))
            photos = db.execute(f"SELECT COUNT(*) FROM photos WHERE post_id IN ({marks})", photo_ids).fetchone()[0]
        songs = []
        for p in posts:
            if p["song_json"]:
                try:
                    s = json.loads(p["song_json"])
                except ValueError:
                    continue
                songs.append({"postId": p["id"], "who": p["username"], "title": s.get("title", ""), "artist": s.get("artist", ""),
                              "coverArt": s.get("cover_art", "")})
        def people(rows):
            seen: list[str] = []
            for p in rows:
                if p["username"] not in seen:
                    seen.append(p["username"])
            return seen
        words = [p for p in posts if p["kind"] == ""]
        clip_ids = {r[0] for r in db.execute("SELECT post_id FROM videos WHERE kind = 'clip' AND post_id IN (%s)"
                                             % ",".join("?" * len(posts)), [p["id"] for p in posts])} if posts else set()
        video_posts = [p for p in posts if p["kind"] == "video" and p["id"] not in clip_ids]
        clip_posts = [p for p in posts if p["id"] in clip_ids]
        latest = [{"postId": p["id"], "who": p["username"], "text": _snip(p["text"]), "at": _iso(p["created"]),
                   "song": bool(p["song_json"])} for p in words[:3]]
        playlists = [{"id": r["id"], "name": r["name"], "who": r["username"]} for r in db.execute(
            """SELECT id, name, username FROM playlists WHERE featured = 1 AND username != ? AND updated > ?
               ORDER BY updated DESC LIMIT 5""", (me, at))]
        stations = [{"id": r["id"], "name": r["name"], "who": r["author"] or r["owner"], "frequency": r["frequency"]} for r in db.execute(
            """SELECT id, name, owner, author, frequency FROM radio_stations WHERE owner != ? AND created > ?
               ORDER BY created DESC LIMIT 5""", (me, at))]
        # Reactions and replies on This week's lines about you (routers/week.py).
        items.extend(week.recap_items(db, me, at))
        # Missed calls (routers/calls.py).
        items.extend(calls.recap_items(db, me, at))
    items.sort(key=lambda i: i["at"], reverse=True)
    return {
        "since": since,
        "now": now,
        "forYou": items[:40],
        "around": {
            "posts": len(words), "postPeople": people(words), "latest": latest,
            "photos": photos, "photoPosts": len(photo_ids), "photoPeople": people([p for p in posts if p["kind"] == "photo"]),
            "videos": len(video_posts), "videoPeople": people(video_posts),
            "clips": len(clip_posts), "clipPeople": people(clip_posts),
            "songs": songs[:5], "songCount": len(songs),
            "playlists": playlists, "stations": stations,
            # Streams other people started (routers/live.py).
            "live": live.started_since(since, me),
            # For admins: channels and spaces people made on Matrix
            # (routers/chat_admin.py), so nothing new goes unseen.
            "rooms": chat_admin.created_since(since) if is_admin(get_groups(request)) else [],
        },
    }
