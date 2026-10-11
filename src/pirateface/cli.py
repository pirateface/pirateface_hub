import argparse

import pirateface._boot  # noqa: F401  # uv tools skip .pth files


def main():
    p = argparse.ArgumentParser(prog="pf")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download", help="download a model into the Hugging Face cache")
    d.add_argument("repo_id")
    d.add_argument("--revision")
    args = p.parse_args()

    from huggingface_hub import snapshot_download

    print(snapshot_download(args.repo_id, revision=args.revision, max_workers=1))
