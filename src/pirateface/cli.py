import argparse

import pirateface._boot  # noqa: F401  # uv tools skip .pth files


def main():
    p = argparse.ArgumentParser(prog="pf")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download", help="download a model into the Hugging Face cache")
    d.add_argument("repo_id")
    d.add_argument("--revision")
    d.add_argument("--local-dir", help="put the files in this folder instead of the Hugging Face cache")
    s = sub.add_parser(
        "seed",
        help="share a model on Pirate Face: seed it if listed, else package, submit and seed it",
        description="Seeds the model if Pirate Face lists it. Otherwise builds the torrent, checks it against "
        "Hugging Face, adds it to your torrent client and submits it. Files are only read, never changed or uploaded.",
    )
    s.add_argument("repo_id", nargs="?", help="Hugging Face id, author/model")
    s.add_argument(
        "folder", nargs="?", help="the model's files (default: the Hugging Face cache, downloading if needed)"
    )
    s.add_argument("--revision", help="commit the files came from (default: Hugging Face's, when the hashes match)")
    s.add_argument(
        "--hf-cache", action="store_true", help="every complete model in the Hugging Face cache, asking per model"
    )
    s.add_argument(
        "--update-trackers", action="store_true", help="add your seeding tracker to torrents already seeding"
    )
    s.add_argument("-y", "--yes", action="store_true", help="skip every prompt; only on the user's explicit OK")
    u = sub.add_parser(
        "submit",
        help="package a model folder and submit it to Pirate Face (the flags of package.sh)",
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
    r = sub.add_parser(
        "restore",
        help="restore missing .torrent metadata on Pirate Face",
        description="Uploads one or more .torrent files (or a torrent client folder) to restore missing metadata on Pirate Face.",
    )
    r.add_argument("paths", nargs="+", help=".torrent file(s) or folder(s) (e.g. qBittorrent BT_backup or Transmission torrents/)")
    r.add_argument("-y", "--yes", action="store_true", help="skip prompts")
    w = sub.add_parser(
        "watch",
        help="watch ComfyUI and/or Ollama model folders and share new models",
        description="Poll local model folders. ComfyUI weight files and complete Ollama manifests are packaged as "
        "direct Pirate Face listings under your account namespace.",
    )
    w.add_argument("--comfyui", metavar="MODELS_DIR", help="ComfyUI models folder (for example ComfyUI/models)")
    w.add_argument("--ollama", metavar="MODELS_DIR", help="Ollama models store (the folder containing manifests/ and blobs/)")
    w.add_argument("--owner", required=True, help="your Hugging Face username or organization")
    w.add_argument("--license", required=True, help="Pirate Face license ID to apply to every watched model")
    w.add_argument("--interval", type=float, default=60, help="seconds between scans (default: 60)")
    w.add_argument("--settle", type=float, default=60, help="ignore files modified within this many seconds (default: 60)")
    w.add_argument("--state", help="watch state file (default: $PIRATEFACE_SEEDS/.watch-state.json)")
    w.add_argument("--once", action="store_true", help="scan once, then exit")
    w.add_argument("--dry-run", action="store_true", help="list candidates only; requires --once")
    w.add_argument("-y", "--yes", action="store_true", help="submit every eligible new model without per-model prompts")
    args = p.parse_args()

    if args.cmd == "restore":
        from pirateface import restore

        return restore.run(args)

    if args.cmd in ("seed", "submit"):
        from pirateface.seed import command

        return command.run(args)

    if args.cmd == "watch":
        from pirateface.seed import watch

        return watch.run(args)

    from huggingface_hub import snapshot_download

    print(snapshot_download(args.repo_id, revision=args.revision, local_dir=args.local_dir, max_workers=1))
