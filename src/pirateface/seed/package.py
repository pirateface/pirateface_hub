"""Model folder -> links, checksums and a hybrid (v1 + v2) torrent."""

import concurrent.futures
import hashlib
import os
import re
import shutil
import urllib.parse
from pathlib import Path

import libtorrent as lt

# Hidden paths (.git, .cache, .env), keys and partial downloads stay out of the torrent.
SKIP = re.compile(r"(^|/)(\.|__pycache__/|id_rsa)|\.(pem|key|incomplete|lock|part)$")


class LinkError(Exception):
    pass


def link(target, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.symlink_to(target)
    except OSError as e:
        raise LinkError(
            f"could not link {dest.name} ({e}). On Windows, turn on Developer Mode "
            "(Settings > System > For developers) or run as administrator"
        ) from e


def stage(src, dest):
    """Link the model files in src into dest. Returns (included paths, skipped paths)."""
    shutil.rmtree(dest, ignore_errors=True)
    included, skipped = [], []
    for d, _, names in os.walk(src, followlinks=True):
        for n in names:
            rel = Path(d, n).relative_to(src).as_posix()
            (skipped if SKIP.search(rel) else included).append(rel)
    for rel in sorted(included):
        link(Path(src, rel).absolute(), dest / rel)
    return sorted(included), sorted(skipped)


def link_existing(src, dest, paths):
    """Link the files of a listed torrent that src already has. Returns how many."""
    shutil.rmtree(dest, ignore_errors=True)
    found = [p for p in paths if Path(src, p).is_file()]
    for p in found:
        link(Path(src, p).absolute(), dest / p)
    return len(found)


def checksums(root, paths):
    """{path: sha256}"""

    def one(p):
        with Path(root, p).open("rb") as f:
            return p, hashlib.file_digest(f, "sha256").hexdigest()

    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        return dict(pool.map(one, paths))


def make_torrent(save_dir, name, trackers):
    """Hybrid (BEP 52) torrent of save_dir/name with BEP 47 pad files. Web seed downloads of plain v1
    multi-file torrents stall at ~99%. About 2000 pieces of 256 KiB-64 MiB (stays under the 3 MB upload limit
    up to ~10 TB). Returns (bytes, v1 infohash, v2 infohash)."""
    fs = lt.file_storage()
    lt.add_files(fs, str(save_dir / name))
    exp = 18
    while exp < 26 and fs.total_size() / (1 << exp) > 2000:
        exp += 1
    ct = lt.create_torrent(fs, 1 << exp)
    for tier, url in enumerate(trackers):
        ct.add_tracker(url, tier)
    lt.set_piece_hashes(ct, str(save_dir))
    data = lt.bencode(ct.generate())
    hashes = lt.torrent_info(data).info_hashes()
    return data, str(hashes.v1), str(hashes.v2)


def with_trackers(data, trackers):
    """The same torrent (same infohash) announcing to these trackers."""
    t = lt.bdecode(data)
    t[b"announce"] = trackers[0].encode()
    t[b"announce-list"] = [[url.encode()] for url in trackers]
    return lt.bencode(t)


def info(data):
    """(name, v1 infohash, v2 infohash or None, single file?)"""
    ti = lt.torrent_info(data)
    v2 = ti.info_hashes().v2
    return ti.name(), str(ti.info_hashes().v1), None if v2.is_all_zeros() else str(v2), ti.num_files() == 1


def magnet(v1, v2, name=None, trackers=()):
    q = urllib.parse.quote
    m = f"magnet:?xt=urn:btih:{v1}" + (f"&xt=urn:btmh:1220{v2}" if v2 else "")
    if name:
        m += "&dn=" + q(name, safe="")
    return m + "".join("&tr=" + q(t, safe="") for t in trackers)
