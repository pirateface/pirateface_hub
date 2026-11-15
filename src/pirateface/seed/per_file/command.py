"""Per-file swarms: for one model as pf seed shares it, or for every model already in qBittorrent
(pf seed --update-per-file). Either way the model torrents keep seeding and nothing is downloaded."""

from pirateface.seed import clients
from pirateface.seed.per_file.adder import Adder
from pirateface.seed.per_file.files import local_files
from pirateface.seed.user import SeedError

NEEDS_QBITTORRENT = "per-file torrents are BitTorrent v2 only and need qBittorrent (Web UI on)"
NOT_YET = "    Per-file swarms are not available yet."


def qbittorrent():
    return next((c for c in clients.running() if isinstance(c, clients.QBittorrent)), None)


def seed_model(data, save_dir, v1, verified=False):
    """Per-file torrents for the model pf seed just added. verified: its files were just hashed."""
    print("\n==> Per-file swarms")
    found = local_files(data, save_dir)
    if found is None:
        print("    Skipped: this torrent is v1 only, so its files have no per-file swarms.")
        return
    qbt = qbittorrent()
    if not qbt:
        print(f"    Skipped: {NEEDS_QBITTORRENT}.")
        return
    model = qbt.find(v1)
    verified = verified or bool(model and model.get("progress") == 1)
    adder = Adder(qbt)
    adder.add_files(found, v1, verified)
    if not adder.listed:
        print(NOT_YET)
    elif not adder.added | adder.had:
        print("    None of its files (1 MiB or more) are in another listed upload.")
    else:
        how = "Already verified: they seed now." if verified else "Each is checked against your copy, then seeds."
        print(f"    Added {len(adder.added)} to qBittorrent ({len(adder.had)} already there). {how}")


def update_all():
    """Per-file torrents for every Pirate Face model torrent already in qBittorrent, from the files it has."""
    print("==> Adding per-file swarms for the Pirate Face torrents in qBittorrent")
    qbt = qbittorrent()
    if not qbt:
        raise SeedError(NEEDS_QBITTORRENT)
    adder = Adder(qbt)
    scanned = v1_only = 0
    for t in qbt.torrents():
        # Model torrents are v1 or hybrid; pure v2 ones (no v1 hash) are per-file torrents themselves.
        if not t.get("infohash_v1") or not clients.is_pirateface(qbt.trackers(t["hash"])):
            continue
        scanned += 1
        found = local_files(qbt.export(t["hash"]), t["save_path"])
        if found is None:
            v1_only += 1
            continue
        adder.add_files(found, t["infohash_v1"], verified=t.get("progress") == 1)
    if adder.asked and not adder.listed:
        print(NOT_YET)
        return
    print(
        f"    {scanned} models scanned, {len(adder.added)} per-file torrents added "
        f"({len(adder.had)} already there), {v1_only} v1-only skipped."
    )
