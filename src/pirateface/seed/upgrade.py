"""pf seed --upgrade: move v1-only Pirate Face torrents to hybrid (v1 + v2) ones, whose files get per-file swarms.

With a repo (its uploader): rebuild your v1 torrent of it as a hybrid from the copy qBittorrent seeds, same name and
paths, and have Pirate Face list it in place of the old one. Then seed it at once from the same files, drop the old
torrent (files stay) and add the per-file swarms. Without a repo (any seeder): switch every torrent in qBittorrent
that Pirate Face replaced this way to its replacement, so you keep seeding the swarm that is listed."""

import base64
from pathlib import Path

from pirateface.client import api, torrent
from pirateface.config import API
from pirateface.seed import clients, hybrid, package, user
from pirateface.seed.per_file.command import NEEDS_QBITTORRENT, qbittorrent, seed_model
from pirateface.seed.user import SeedError


def _qbittorrent():
    qbt = qbittorrent()
    if not qbt:
        raise SeedError(NEEDS_QBITTORRENT)
    return qbt


def _remove(qbt, entry):
    """Take a torrent out of qBittorrent and leave its files on disk."""
    qbt.call("torrents/delete", {"hashes": entry["hash"], "deleteFiles": "false"})


def rebuild(repo):
    key = user.load_key()
    qbt = _qbittorrent()
    try:
        old = api.torrent_file(repo)
    except api.Unreachable as e:
        raise SeedError(f"could not reach {API}") from e
    if not old:
        raise SeedError(f"Pirate Face has no .torrent for {repo} you can use. See {API}/{repo}")
    name, v1, v2, single = package.info(old)
    if v2:
        print(f"==> {repo} is already hybrid. If you still seed its v1 torrent, switch with: pf seed --upgrade")
        return
    if single:
        raise SeedError("this is a single-file torrent: its file already has one swarm")
    entry = qbt.find(v1)
    if not entry or entry.get("progress") != 1:
        raise SeedError(f"qBittorrent must hold {repo}'s torrent complete. Run pf seed {repo}, let it finish, rerun")
    folder = Path(entry["save_path"], name)
    files = torrent.files_in(old)
    for path, size, _ in files:
        if not folder.joinpath(path).is_file() or folder.joinpath(path).stat().st_size != size:
            raise SeedError(f"{folder / path} is missing or has the wrong size")

    print(f"==> Rebuilding {repo} as a hybrid torrent from {folder}")
    sums = {}  # each file's SHA-256, from the same read; must match the listing's manifest
    trackers = api.trackers(key)
    sources = sorted(
        ((p.split("/"), lambda p=p: folder.joinpath(p).open("rb")) for p, _, _ in files),
        key=lambda s: [c.encode() for c in s[0]],
    )
    plen = hybrid.piece_length(sum(size for _, size, _ in files))
    data, new_v1, _ = hybrid.build_from(name, sources, trackers, plen, sums)
    body = {
        "from": v1,
        "torrent": base64.b64encode(data).decode(),
        "manifest": [{"path": p, "sha256": h} for p, h in sorted(sums.items())],
    }
    code, answer = api.upgrade(key, body)
    if code != 200:
        raise SeedError(f"Pirate Face did not take the upgrade (HTTP {code}): {answer.get('error') or answer}")

    print(f"    Listed in place of the v1 torrent: {API}/{repo}")
    if not qbt.has(new_v1):  # a rerun after the site took it
        qbt.add(data, entry["save_path"], skip_checking=True)  # every byte was just hashed
    _remove(qbt, entry)
    print(f"    Seeding the hybrid ({new_v1}) from the same files; the v1 torrent is out of qBittorrent.")
    print("    Seeders of the old torrent get told to switch: pf seed --upgrade")
    seed_model(data, entry["save_path"], new_v1, verified=True)


def switch_all():
    """Every v1-only Pirate Face torrent in qBittorrent that Pirate Face replaced: add the replacement on the same
    files, then drop the old one. qBittorrent checks the files against the new torrent; nothing is downloaded."""
    print("==> Switching replaced Pirate Face torrents in qBittorrent")
    qbt = _qbittorrent()
    trackers = api.trackers(user.load_key())
    switched = skipped = 0
    for t in qbt.torrents():
        if t.get("infohash_v2") or not clients.is_pirateface(qbt.trackers(t["hash"])):
            continue
        status = api.torrent_status(t.get("infohash_v1") or t["hash"])
        new = status and status.get("status") == "superseded" and status.get("supersededBy")
        if not new:
            continue
        if not qbt.has(new):
            data = api.download(f"/api/torrents/{new}.torrent")
            if not data or package.info(data)[0] != t["name"]:  # another name would download everything again
                print(f"    Skipped {t['name']}: get its replacement at {API}/{status.get('repo')}")
                skipped += 1
                continue
            qbt.add(package.with_trackers(data, trackers), t["save_path"])
        _remove(qbt, t)
        switched += 1
    print(f"    {switched} switched, {skipped} skipped. Then: pf seed --update-per-file")
