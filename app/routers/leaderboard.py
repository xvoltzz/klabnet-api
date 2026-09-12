"""Experimental leaderboard — minutes listened, posts, reactions given,
music requests submitted. Read-only aggregate over data other routers
already own (posts, post_reactions, music_requests) plus listening_stats,
which routers/presence.py accumulates from the existing presence heartbeat.
"""

from fastapi import APIRouter

from ..db import get_db

router = APIRouter()


@router.get("/api/leaderboard")
def get_leaderboard():
    with get_db() as db:
        listening = {
            r["username"]: r["seconds_listened"]
            for r in db.execute("SELECT username, seconds_listened FROM listening_stats").fetchall()
        }
        posts = {
            r["username"]: r["c"]
            for r in db.execute("SELECT username, COUNT(*) AS c FROM posts GROUP BY username").fetchall()
        }
        reactions = {
            r["username"]: r["c"]
            for r in db.execute("SELECT username, COUNT(*) AS c FROM post_reactions GROUP BY username").fetchall()
        }
        requests_ = {
            r["username"]: r["c"]
            for r in db.execute("SELECT username, COUNT(*) AS c FROM music_requests GROUP BY username").fetchall()
        }

    usernames = set(listening) | set(posts) | set(reactions) | set(requests_)
    leaderboard = [
        {
            "username": u,
            "seconds_listened": listening.get(u, 0),
            "posts_count": posts.get(u, 0),
            "reactions_given_count": reactions.get(u, 0),
            "requests_count": requests_.get(u, 0),
        }
        for u in usernames
    ]
    return {"leaderboard": leaderboard}
