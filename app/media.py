"""Photo storage on the media share.

If the share isn't mounted when the container starts, Docker quietly binds
the empty directory underneath instead, and anything written would land on
the app server's own disk. A sentinel file that only exists on the real
share tells the two apart, so nothing is ever written without it.
"""

import os

from .config import MEDIA_DIR

SENTINEL = ".klabnet-media"


def media_ready() -> bool:
    return os.path.isfile(os.path.join(MEDIA_DIR, SENTINEL))
