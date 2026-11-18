import argparse
import math
import os
import re
import sys
import time

import pirateface._boot  # noqa: F401  # uv tools skip .pth files

_WORK_NAME = re.compile(r"[A-Za-z0-9._-]{1,24}")


def _work_name(value):
    if not _WORK_NAME.fullmatch(value):
        raise argparse.ArgumentTypeError("use 1 to 24 letters, digits, dots, underscores or hyphens")
    return value


def _max_size(value):
    try:
        size = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("use a whole number of GiB (0 = no limit)") from None
    if size < 0:
        raise argparse.ArgumentTypeError("use a whole number of GiB (0 = no limit)")
    return size


def _download_rate(value):
    try:
        rate = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("use a positive number of MB/s") from None
    if not math.isfinite(rate) or rate <= 0:
        raise argparse.ArgumentTypeError("use a positive number of MB/s")
    return rate


def _index(module, do_sync):
    from pirateface import ui

    try:
        date = module.sync() if do_sync else module.build()
        models = module.count() if date else 0
    except Exception as e:  # no peers, no disk: say it once instead of a traceback
        print(f"could not read the index: {e}")
        return 1
    if date is None:
        print("no cached index yet: run `pf index --sync` once, while Pirate Face is reachable")
        return 1
    if module.broken():
        print("a removals list could not be read, so this copy answers nothing: run `pf index --sync`")
        return 1
    rows = [("build", date), ("models", f"{models:,}"), ("in", str(module.SHARDS))]
    if not models:
        rows.append(("next", "run `pf index --sync` if this should not be empty"))
    said = module.state()
    mirrored = module.infohash_of(said.get("magnet") or "")
    found = module.published()
    if found and mirrored and found[1] != mirrored:
        rows.append(("next", "a newer build is available: run `pf index --sync`"))
    if synced := said.get("synced"):
        rows.append(("mirrored", time.strftime("%Y-%m-%d %H:%M", time.localtime(synced))))
    # The report goes through the same writer as every other fact: one line each, labels aligned.
    ui.aligned(rows)
    return 0


def main():
    try:
        return _main()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        sys.exit(130)  # Ctrl-C is how pf work is meant to stop: a message, not a traceback


