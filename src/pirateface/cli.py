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
    args = p.parse_args()

    if args.cmd == "seed":
        from pirateface.seed import command

        return command.run(args)

    from huggingface_hub import snapshot_download

    print(snapshot_download(args.repo_id, revision=args.revision, local_dir=args.local_dir, max_workers=1))
