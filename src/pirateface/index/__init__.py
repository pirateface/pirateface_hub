"""Pirate Face's magnet index: a weekly torrent of shards (one bare magnet per line) plus the license.
What every part of pf that handles the index shares: its repo id and how to tell a build apart."""

import re

from pirateface.seed import clients

INDEX_REPO = "pirateface/magnet-index"
NAME = "magnet-index"  # the torrent name of every build
FILE = re.compile(r"LICENSE|shards/(removed-)?\d{4}-\d{2}-\d{2}\.txt")


def is_build(urls, paths):
    """Whether a torrent called NAME is a magnet index build: Pirate Face trackers, only the license and shards."""
    return clients.is_pirateface(urls) and bool(paths) and all(FILE.fullmatch(p) for p in paths)