def _main():
    p = argparse.ArgumentParser(
        prog="pf",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Get a model, seed it, then add the layers the swarm can use.\n"
            "\n"
            "  pf download org/model        swarm, then Hugging Face for the rest\n"
            "  pf seed org/model            seed the listing, or submit it and seed\n"
            "  pf seed --update-per-file    per-file swarms for what you already seed\n"
            "  pf seed --upgrade org/model  v1 only becomes hybrid v1+v2\n"
            "  pf work --once               one queued model, from Hugging Face\n"
            "  pf restore PATH              put a .torrent back on a listing\n"
            "  pf index [--sync]            show the cached index, or mirror a newer build"
        ),
    )
    from pirateface.client.api import VERSION

    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True, title="commands")
    d = sub.add_parser("download", help="get a model into the Hugging Face cache")
    d.add_argument("repo_id")
    d.add_argument("--revision")
    d.add_argument("--local-dir", help="put the files in this folder instead of the Hugging Face cache")
    d.add_argument("--hf-first", action="store_true", help="Hugging Face first, the swarm only when it fails")
    s = sub.add_parser(
        "seed",
        help="seed it on Pirate Face, or submit it and seed it",
        description="Seeds the model if Pirate Face lists it. Otherwise builds the torrent, checks it against "
        "Hugging Face, adds it to your torrent client and submits it. Files are only read, never changed or uploaded.",
    )
    model = s.add_argument_group("model")
    model.add_argument("repo_id", nargs="?", help="Hugging Face id, author/model")
    model.add_argument(
        "folder", nargs="?", help="the model's files (default: the Hugging Face cache, downloading if needed)"
    )
    model.add_argument("--revision", help="commit the files came from (default: Hugging Face's, when the hashes match)")
    model.add_argument(
        "--hf-cache", action="store_true", help="every complete model in the Hugging Face cache, asking per model"
    )
    model.add_argument(
        "--from-cache",
        action="store_true",
        help="seed already-listed torrents from Hugging Face/Ollama cache files (qBittorrent required)",
    )
    per_file = s.add_argument_group("per-file layer", "a swarm per shared file, on top of the model torrent")
    per_file.add_argument(
        "--update-per-file",
        action="store_true",
        help="add per-file swarms for the models you already seed in qBittorrent (no download, no submit)",
    )
    per_file.add_argument(
        "--per-file",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="also seed each file another upload shares (1 MiB or more) in its own swarm (qBittorrent; default on)",
    )
    hybrid = s.add_argument_group("hybrid layer", "v1 only has no per-file swarms; hybrid is v1 and v2")
    hybrid.add_argument(
        "--upgrade",
        action="store_true",
        help="with a repo: relist your v1 torrent of it as hybrid (v1 + v2) so its files get per-file swarms; "
        "without: switch torrents Pirate Face replaced to their replacement (qBittorrent)",
    )
    hybrid.add_argument(
        "--mine",
        action="store_true",
        help="with --upgrade: every v1 torrent you submitted that has a complete copy in qBittorrent, "
        "not one at a time",
    )
    trackers = s.add_argument_group("trackers")
    trackers.add_argument(
        "--update-trackers", action="store_true", help="add your seeding tracker to torrents already seeding"
    )
    s.add_argument("-y", "--yes", action="store_true", help="skip every prompt; only on the user's explicit OK")
    u = sub.add_parser(
        "submit",
        help="package a folder and submit the listing",
        description="Builds the hybrid torrent, hashes the files, checks them against Hugging Face, adds the torrent "
        "to your torrent client, then submits it. Files are only read, never changed or uploaded.",
    )
    u.add_argument("folder", nargs="?", help="the model's files")
    u.add_argument("--repo", help="Hugging Face id, author/model")
    u.add_argument("--revision", help="commit the files came from (default: Hugging Face's, when the hashes match)")
    u.add_argument("--name", help="torrent name (default: the folder's name)")
    u.add_argument("--out", help="where the .torrent and file links go (default: ./pirateface-NAME)")
    u.add_argument("--direct", action="store_true", help="your own model, not on Hugging Face: no Hugging Face check")
    u.add_argument("--license", help="license id from pirateface.co/api/licenses (direct listings, or none on HF)")
    u.add_argument("--web-seed", help="direct listings: https base URL serving BASE/NAME/PATH (BEP 19)")
    u.add_argument("--dry-run", action="store_true", help="build and print everything; add nothing, submit nothing")
    u.add_argument("-y", "--yes", action="store_true", help="skip every prompt; only on the user's explicit OK")
    w = sub.add_parser(
        "work",
        help="build the next queued model, streamed from Hugging Face",
        description="Leases one queued model from Pirate Face, streams its files from Hugging Face through memory, "
        "builds the hybrid torrent and submits it. No model bytes are written to disk; a private checkpoint "
        "stores hashes and progress so an interrupted run can resume. Loops until stopped (Ctrl-C).",
    )
    w.add_argument("--once", action="store_true", help="take one job and exit")
    w.add_argument(
        "--clear-progress",
        action="store_true",
        help="discard this worker's saved piece and file hashes; the lease is kept",
    )
    w.add_argument(
        "--name",
        type=_work_name,
        help="optional label shown next to your account name on the rescue board (1 to 24 letters, digits, . _ -)",
    )
    w.add_argument("--idle", type=int, default=300, help="seconds between checks when nothing is queued (default 300)")
    w.add_argument(
        "--max-size",
        type=_max_size,
        metavar="GiB",
        help="biggest model to take, in GiB; 0 means any size (default: the server's 500 GiB cap)",
    )
    w.add_argument(
        "--max-download-rate",
        type=_download_rate,
        metavar="MB/s",
        help="per-process Hugging Face download rate in decimal MB/s (default: unlimited)",
    )
    w.add_argument("-y", "--yes", action="store_true", help="skip the prompt; only on the user's explicit OK")
    r = sub.add_parser(
        "restore",
        help="put a missing .torrent back on a listing",
        description="Uploads one or more .torrent files (or a torrent client folder) to restore missing metadata on Pirate Face.",
    )
    r.add_argument(
        "paths", nargs="+", help=".torrent file(s) or folder(s) (e.g. qBittorrent BT_backup or Transmission torrents/)"
    )
    r.add_argument("-y", "--yes", action="store_true", help="skip prompts")
    i = sub.add_parser(
        "index",
        help="the cached magnet index: show the build it holds, or mirror a newer one",
        description="The index is what lets a download start while Pirate Face is down. Mirroring fetches the "
        "index torrent's shards into the cache, and replaces any cached copy the build names differently "
        "-- by size, or by a different sha256 for the same size where a digest has been recorded.",
    )
    i.add_argument("--sync", action="store_true", help="mirror the index torrent's shards into the cache now")
    args = p.parse_args()

    if args.cmd == "index":
        from pirateface.index import mirror

        return _index(mirror, args.sync)

    if args.cmd == "work":
        from pirateface import work

        return work.run(args)

    if args.cmd == "restore":
        from pirateface import restore

        return restore.run(args)

    if args.cmd in ("seed", "submit"):
        from pirateface.seed import command

        return command.run(args)

    from huggingface_hub import snapshot_download

    from pirateface import ui

    if args.hf_first:
        os.environ["PF_HF_FIRST"] = "1"
    path = snapshot_download(args.repo_id, revision=args.revision, local_dir=args.local_dir)
    ui.download_summary()
    print(path)
