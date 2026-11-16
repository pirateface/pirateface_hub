"""Per-file swarms: for one model as pf seed shares it, or for every model already seeded (pf seed
--update-per-file). Either way the model torrents keep seeding and nothing is downloaded."""

from pathlib import Path

from pirateface import ui
from pirateface.seed import clients
from pirateface.seed.per_file.adder import Adder
from pirateface.seed.per_file.files import MIN_SIZE, biggest_file, local_files
from pirateface.seed.user import SeedError, seeding_tracker

NEEDS_QBITTORRENT = "per-file torrents are BitTorrent v2 only and need qBittorrent (Web UI on)"
NOT_YET = "    Per-file swarms are not available yet."


def qbittorrent():
    return next((c for c in clients.running() if isinstance(c, clients.QBittorrent)), None)


def _layers(model, per_file, nxt, added, skipped, report):
    if report:
        rows = [("per-file", per_file)]
        if model:
            rows.insert(0, ("model", model))
        ui.layer_block(rows, nxt)
    return {"added": added, "skipped": skipped}


def seed_model(data, save_dir, v1, verified=False, label=None, report=True):
    """Per-file torrents for the model pf seed just added. verified: its files were just hashed.
    label: the repo (author/model) to name in a skip. Returns added and skipped counts.
    report: print the layer block. A caller that sums several models prints one block itself."""
    print("\n==> Per-file swarms")
    try:
        found = local_files(data, save_dir)
    except (RuntimeError, ValueError) as exc:  # libtorrent rejects a malformed piece layer
        print(f"    Skipped: this torrent's metadata could not be read ({exc}).")
        return _layers(None, "skipped", None, 0, 0, report)
    if found is None:
        print("    Skipped: this torrent is v1 only, so its files have no per-file swarms.")
        nxt = f"pf seed --upgrade {label}" if label else "pf seed --upgrade"
        return _layers("v1 only", "skipped", nxt, 0, 0, report)
    qbt = qbittorrent()
    if not qbt:
        print(f"    Skipped: {NEEDS_QBITTORRENT}.")
        return _layers("hybrid v1+v2", "skipped", None, 0, 0, report)
    model = qbt.find(v1)
    verified = verified or bool(model and model.get("progress") == 1)
    adder = Adder(qbt, mine=seeding_tracker())
    adder.add_files(found, v1, verified, label)
    if not adder.listed:
        print(NOT_YET)
        return _layers("hybrid v1+v2", "skipped", None, 0, 0, report)
    if not adder.added | adder.had:
        print("    None of its files (1 MiB or more) are in another listed upload.")
    else:
        how = "Already verified: they seed now." if verified else "Each is checked against your copy, then seeds."
        print(f"    {how}")
    added, skipped = len(adder.added), len(adder.had)
    return _layers("hybrid v1+v2", f"{added} added, {skipped} skipped", None, added, skipped, report)


def repo_of(t):
    """The repo a qBittorrent model torrent belongs to (author/model from its seed folder), else its name.
    pf seeds every model into a <SEEDS>/<author>--<model>/files folder, so the parent folder names the repo."""
    folder = Path(t.get("save_path") or "").parent.name
    if folder and "--" in folder:
        return folder.replace("--", "/", 1)
    return t.get("name") or t["hash"][:8]


def classify(t, qbt):
    """(bucket, label, found) for one qBittorrent torrent, or None to skip it. bucket is "ready" (with
    the torrent's local files of 1 MiB or more in found), "v1_only" or "incomplete". Pure-v2 torrents are
    per-file torrents themselves, and a torrent without a Pirate Face tracker isn't a model pf seeded."""
    if not t.get("infohash_v1"):
        return None  # a per-file torrent itself, not a model
    label = repo_of(t)
    try:
        if not clients.is_pirateface(qbt.trackers(t["hash"])):
            return None  # not a model pf seeded
        data = qbt.export(t["hash"])
        if biggest_file(data) < MIN_SIZE:
            return None  # nothing of 1 MiB or more is worth a per-file swarm
        found = local_files(data, t["save_path"])
    except (OSError, RuntimeError, ValueError) as exc:  # OSError: a qBittorrent call 404 or 409 (HTTPError)
        print(f"    Skipped {t.get('name') or t['hash'][:8]}: {exc}")
        return None  # one model's failure never stops the run
    if found is None:
        return "v1_only", label, None
    if t.get("progress") != 1 or not found:
        return "incomplete", label, None  # its files aren't all there yet, so its per-file torrents can't seed
    return "ready", label, found


def scan(qbt, adder):
    """(v1_only, incomplete): the Pirate Face model torrents in qBittorrent that the seeder can act on.
    The ready ones get their per-file torrents added here; nothing about them is reported."""
    v1_only, incomplete = [], []
    for t in qbt.torrents():
        result = classify(t, qbt)
        if not result:
            continue
        bucket, label, found = result
        if bucket == "ready":
            adder.add_files(found, t["infohash_v1"], verified=True, label=label)
        elif bucket == "v1_only":
            v1_only.append(label)
        else:
            incomplete.append(label)
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
        print(
            "    These are v1 only. `pf seed --upgrade` switches any their uploader made hybrid; "
            "if you uploaded one, run `pf seed --upgrade org/model`:"
        )
        print_models(v1_only)
    if incomplete:
        print("    These aren't complete yet:")
        print_models(incomplete)


def update_all():
    """Per-file torrents for every model pf has seeded that can have them, then the two kinds the seeder
    can act on: v1-only models (pf seed --upgrade switches any their uploader made hybrid; if you uploaded
    one, pf seed --upgrade org/model) and ones that aren't complete yet."""
    print("==> Adding per-file swarms for the Pirate Face torrents you seed")
    qbt = qbittorrent()
    if not qbt:
        raise SeedError(NEEDS_QBITTORRENT)
    adder = Adder(qbt, mine=seeding_tracker())
    v1_only, incomplete = scan(qbt, adder)
    report(v1_only, incomplete, adder)
    added, had = len(adder.added), len(adder.had)
    rows = []
    if added or had:
        rows.append(("model", "hybrid v1+v2"))
    if len(v1_only) == 1 and not added and not had:
        rows.append(("model", "v1 only"))
    elif v1_only:
        rows.append(("v1 only", str(len(v1_only))))
    if incomplete:
        rows.append(("incomplete", str(len(incomplete))))
    per = "skipped" if v1_only and not added and not had and not incomplete else f"{added} added, {had} skipped"
    rows.append(("per-file", per))
    if len(v1_only) == 1:
        nxt = f"pf seed --upgrade {v1_only[0]}"
    elif v1_only:
        nxt = "pf seed --upgrade"
    else:
        nxt = None
    ui.layer_block(rows, nxt)
