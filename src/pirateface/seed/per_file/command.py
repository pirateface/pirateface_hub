"""Per-file swarms: for one model as pf seed shares it, or for every model already seeded (pf seed
--update-per-file). Either way the model torrents keep seeding and nothing is downloaded."""

from pathlib import Path

from pirateface.seed import clients
from pirateface.seed.per_file.adder import Adder
from pirateface.seed.per_file.files import MIN_SIZE, biggest_file, local_files
from pirateface.seed.user import SeedError, seeding_tracker

NEEDS_QBITTORRENT = "per-file torrents are BitTorrent v2 only and need qBittorrent (Web UI on)"
NOT_YET = "    Per-file swarms are not available yet."


def qbittorrent():
    return next((c for c in clients.running() if isinstance(c, clients.QBittorrent)), None)


def seed_model(data, save_dir, v1, verified=False):
    """Per-file torrents for the model pf seed just added. verified: its files were just hashed."""
    print("\n==> Per-file swarms")
    try:
        found = local_files(data, save_dir)
    except (RuntimeError, ValueError) as exc:  # libtorrent rejects a malformed piece layer
        print(f"    Skipped: this torrent's metadata could not be read ({exc}).")
        return
    if found is None:
        print("    Skipped: this torrent is v1 only, so its files have no per-file swarms.")
        return
    qbt = qbittorrent()
    if not qbt:
        print(f"    Skipped: {NEEDS_QBITTORRENT}.")
        return
    model = qbt.find(v1)
    verified = verified or bool(model and model.get("progress") == 1)
    adder = Adder(qbt, mine=seeding_tracker())
    adder.add_files(found, v1, verified)
    if not adder.listed:
        print(NOT_YET)
    elif not adder.added | adder.had:
        print("    None of its files (1 MiB or more) are in another listed upload.")
    else:
        how = "Already verified: they seed now." if verified else "Each is checked against your copy, then seeds."
        print(f"    Added {len(adder.added)} to qBittorrent ({len(adder.had)} already there). {how}")


def repo_of(t):
    """The repo a qBittorrent model torrent belongs to (author/model from its seed folder), else its name.
    pf seeds every model into a <SEEDS>/<author>--<model>/files folder, so the parent folder names the repo."""
    folder = Path(t.get("save_path") or "").parent.name
    if folder and "--" in folder:
        return folder.replace("--", "/", 1)
    return t.get("name") or t["hash"][:8]


def classify(t, qbt, adder):
    """(bucket, label) for one qBittorrent torrent, or None to skip it. Pure-v2 torrents are per-file
    torrents themselves, and a torrent without a Pirate Face tracker isn't a model pf seeded. Only the two
    kinds a seeder can act on are reported; everything else gets its per-file torrents added or is skipped."""
    if not t.get("infohash_v1") or not clients.is_pirateface(qbt.trackers(t["hash"])):
        return None
    label = repo_of(t)
    try:
        data = qbt.export(t["hash"])
        if biggest_file(data) < MIN_SIZE:
            return None  # nothing of 1 MiB or more is worth a per-file swarm
        found = local_files(data, t["save_path"])
    except (OSError, RuntimeError, ValueError) as exc:  # OSError: qBittorrent 409 before metadata is fetched
        print(f"    Skipped {t.get('name') or t['hash'][:8]}: {exc}")
        return None
    if found is None:
        return "v1_only", label
    if t.get("progress") != 1 or not found:
        return "incomplete", label  # its files aren't all there yet, so its per-file torrents can't seed
    adder.add_files(found, t["infohash_v1"], verified=True)
    return None


def scan(qbt, adder):
    """(v1_only, incomplete): the Pirate Face model torrents in qBittorrent that the seeder can act on.
    The rest are either skipped or got their per-file torrents added, with nothing reported."""
    v1_only, incomplete = [], []
    for t in qbt.torrents():
        result = classify(t, qbt, adder)
        if result:
            bucket, label = result
            (v1_only if bucket == "v1_only" else incomplete).append(label)
    return v1_only, incomplete


def print_models(labels):
    """At most 20 models, then a count of the rest; the heading above names the group."""
    for label in labels[:20]:
        print(f"      {label}")
    if len(labels) > 20:
        print(f"      … and {len(labels) - 20} more")


def report(v1_only, incomplete, adder):
    """The two lists a seeder can act on, each with what to do. Nothing else is printed."""
    if adder.asked and not adder.listed:
        print(NOT_YET)
    if v1_only:
        print("    These are v1 only. Run `pf seed --upgrade` to make them per-file:")
        print_models(v1_only)
    if incomplete:
        print("    These aren't complete yet:")
        print_models(incomplete)


def update_all():
    """Per-file torrents for every model pf has seeded that can have them, then the two kinds the seeder
    can act on: v1-only models (run pf seed --upgrade) and ones that aren't complete yet."""
    print("==> Adding per-file swarms for the Pirate Face torrents you seed")
    qbt = qbittorrent()
    if not qbt:
        raise SeedError(NEEDS_QBITTORRENT)
    adder = Adder(qbt, mine=seeding_tracker())
    v1_only, incomplete = scan(qbt, adder)
    report(v1_only, incomplete, adder)
