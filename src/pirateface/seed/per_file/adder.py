"""Add per-file torrents to qBittorrent, each linked to a file already on disk."""

from pathlib import Path

from pirateface.client import api
from pirateface.config import SEEDS
from pirateface.seed import package


class Adder:
    """Adds the per-file torrents of one or more models to one qBittorrent, never the same one twice.

    verified: the files were checked against the model torrent (just hashed, or the model torrent is
    complete in the client). The per-file torrent has the same pieces root, so it's added without a check.
    Peers still check every 16 KiB block they get against that root."""

    def __init__(self, qbt):
        self.qbt = qbt
        self.known = {t.get("infohash_v2") for t in qbt.torrents()}  # in the client before this run
        self.added, self.had = set(), set()  # per-file v2 infohashes added now / already there
        self.asked = self.listed = 0  # files looked up / that Pirate Face answered for

    def add_files(self, found, v1, verified):
        """found: local_files() of the model torrent whose v1 infohash is v1."""
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
            elif h not in self.added and self.add_file(entry, path, verified):
                self.added.add(h)

    def add_file(self, entry, path, verified):
        name = entry["name"]  # from the first upload that had the file; can differ from yours
        torrent = api.download(entry["torrent"])
        if not torrent or Path(name).name != name:
            return False
        folder = Path(SEEDS, "per-file", entry["root"][:16])
        (folder / name).unlink(missing_ok=True)
        package.link(path.resolve(), folder / name)
        self.qbt.add(torrent, folder, skip_checking=verified)
        self.qbt.force_start(entry["infohashV2"])
        return True
