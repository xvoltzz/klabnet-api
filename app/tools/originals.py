"""Give videos made before the Original quality existed theirs: the kept
upload remuxed (no re-encode) into original.mp4, when its codecs are ones
browsers play (routers/videos.py make_original). Quick: it copies streams.

    python -m app.tools.originals [--dry-run]"""
import argparse
import json
import os
import sys

from ..db import get_db, init_db
from ..media import media_ready
from ..routers import videos


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.tools.originals", description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    init_db()
    if not media_ready():
        print("the media share isn't there (MEDIA_DIR)")
        return 1
    with get_db() as db:
        rows = db.execute("SELECT post_id, file_id, ext FROM videos WHERE status='ready' AND original='' ORDER BY post_id").fetchall()
    made = none = 0
    for r in rows:
        folder = videos._folder(r["file_id"])
        src = os.path.join(folder, f"source.{r['ext']}")
        info = videos.probe(src) if os.path.isfile(src) else None
        if info is None:
            print(f"video {r['post_id']}: no upload to work from")
            none += 1
            continue
        if a.dry_run:
            playable = info["vcodec"] in videos._DIRECT_VIDEO and (not info["audio"] or info["acodec"] in videos._DIRECT_AUDIO)
            print(f"video {r['post_id']}: {info['vcodec']}/{info['acodec'] or '-'} {'would get one' if playable else 'not playable direct'}")
            continue
        orig = videos.make_original(src, folder, info)
        if orig:
            videos._set(r["post_id"], original=json.dumps(orig))
            made += 1
            print(f"video {r['post_id']}: Original {orig['height']}p {orig['codec']} {orig['bitrate'] / 1e6:.1f} Mbps")
        else:
            none += 1
            print(f"video {r['post_id']}: {info['vcodec']}/{info['acodec'] or '-'} can't play direct; no Original")
    print(f"{made} made, {none} without one, of {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
