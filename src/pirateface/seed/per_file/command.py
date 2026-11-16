"""Per-file swarms: for one model as pf seed shares it, or for every model already seeded (pf seed
--update-per-file). Either way the model torrents keep seeding and nothing is downloaded."""

from pathlib import Path

from pirateface.seed import clients
from pirateface.seed.per_file.adder import Adder
from pirateface.seed.per_file.files import MIN_SIZE, biggest_file, local_files, seeded_models
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


def update_all():
    """Per-file torrents for every model pf has seeded that can have them, then which ones can't and why
    (v1 only, no file of 1 MiB or more, not complete, not in qBittorrent)."""
    print("==> Adding per-file swarms for the Pirate Face torrents you seed")
    qbt = qbittorrent()
    if not qbt:
        raise SeedError(NEEDS_QBITTORRENT)
    adder = Adder(qbt, mine=seeding_tracker())
    ready, v1_only, no_big, incomplete, not_in_qbt, not_listed, unreadable = [], [], [], [], [], [], []
    seen = set()  # every model torrent qBittorrent holds, so the seed-folder scan below reports only the rest
    for t in qbt.torrents():
        # Model torrents are v1 or hybrid; pure v2 ones (no v1 hash) are per-file torrents themselves.
        if not t.get("infohash_v1") or not clients.is_pirateface(qbt.trackers(t["hash"])):
            continue
        label = repo_of(t)
        seen.update(x for x in (t.get("infohash_v1"), t.get("infohash_v2")) if x)
        try:
            data = qbt.export(t["hash"])
            if biggest_file(data) < MIN_SIZE:
                no_big.append(label)
                continue
            found = local_files(data, t["save_path"])
        except (OSError, RuntimeError, ValueError) as exc:  # OSError: qBittorrent 409 before metadata is fetched
            print(f"    Skipped {t.get('name') or t['hash'][:8]}: {exc}")
            unreadable.append(label)
            continue
        if found is None:
            v1_only.append(label)
        elif t.get("progress") != 1 or not found:
            incomplete.append(label)  # its files aren't all there yet, so its per-file torrents can't seed
        else:
            outcome = adder.add_files(found, t["infohash_v1"], verified=True)
            (ready if outcome == "ready" else not_listed).append(label)
    for label, v1, v2 in seeded_models():
        if v1 in seen or (v2 and v2 in seen):
            continue  # in qBittorrent: already counted above
        (v1_only if v2 is None else not_in_qbt).append(label)
    if adder.asked and not adder.listed:
        print(NOT_YET)
        not_listed_why = "couldn't be checked: Pirate Face didn't answer"
    else:
        not_listed_why = "no per-file swarm to join: its files aren't shared with another listed upload"
    print(f"    Per-file ready: {len(ready)} models.")
    print(f"    Added {len(adder.added)} per-file torrents ({len(adder.had)} already there).")
    blocks = [
        (v1_only, "v1 only (no v2 infohash): these can never have per-file swarms"),
        (not_in_qbt, "not in qBittorrent: per-file torrents are v2 only, so they must seed there"),
        (no_big, "no file of 1 MiB or more: nothing worth a per-file swarm"),
        (incomplete, "not complete in qBittorrent: their files aren't all there yet"),
        (not_listed, not_listed_why),
        (unreadable, "their metadata couldn't be read"),
    ]
    skipped = sum(len(labels) for labels, _ in blocks)
    print(f"    Can't go per-file: {skipped} models:")
    for labels, why in blocks:
        if not labels:
            continue
        print(f"      {len(labels)} {why}:")
        for label in labels[:20]:
            print(f"        {label}")
        if len(labels) > 20:
            print(f"        … and {len(labels) - 20} more")
