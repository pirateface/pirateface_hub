"""pf seed: share a model on Pirate Face. Listed already -> seed it (here). Not listed -> package, submit and seed it
(submit.py). --hf-cache does that for the whole Hugging Face cache (cache.py). Local files are only read, never changed."""

import os
import re
import sys
from pathlib import Path

from pirateface.client import api, torrent
from pirateface.config import API
from pirateface.seed import clients, package, submit, user
from pirateface.seed.user import SeedError

WEIGHT = re.compile(r"\.(safetensors|gguf|onnx|bin|pt|pth|ckpt|mlx|npz)$", re.IGNORECASE)  # as the site keys a quant


def local_copy(repo, revision, download):
    """The model's folder in the Hugging Face cache; downloads it first if download and it isn't there."""
    from huggingface_hub import snapshot_download

    try:
        return Path(snapshot_download(repo, revision=revision, local_files_only=True))
    except Exception:
        if not download:
            return None
    print(f"==> Downloading {repo}")
    os.environ["PF_NO_RESCUE"] = "1"  # we're about to submit it ourselves
    return Path(snapshot_download(repo, revision=revision, max_workers=1))


def weights(src, paths):
    """The weight files (each quant's own files) among paths that src has."""
    return {p for p in paths if WEIGHT.search(p) and Path(src, p).is_file()} if src else set()


def matching_quant(repo, src):
    """The listed quant whose weight files src has most of, or None for the default torrent.
    A revision can have one torrent per quant (say UD-Q8_K_XL and UD-IQ4_XS)."""
    entry = api.lookup(repo)
    quants = (entry.get("quants") or []) if isinstance(entry, dict) else []
    best = max(quants, key=lambda q: len(weights(src, [f["path"] for f in q["files"]])), default=None)
    return best["quant"] if len(quants) > 1 and weights(src, [f["path"] for f in best["files"]]) else None


def seed_listed(repo, src, key):
    """Seed the torrent Pirate Face already lists, with whatever files src has."""
    data = api.torrent_file(repo, matching_quant(repo, src))
    if not data:
        raise SeedError(f"Pirate Face has no .torrent for {repo} you can use yet (license terms?). See {API}/{repo}")
    paths = [p for p, _, _ in torrent.files_in(data)]
    mine = weights(src, [str(p.relative_to(src)) for p in Path(src).rglob("*")]) if src else set()
    if mine and not weights(src, paths):
        # Another quant of this revision: seeding the listed torrent would download it instead.
        raise SeedError(
            f"Pirate Face doesn't list your files for {repo} yet ({', '.join(sorted(mine)[:3])}). "
            f"Submit them as their own torrent: pf submit --repo {repo} {src}"
        )
    trackers = api.trackers(key)
    data = package.with_trackers(data, trackers)
    name, v1, v2, single = package.info(data)
    out = submit.SEEDS / repo.split("/")[1]
    save_dir = out / "files"
    have = package.link_existing(src, save_dir if single else save_dir / name, paths) if src else 0
    torrent_path = out / f"{name}.torrent"
    torrent_path.write_bytes(data)
    rest = "" if have == len(paths) else "; the client downloads the rest"
    print(f"==> {repo} is on Pirate Face. You have {have} of its {len(paths)} files{rest}.")
    submit.start_seeding(repo, data, save_dir, v1, v2, name, trackers, torrent_path, wait=False)


def seed(repo, folder=None, revision=None):
    if not submit.REPO.fullmatch(repo):
        raise SeedError("the repo must look like author/model")
    if revision and not submit.COMMIT.fullmatch(revision):
        raise SeedError("--revision must be a full 40-character commit ID")
    if folder and not Path(folder).is_dir():
        raise SeedError(f"{folder} is not a folder")
    key = user.load_key()
    entry = api.lookup(repo)
    if entry is None:
        raise SeedError(f"could not reach {API}")
    if entry is api.NOT_LISTED:
        src = Path(folder) if folder else local_copy(repo, revision, download=True)
        submit.submit_new(repo, src, revision or (None if folder else src.name), key)
    else:
        src = Path(folder) if folder else local_copy(repo, entry.get("revision"), download=False)
        seed_listed(repo, src, key)


def update_trackers():
    """Add this account's seeding tracker to every Pirate Face torrent already in the client. Trackers are
    outside the info dict: the infohash stays the same and nothing is downloaded again."""
    mine = api.trackers(user.load_key())[-1]
    if not re.fullmatch(r"https?://[^ ]+:6970/[^ ]+/announce", mine):
        raise SeedError(f"could not get your seeding tracker from {API}/api/trackers (key rejected or site down)")
    print("==> Adding your seeding tracker to Pirate Face torrents")
    try:
        clients.update_trackers(mine)
    except RuntimeError as e:
        raise SeedError(str(e)) from e
    print("    Done. Pirate Face counts your seeding from the next announce (within 10 minutes).")


def run(args):
    user.YES = args.yes
    try:
        if args.cmd == "submit":
            submit.submit(args)
        elif args.update_trackers:
            update_trackers()
        elif args.hf_cache:
            from pirateface.seed import cache  # imports this module for seed_listed

            cache.seed_cache()
        elif args.repo_id:
            seed(args.repo_id, args.folder, args.revision)
        else:
            raise SeedError("give a repo (author/model), --hf-cache or --update-trackers")
    except user.RateLimited as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(75)  # same code as package.sh, so scripts that loop over models can stop
    except (SeedError, package.LinkError) as e:
        sys.exit(f"error: {e}")
    except OSError as e:  # unreadable file, full disk, path too long: a message, not a stack trace
        hint = ""
        if os.name == "nt" and len(str(e.filename or "")) >= 260:
            hint = " (path over 260 characters: use a shorter --out, or turn on Windows long paths)"
        sys.exit(f"error: {e}{hint}")
