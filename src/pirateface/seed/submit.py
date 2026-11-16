"""pf submit, and the half of pf seed that shares a model Pirate Face doesn't list: package, check, seed, submit."""

import base64
import contextlib
import json
import os
import re
import urllib.parse
from pathlib import Path

from pirateface import ui
from pirateface.client import api
from pirateface.config import API, SEEDS
from pirateface.seed import clients, hub, package, user
from pirateface.seed.per_file.command import seed_model
from pirateface.seed.user import SeedError

TERMS_VERSION = "2026-09-18.2"
REPO = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
COMMIT = re.compile(r"[0-9a-fA-F]{40}")
UPGRADE = "uvx -p 3.13 --from pirateface@latest pf ..., or pipx upgrade pirateface, or pip install -U pirateface"


def start_seeding(repo, data, save_dir, v1, v2, name, trackers, torrent_path, wait, per_file=False):
    print("\n==> Start seeding")
    # Per-file swarms live in qBittorrent, which can only skip their check when it holds the model too.
    done = clients.add(data, save_dir, v1, prefer=clients.QBittorrent if per_file else None)
    if done:
        print(f"    {done}")
    elif user.YES:
        raise user.NeedsYou(repo, torrent_path, save_dir)
    else:
        link = package.magnet(v1, v2, name, trackers)
        print("    Command-click the magnet (Control-click on Linux). Your torrent client opens.")
        print(f"    Save the files to {save_dir} so it finds them and starts seeding.")
        print(f"\033]8;;{link}\a{link}\033]8;;\a" if ui.ansi() else link)
        print(f"    Or open the file: {torrent_path}")
        if wait:
            user.ask("    Press Enter once it says Seeding. ")
    print("    Keep it running. BitTorrent shows your IP address to other peers.")


class Answers:
    """Prompts during a submission. A dry run never asks: it records what a real run would ask for."""

    def __init__(self, dry_run):
        self.dry_run, self.skipped = dry_run, []

    def ask(self, prompt, what):
        if self.dry_run:
            self.skipped.append(what)
            print(f"    (dry run: a real run asks for the {what} here)")
            return None
        return user.ask(prompt)


def check_web_seed(url):
    """The site stores a direct listing's web seed (BEP 19 base URL) as given; only its form is checked here."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise SeedError("--web-seed must be an https:// URL without a user name or password")


def check_client_version():
    """Stop before any hashing if Pirate Face no longer accepts submissions from this version."""
    minimum = api.min_client_version()
    if minimum and api.older(api.VERSION, minimum):
        raise SeedError(
            f"this is pirateface {api.VERSION}; Pirate Face now needs {minimum} or newer to submit. Upgrade: {UPGRADE}"
        )


def build_torrent(src, save_dir, name, trackers):
    """Stage src's files under save_dir/name, hash them and make the torrent. Returns (paths, sha256s, torrent, v1, v2)."""
    print(f"==> Selecting files in {src}")
    paths, skipped = package.stage(src, save_dir / name)
    for p in skipped:
        print(f"    skip  {p}")
    if not paths:
        raise SeedError("no files left to package")
    print(f"    {len(paths)} files included, {len(skipped)} skipped")
    print("==> Hashing and creating the torrent")
    sums = {}  # SHA-256 of each file, filled by the same read that hashes the pieces
    data, v1, v2 = package.make_torrent(save_dir, name, trackers, sums)
    return paths, sums, data, v1, v2


def check_hugging_face(repo, revision, license, sums, licenses, answers):
    """Compare the hashes with Hugging Face. Returns (match, revision, Hugging Face's license id). match is
    "n of the m" LFS files that are in the torrent and match, when at least one is and none differs, else None."""
    # If every Hugging Face LFS hash matches, it's listed right away instead of going to review.
    print(f"==> Checking against huggingface.co/{repo}")
    match = None
    try:
        commit, card, remote = hub.model(repo, revision)
        lfs = {p: h for p, (h, is_lfs) in remote.items() if is_lfs}
        bad = [p for p in lfs if p in sums and sums[p] != lfs[p]]
        missing = [p for p in lfs if p not in sums]
        hf_license = hub.license_id(card, licenses)
        matched = len(lfs) - len(bad) - len(missing)
        print(f"    revision {commit}: {matched}/{len(lfs)} LFS files match, license {hf_license}")
        for p in bad:
            print(f"    MISMATCH  {p}")
        for p in missing:
            print(f"    not in torrent  {p}")
        match = f"{matched} of the {len(lfs)}" if matched and not bad else None
    except Exception as e:
        print(f"    could not read it from Hugging Face ({e}); the review team will check by hand")
        commit, hf_license = None, "-"
    if hf_license == "-" and license:
        hf_license = license
    if match:
        revision = commit
    elif not revision:
        print("    Hashes don't match Hugging Face, so it goes to manual review.")
        revision = answers.ask("    Commit ID your copy came from (40 characters): ", "revision")
    if revision is not None and not COMMIT.fullmatch(revision):
        raise SeedError("need the full 40-character commit ID; rerun with --revision")
    return match, revision, hf_license


