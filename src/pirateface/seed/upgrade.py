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


def listed_v1(repo):
    """(.torrent bytes, name, v1 infohash) of the repo's listed v1-only torrent, or None if it is already hybrid."""
    try:
        old = api.torrent_file(repo)
    except api.Unreachable as e:
        raise SeedError(f"could not reach {API}") from e
    if not old:
        raise SeedError(f"Pirate Face has no .torrent for {repo} you can use. See {API}/{repo}")
    name, v1, v2, single = package.info(old)
    if v2:
        print(f"==> {repo} is already hybrid. If you still seed its v1 torrent, switch with: pf seed --upgrade")
        return None
    if single:
        raise SeedError("this is a single-file torrent: its file already has one swarm")
    return old, name, v1


def complete_copy(qbt, repo, old, v1, name):
    """(qBittorrent entry, folder, files) of the torrent qBittorrent holds complete, every file there at its size."""
    entry = qbt.find(v1)
    if not entry or entry.get("progress") != 1:
        raise SeedError(f"qBittorrent must hold {repo}'s torrent complete. Run pf seed {repo}, let it finish, rerun")
    folder = Path(entry["save_path"], name)
    files = torrent.files_in(old)
    for path, size, _ in files:
        if not folder.joinpath(path).is_file() or folder.joinpath(path).stat().st_size != size:
            raise SeedError(f"{folder / path} is missing or has the wrong size")
    return entry, folder, files


def build_hybrid(name, folder, files, trackers):
    """(hybrid .torrent bytes, its v1 infohash, SHA-256 manifest) of files, same name and paths as the v1 torrent."""
    sums = {}  # each file's SHA-256, from the same read; must match the site's manifest
    sources = sorted(
        ((p.split("/"), lambda p=p: folder.joinpath(p).open("rb")) for p, _, _ in files),
        key=lambda s: [c.encode() for c in s[0]],
    )
    plen = hybrid.piece_length(sum(size for _, size, _ in files))
    data, new_v1, _ = hybrid.build_from(name, sources, trackers, plen, sums)
    return data, new_v1, [{"path": p, "sha256": h} for p, h in sorted(sums.items())]


def send_upgrade(key, v1, data, manifest):
    """Have Pirate Face list the hybrid in place of the v1 torrent."""
    body = {"from": v1, "torrent": base64.b64encode(data).decode(), "manifest": manifest}
    code, answer = api.upgrade(key, body)
    if code != 200:
        raise SeedError(f"Pirate Face did not take the upgrade (HTTP {code}): {answer.get('error') or answer}")


def rebuild(repo):
    key = user.load_key()
    qbt = _qbittorrent()
    listed = listed_v1(repo)
    if not listed:
        return
    old, name, v1 = listed
    entry, folder, files = complete_copy(qbt, repo, old, v1, name)
    print(f"==> Rebuilding {repo} as a hybrid torrent from {folder}")
    data, new_v1, manifest = build_hybrid(name, folder, files, api.trackers(key))
    send_upgrade(key, v1, data, manifest)
    print(f"    Listed in place of the v1 torrent: {API}/{repo}")
    if not qbt.has(new_v1):  # a rerun after the site took it
        qbt.add(data, entry["save_path"], skip_checking=True)  # every byte was just hashed
    _remove(qbt, entry)
    print(f"    Seeding the hybrid ({new_v1}) from the same files; the v1 torrent is out of qBittorrent.")
    print("    Seeders of the old torrent get told to switch: pf seed --upgrade")
    seed_model(data, entry["save_path"], new_v1, verified=True, label=repo)


def replacement(qbt, t):
    """The site's record of a v1-only Pirate Face torrent in qBittorrent if it was replaced, else None."""
    if t.get("infohash_v2") or not clients.is_pirateface(qbt.trackers(t["hash"])):
        return None
    status = api.torrent_status(t.get("infohash_v1") or t["hash"])
    return status if status and status.get("status") == "superseded" and status.get("supersededBy") else None


def switch(qbt, t, status, trackers):
    """Add the replacement on t's files (unless there already), then drop t. False if it can't be done safely."""
    new = status["supersededBy"]
    if not qbt.has(new):
        data = api.download(f"/api/torrents/{new}.torrent")
        if not data or package.info(data)[0] != t["name"]:  # another name would download everything again
            print(f"    Skipped {t['name']}: get its replacement at {API}/{status.get('repo')}")
            return False
        qbt.add(package.with_trackers(data, trackers), t["save_path"])
    _remove(qbt, t)
    return True


def switch_all():
    """Every v1-only Pirate Face torrent in qBittorrent that Pirate Face replaced: add the replacement on the same
    files, then drop the old one. qBittorrent checks the files against the new torrent; nothing is downloaded."""
    print("==> Switching replaced Pirate Face torrents in qBittorrent")
    qbt = _qbittorrent()
    trackers = api.trackers(user.load_key())
    switched = skipped = 0
    for t in qbt.torrents():
        status = replacement(qbt, t)
        if status and switch(qbt, t, status, trackers):
            switched += 1
        elif status:
            skipped += 1
    print(f"    {switched} switched, {skipped} skipped. Then: pf seed --update-per-file")
