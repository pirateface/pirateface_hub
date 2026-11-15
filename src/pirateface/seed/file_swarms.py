"""pf seed --per-file: also seed the model's shared files, each in its own swarm.

Pirate Face publishes a pure v2 torrent per file (by BEP 52 pieces root, /api/files/<root>). Every upload
with the same bytes shares it. Added over the local copy, the client only checks the file: nothing is
downloaded. The model torrent keeps seeding."""

from pathlib import Path

import libtorrent as lt

from pirateface.client import api
from pirateface.seed import clients, package

MIN_SIZE = 1 << 20  # configs and tokenizers aren't worth a swarm each


def local_files(data, save_dir):
    """(path, pieces root) of the torrent's files of MIN_SIZE or more that save_dir has whole. None if v1 only."""
    ti = lt.torrent_info(data)
    if ti.info_hashes().v2.is_all_zeros():
        return None
    fs = ti.files()
    found = []
    for i in range(fs.num_files()):
        path, size = Path(save_dir, fs.file_path(i)), fs.file_size(i)
        if size >= MIN_SIZE and path.is_file() and path.stat().st_size == size:
            found.append((path, str(fs.root(i))))
    return found


def seed(data, save_dir, out, v1):
    """Add the per-file torrent of each file that another listed upload also has, linked to the local copy."""
    print("\n==> Per-file swarms")
    found = local_files(data, save_dir)
    if found is None:
        print("    Skipped: this torrent is v1 only, so its files have no per-file swarms.")
        return
    qbt = next((c for c in clients.running() if isinstance(c, clients.QBittorrent)), None)
    if not qbt:
        print("    Skipped: per-file torrents are BitTorrent v2 only and need qBittorrent (Web UI on).")
        return
    added = had = 0
    for path, root in found:
        entry = api.file_root(root)
        if not entry or all(s["infohash"] == v1 for s in entry["siblings"]):
            continue  # not listed yet, or only in this upload
        if qbt.has(entry["infohashV2"]):
            had += 1
            continue
        name = entry["name"]  # from the first upload that had the file; can differ from yours
        torrent = api.download(entry["torrent"])
        if not torrent or Path(name).name != name:
            continue
        folder = out / "per-file" / root[:16]
        (folder / name).unlink(missing_ok=True)
        package.link(path.resolve(), folder / name)
        qbt.add(torrent, folder)
        added += 1
    if not added + had:
        print("    None of its files (1 MiB or more) are in another listed upload.")
        return
    print(f"    Added {added} to qBittorrent ({had} already there). Each is checked against your copy, then seeds.")