def choose_license(repo, hf_license, sums, licenses, answers):
    """The Pirate Face license id to submit under; stops if it isn't listed or its required file is missing."""
    ids = [lic["id"] for lic in licenses["licenses"]]
    approved = hub.approved_license(repo, licenses)
    if approved and hf_license in (approved, "other", "-"):
        # Approved one by one: with no license on Hugging Face, or "other" for a listed license, it takes the approved one.
        lic = approved if hf_license == "-" or (hf_license == "other" and approved in ids) else hf_license
    elif hf_license in ids:
        lic = hf_license
    elif hf_license == "-":
        lic = answers.ask(f"    License ({', '.join(ids)}): ", "license")
        if lic is not None and lic not in ids:
            raise SeedError(f"Pirate Face lists only the licenses at {API}/policy")
    else:
        raise SeedError(f"license is {hf_license}; Pirate Face lists only the licenses at {API}/policy")
    need = lic and hub.required_file(lic, licenses)
    if need and need not in sums:
        raise SeedError(
            f"the {lic} license requires {need} in the torrent; download it into the model folder and rerun"
        )
    return lic


def evidence_for(repo, revision, lic, match, direct, command, answers):
    if match:
        return (
            f"Source: https://huggingface.co/{repo} at revision {revision}. License per model card: {lic}. "
            f"{match} Hugging Face LFS files at that revision are in this torrent, and their SHA-256 hashes match "
            f"the local copy (checked by {command})."
        )
    if direct:
        return f"Direct listing: published on Pirate Face by {repo.split('/')[0]} itself, not on Hugging Face. License: {lic}."
    print("    Where did this copy come from? Links, license proof, and why it's gone from Hugging Face if it is.")
    evidence = answers.ask("    Evidence: ", "evidence")
    if evidence is not None and len(evidence) < 30:
        raise SeedError("evidence needs at least 30 characters")
    return evidence


def request_body(repo, revision, lic, evidence, sums, data, v1, v2, readme, web_seed):
    """The POST /api/community-torrents body."""
    if len(data) > 3_000_000:
        raise SeedError("the .torrent is over the 3 MB upload limit")
    body = {
        "repoId": repo,
        "revision": revision,
        "magnet": package.magnet(v1, v2),  # the site adds the trackers
        "license": lic,
        "evidence": evidence,
        "manifest": [{"path": p, "sha256": h} for p, h in sorted(sums.items())],
        "torrent": base64.b64encode(data).decode(),  # lets the magnet work with no peer online
        "submissionTermsVersion": TERMS_VERSION,
        **dict.fromkeys(
            [
                "redistributionConfirmed",
                "accuracyConfirmed",
                "reviewAcknowledged",
                "privacyAcknowledged",
                "termsAccepted",
            ],
            True,
        ),
    }
    # README.md is the model card. Pirate Face shows it for models that aren't on Hugging Face.
    if readme.is_file() and readme.stat().st_size <= 200_000:
        with contextlib.suppress(UnicodeDecodeError):
            body["readme"] = readme.read_bytes().decode("utf-8")  # as is: the site checks it against the manifest
    if web_seed:
        body["webSeed"] = web_seed
    return body


def confirm_and_post(repo, src, key, body, lic, paths, out, torrent_path):
    """Ask for the submission terms, POST, and say what the answer means."""
    print(f"""
==> Submitting {repo} ({lic}, {len(paths)} files) to {API}. By typing yes you confirm:
    1. You have the complete files and the right to redistribute them.
    2. The repository, revision, license, checksums and evidence are accurate, and
       the files are not malware, mislabeled or tampered with. You take responsibility.
    3. Pirate Face may review, reject or remove it and respond to takedowns.
    4. BitTorrent shows your IP to peers, and the torrent has no secrets or personal info.
    5. You agree to the Terms ({API}/terms#model-torrent-submissions) and Listing policy ({API}/policy).""")
    if not user.YES and user.ask("    Type yes: ") != "yes":
        raise SeedError(f"not submitted. Your torrent is in {out}; rerun any time")

    code, answer = api.submit(key, body)
    result = answer.get("status") or answer.get("error") or answer
    print()
    if code == 200 and result == "approved":
        print(f"Listed! {API}/{repo}")
    elif code == 200 and result == "pending":
        print(f"Submitted. The repo is not on Hugging Face, so it's in the review queue: {API}/{repo} once approved.")
    elif code == 409:
        print(answer.get("error") or "Already submitted.")
        if answer.get("existing", {}).get("magnet"):
            # The torrent just added is not listed: the listed one has these files.
            print(f"Remove {torrent_path.name} from your torrent client; Pirate Face does not list it.")
            print(f"Then run `pf seed {repo} {src}` to seed the listed torrent with these files.")
            return
    elif code == 401:
        if not os.environ.get("PIRATEFACE_KEY"):
            user.KEY_FILE.unlink(missing_ok=True)  # an env-var key may not be the one saved on disk
        raise SeedError(f"key rejected. Get a new one at {API}/account and rerun")
    elif code == 429:
        raise user.RateLimited(str(result))
    else:
        raise SeedError(f"HTTP {code}: {result}")
    print(f"Keep seeding. Your files: {out}")


