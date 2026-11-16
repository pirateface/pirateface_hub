"""Add per-file torrents to qBittorrent, each linked to a file already on disk."""

import os
import time
import urllib.error
from pathlib import Path

from pirateface.client import api
from pirateface.config import SEEDS
from pirateface.seed import package

TRACKER_WAIT = 0.5  # seconds between retries of a freshly added per-file torrent
TRACKER_TRIES = 3  # a 404 on its hash right after torrents/add is the add still registering


class Adder:
    """Adds the per-file torrents of one or more models to one qBittorrent, never the same one twice.

    verified: the files were checked against the model torrent (just hashed, or the model torrent is
    complete in the client). The per-file torrent has the same pieces root, so it's added without a check.
    Peers still check every 16 KiB block they get against that root.

    mine: this account's keyed seeding tracker URL (user.seeding_tracker), or None. Each per-file torrent
    announces to it too, so seeding the file's own swarm counts on the account. The site's trackers stay,
    so the swarm still shows on opentracker; qBittorrent announces to all of them (announce_to_all_trackers)."""

    def __init__(self, qbt, mine=None):
        self.qbt = qbt
        self.mine = mine
        self.known = {t.get("infohash_v2") for t in qbt.torrents()}  # in the client before this run
        self.added, self.had = set(), set()  # per-file v2 infohashes added now / already there
        self.asked = self.listed = 0  # files looked up / that Pirate Face answered for

    def add_files(self, found, v1, verified, label=None):
        """found: local_files() of the model torrent whose v1 infohash is v1. label: the model, for a skip's message."""
        for path, root in found:
            self.asked += 1
            entry = api.file_root(root)
            if not entry:
                continue
            self.listed += 1
            if all(s["infohash"] == v1 for s in entry["siblings"]):
                continue  # only in this upload
            h = entry["infohashV2"]
            if h in self.known:
                self.had.add(h)
            elif h not in self.added and self.add_file(entry, path, verified, label):
                self.added.add(h)

    def add_file(self, entry, path, verified, label=None):
        name = entry["name"]  # from the first upload that had the file; can differ from yours
        torrent = api.download(entry["torrent"])
        root = entry["root"][:16]  # from the site too: neither may lead out of the seeds folder
        if not torrent or Path(name).name != name or package.unsafe_path(f"{root}/{name}", windows=os.name == "nt"):
            return False
        folder = Path(SEEDS, "per-file", root)
        (folder / name).unlink(missing_ok=True)
        package.link(path.resolve(), folder / name)
        self.qbt.add(torrent, folder, skip_checking=verified)
        self.qbt.force_start(entry["infohashV2"])
        self._add_seeding_tracker(entry, label)
        return True

    def _add_seeding_tracker(self, entry, label=None):
        """Announce this per-file torrent to the account's seeding tracker too, so seeding it counts.
        No-op without a keyed tracker (unsigned-in or the site did not answer). qBittorrent must announce
        to every tracker in a tier, else it stops at the site tracker and never reaches ours. A 404 on the
        fresh hash is retried: qBittorrent registers an added torrent asynchronously, so it may not be
        queryable for a moment. If it is still missing the per-file torrent is skipped, naming the model."""
        if not self.mine:
            return
        h = entry["infohashV2"][:40]  # qBittorrent ids a v2 torrent by its hash cut to 40 digits
        for attempt in range(TRACKER_TRIES):
            try:
                if self.mine in self.qbt.trackers(h):
                    return
                self.qbt.ensure_announce_all()
                self.qbt.call("torrents/addTrackers", {"hash": h, "urls": self.mine})
                return
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
                if attempt == TRACKER_TRIES - 1:
                    print(f"    Skipped {label or entry['name']}: {exc}")
                else:
                    time.sleep(TRACKER_WAIT)
