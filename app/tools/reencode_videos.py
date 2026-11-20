"""Make videos' HLS ladders again from their kept uploads (after the
encoder settings change): each is built in a new folder while the old one
keeps playing, then swapped in (routers/videos.py rebuild).

    python -m app.tools.reencode_videos --all
    python -m app.tools.reencode_videos --id 12 --id 40

It runs here, in the foreground, one video at a time; the API's own worker
may be making new uploads at the same time (they share the CPU)."""
import argparse
import sys

from ..db import get_db, init_db
from ..media import media_ready
from ..routers import videos


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.tools.reencode_videos", description=__doc__.split("\n\n")[0])
    which = ap.add_mutually_exclusive_group(required=True)
    which.add_argument("--all", action="store_true", help="every ready video")
    which.add_argument("--id", type=int, action="append", help="a video's id (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="list what would be rebuilt")
    a = ap.parse_args(argv)
    init_db()
    if not media_ready():
        print("the media share isn't there (MEDIA_DIR)")
        return 1
    with get_db() as db:
        if a.all:
            ids = [r[0] for r in db.execute("SELECT post_id FROM videos WHERE status='ready' ORDER BY post_id")]
        else:
            ids = a.id
    print(f"{len(ids)} video{'s' if len(ids) != 1 else ''} to rebuild")
    if a.dry_run:
        print(" ".join(map(str, ids)))
        return 0
    ok = sum(1 for i in ids if videos.rebuild(i))
    print(f"rebuilt {ok} of {len(ids)}")
    return 0 if ok == len(ids) else 2


if __name__ == "__main__":
    sys.exit(main())