def submit_new(
    repo,
    src,
    revision,
    key,
    *,
    name=None,
    out=None,
    direct=False,
    license=None,
    web_seed=None,
    dry_run=False,
    command="pf seed",
    per_file=False,
):
    """Package src, check it against Hugging Face (unless direct), start seeding, then submit.
    A dry run stops before the torrent client and the POST, and prints what it would send."""
    check_client_version()
    name = name or repo.split("/")[1]
    # The shared-seeds default keeps each author in their own author--model folder (seed/package.py:seeds_folder
    # applies the same name; seed_listed reuses an old <model> folder that already holds the torrent). An
    # explicit out is used as given; pf submit's own ./pirateface-NAME default is one.
    out = Path(out or Path(SEEDS, repo.replace("/", "--")))
    save_dir = out / "files"
    answers = Answers(dry_run)
    # The torrent carries this account's seeding tracker. Trackers are outside the info dict,
    # so the infohash is the same with or without a key.
    trackers = api.trackers(key) if key else api.TRACKERS

    paths, sums, data, v1, v2 = build_torrent(src, save_dir, name, trackers)
    torrent_path = out / f"{name}.torrent"
    torrent_path.write_bytes(data)

    try:
        licenses = api.licenses()
    except OSError as e:
        raise SeedError(f"could not read the license list from {API}/api/licenses ({e})") from e
    match = None
    if direct:
        # Direct listing: not on Hugging Face. The revision is the torrent's v1 infohash.
        print("==> Direct listing: skipping the Hugging Face check")
        revision, hf_license = revision or v1, license or "-"
    else:
        match, revision, hf_license = check_hugging_face(repo, revision, license, sums, licenses, answers)
    lic = choose_license(repo, hf_license, sums, licenses, answers)
    evidence = evidence_for(repo, revision, lic, match, direct, command, answers)
    body = request_body(repo, revision, lic, evidence, sums, data, v1, v2, save_dir / name / "README.md", web_seed)

    if dry_run:
        print_dry_run(body, data, v1, v2, torrent_path, answers.skipped)
        return

    # Seed first: a listed magnet is only useful if someone is sharing the files.
    start_seeding(repo, data, save_dir, v1, v2, name, trackers, torrent_path, wait=True, per_file=per_file)
    confirm_and_post(repo, src, key, body, lic, paths, out, torrent_path)
    if per_file:
        seed_model(data, save_dir, v1, verified=True)  # build_torrent just hashed every file


def print_dry_run(body, data, v1, v2, torrent_path, skipped):
    shown = dict(body, torrent=f"<{len(data)} bytes, base64>")
    if "readme" in shown:
        shown["readme"] = f"<README.md, {len(body['readme'])} characters>"
    print("\n==> Dry run: nothing added to a torrent client, nothing submitted")
    print(f"    v1 infohash  {v1}")
    print(f"    v2 infohash  {v2}")
    print(f"    torrent      {torrent_path}")
    print(f"    POST {API}/api/community-torrents")
    print(json.dumps(shown, indent=2))
    if skipped:
        print(f"    A real run asks for: {', '.join(skipped)}")


def submit(args):
    """pf submit: package.sh's folder flow. Flags match package.sh."""
    if not (args.folder and args.repo):
        raise SeedError("usage: pf submit --repo author/model [options] ./model-folder")
    if not REPO.fullmatch(args.repo):
        raise SeedError("--repo must look like author/model")
    if args.revision and not COMMIT.fullmatch(args.revision):
        raise SeedError("--revision must be a full 40-character commit ID")
    if args.name and not re.fullmatch(r"[A-Za-z0-9._-]+", args.name):
        raise SeedError("--name may only use letters, numbers, . _ -")
    if args.web_seed:
        if not args.direct:
            raise SeedError("--web-seed is only for direct listings (--direct)")
        check_web_seed(args.web_seed)
    src = Path(args.folder)
    if not src.is_dir():
        raise SeedError(f"{args.folder} is not a folder")
    src = src.resolve()
    name = args.name or src.name
    if package.unportable([name]):
        raise SeedError(f"{name!r} can't be a folder name on Windows; pass --name")
    out = Path(args.out) if args.out else Path.cwd() / f"pirateface-{name}"
    # A dry run needs no key: without one the torrent gets only the public trackers (same infohash).
    if args.dry_run:
        key = os.environ.get("PIRATEFACE_KEY") or (
            user.KEY_FILE.read_text().strip() if user.KEY_FILE.exists() else None
        )
    else:
        key = user.load_key()
    submit_new(
        args.repo,
        src,
        args.revision,
        key,
        name=name,
        out=out,
        direct=args.direct,
        license=args.license,
        web_seed=args.web_seed,
        dry_run=args.dry_run,
        command="pf submit",
    )
