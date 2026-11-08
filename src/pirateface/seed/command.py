"""pf seed: share a model on Pirate Face. Listed already -> seed it. Not listed -> package, submit and seed.
Local files are only read, never changed or uploaded."""

import base64
import contextlib
import getpass
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

from pirateface import ui
from pirateface.client import api, torrent
from pirateface.config import API
from pirateface.seed import clients, hub, package

TERMS_VERSION = "2026-09-18.2"
# %APPDATA% on Windows, else $XDG_CONFIG_HOME or ~/.config (where package.sh kept it).
CONFIG = os.environ.get("APPDATA") if os.name == "nt" else os.environ.get("XDG_CONFIG_HOME")
KEY_FILE = Path(CONFIG or Path.home() / ".config") / "pirateface" / "key"
SEEDS = Path(os.environ.get("PIRATEFACE_SEEDS") or Path.home() / "pirateface-seeds")
HOURLY_LIMIT = 200  # submissions per account per hour, enforced by the site
REPO = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
COMMIT = re.compile(r"[0-9a-fA-F]{40}")
YES = False


class SeedError(Exception):
    pass


class RateLimited(SeedError):
    pass


class NeedsYou(SeedError):
    """-y and no torrent client: nothing was submitted; the user has to open the .torrent."""

    def __init__(self, repo, torrent_path, save_dir):
        super().__init__(f"no torrent client answered. {clients.HELP}")
        self.repo, self.torrent_path, self.save_dir = repo, torrent_path, save_dir


def ask(prompt, hidden=False):
    if YES:
        raise SeedError(f"needs an answer to {prompt.strip()!r}; rerun without -y")
    if not sys.stdin.isatty():
        raise SeedError("no terminal for prompts (agents: pass -y with the user's OK)")
    return (getpass.getpass if hidden else input)(prompt).strip()


def load_key():
    """Account key from /account, saved for next time."""
    key = os.environ.get("PIRATEFACE_KEY") or (KEY_FILE.read_text().strip() if KEY_FILE.exists() else "")
    if key:
        return key
    if YES:
        raise SeedError(f"set PIRATEFACE_KEY (from {API}/account)")
    print(f"\n==> Your Pirate Face key: {API}/account -> Community torrent key")
    key = ask("    Paste it (hidden): ", hidden=True)
    if not key.startswith("pfct_"):
        raise SeedError("that doesn't look like a Pirate Face key (starts with pfct_)")
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.touch(mode=0o600)
    KEY_FILE.write_text(key + "\n")
    print(f"    Saved to {KEY_FILE}")
    return key


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


def start_seeding(repo, data, save_dir, v1, v2, name, trackers, torrent_path, wait):
    print("\n==> Start seeding")
    done = clients.add(data, save_dir, v1)
    if done:
        print(f"    {done}")
    elif YES:
        raise NeedsYou(repo, torrent_path, save_dir)
    else:
        link = package.magnet(v1, v2, name, trackers)
        print("    Command-click the magnet (Control-click on Linux). Your torrent client opens.")
        print(f"    Save the files to {save_dir} so it finds them and starts seeding.")
        print(f"\033]8;;{link}\a{link}\033]8;;\a" if ui.ansi() else link)
        print(f"    Or open the file: {torrent_path}")
        if wait:
            ask("    Press Enter once it says Seeding. ")
    print("    Keep it running. BitTorrent shows your IP address to other peers.")


def seed_listed(repo, src, key):
    """Seed the torrent Pirate Face already lists, with whatever files src has."""
    data = api.torrent_file(repo)
    if not data:
        raise SeedError(f"Pirate Face has no .torrent for {repo} you can use yet (license terms?). See {API}/{repo}")
    trackers = api.trackers(key)
    data = package.with_trackers(data, trackers)
    name, v1, v2, single = package.info(data)
    out = SEEDS / repo.split("/")[1]
    save_dir = out / "files"
    paths = [p for p, _, _ in torrent.files_in(data)]
    have = package.link_existing(src, save_dir if single else save_dir / name, paths) if src else 0
    torrent_path = out / f"{name}.torrent"
    torrent_path.write_bytes(data)
    rest = "" if have == len(paths) else "; the client downloads the rest"
    print(f"==> {repo} is on Pirate Face. You have {have} of its {len(paths)} files{rest}.")
    start_seeding(repo, data, save_dir, v1, v2, name, trackers, torrent_path, wait=False)


class Answers:
    """Prompts during a submission. A dry run never asks: it records what a real run would ask for."""

    def __init__(self, dry_run):
        self.dry_run, self.skipped = dry_run, []

    def ask(self, prompt, what):
        if self.dry_run:
            self.skipped.append(what)
            print(f"    (dry run: a real run asks for the {what} here)")
            return None
        return ask(prompt)


def check_web_seed(url):
    """The site stores a direct listing's web seed (BEP 19 base URL) as given; only its form is checked here."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise SeedError("--web-seed must be an https:// URL without a user name or password")


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
):
    """Package src, check it against Hugging Face (unless direct), start seeding, then submit.
    A dry run stops before the torrent client and the POST, and prints what it would send."""
    check_client_version()
    name = name or repo.split("/")[1]
    out = Path(out) if out else SEEDS / name
    save_dir = out / "files"
    answers = Answers(dry_run)
    # The torrent carries this account's seeding tracker. Trackers are outside the info dict,
    # so the infohash is the same with or without a key.
    trackers = api.trackers(key) if key else api.TRACKERS

    print(f"==> Selecting files in {src}")
    paths, skipped = package.stage(src, save_dir / name)
    for p in skipped:
        print(f"    skip  {p}")
    if not paths:
        raise SeedError("no files left to package")
    print(f"    {len(paths)} files included, {len(skipped)} skipped")

    print("==> Hashing (SHA-256)")
    sums = package.checksums(save_dir / name, paths)
    print("==> Creating torrent")
    data, v1, v2 = package.make_torrent(save_dir, name, trackers)
    torrent_path = out / f"{name}.torrent"
    torrent_path.write_bytes(data)

    try:
        licenses = api.licenses()
    except OSError as e:
        raise SeedError(f"could not read the license list from {API}/api/licenses ({e})") from e
    ids = [lic["id"] for lic in licenses["licenses"]]
    match = False
    if direct:
        # Direct listing: not on Hugging Face. The revision is the torrent's v1 infohash.
        print("==> Direct listing: skipping the Hugging Face check")
        revision, hf_license = revision or v1, license or "-"
    else:
        # If every Hugging Face LFS hash matches, it's listed right away instead of going to review.
        print(f"==> Checking against huggingface.co/{repo}")
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
            match = bool(lfs) and not bad
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

    if match:
        evidence = (
            f"Source: https://huggingface.co/{repo} at revision {revision}. License per model card: {lic}. "
            f"All Hugging Face LFS SHA-256 hashes at that revision match the local copy (checked by {command})."
        )
    elif direct:
        evidence = f"Direct listing: published on Pirate Face by {repo.split('/')[0]} itself, not on Hugging Face. License: {lic}."
    else:
        print("    Where did this copy come from? Links, license proof, and why it's gone from Hugging Face if it is.")
        evidence = answers.ask("    Evidence: ", "evidence")
        if evidence is not None and len(evidence) < 30:
            raise SeedError("evidence needs at least 30 characters")

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
    readme = save_dir / name / "README.md"
    if readme.is_file() and readme.stat().st_size <= 200_000:
        with contextlib.suppress(UnicodeDecodeError):
            body["readme"] = readme.read_bytes().decode("utf-8")  # as is: the site checks it against the manifest
    if web_seed:
        body["webSeed"] = web_seed

    if dry_run:
        print_dry_run(body, data, v1, v2, torrent_path, answers.skipped)
        return

    # Seed first: a listed magnet is only useful if someone is sharing the files.
    start_seeding(repo, data, save_dir, v1, v2, name, trackers, torrent_path, wait=True)

    print(f"""
==> Submitting {repo} ({lic}, {len(paths)} files) to {API}. By typing yes you confirm:
    1. You have the complete files and the right to redistribute them.
    2. The repository, revision, license, checksums and evidence are accurate, and
       the files are not malware, mislabeled or tampered with. You take responsibility.
    3. Pirate Face may review, reject or remove it and respond to takedowns.
    4. BitTorrent shows your IP to peers, and the torrent has no secrets or personal info.
    5. You agree to the Terms ({API}/terms#model-torrent-submissions) and Listing policy ({API}/policy).""")
    if not YES and ask("    Type yes: ") != "yes":
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
            print(f"Run `pf seed {repo}` to seed the listed torrent with these files.")
    elif code == 401:
        KEY_FILE.unlink(missing_ok=True)
        raise SeedError(f"key rejected. Get a new one at {API}/account and rerun")
    elif code == 429:
        raise RateLimited(str(result))
    else:
        raise SeedError(f"HTTP {code}: {result}")
    print(f"Keep seeding. Your files: {out}")


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


UPGRADE = "uvx -p 3.13 --from pirateface@latest pf ..., or pipx upgrade pirateface, or pip install -U pirateface"


def check_client_version():
    """Stop before any hashing if Pirate Face no longer accepts submissions from this version."""
    minimum = api.min_client_version()
    if minimum and api.older(api.VERSION, minimum):
        raise SeedError(
            f"this is pirateface {api.VERSION}; Pirate Face now needs {minimum} or newer to submit. Upgrade: {UPGRADE}"
        )


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
        key = os.environ.get("PIRATEFACE_KEY") or (KEY_FILE.read_text().strip() if KEY_FILE.exists() else None)
    else:
        key = load_key()
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


def seed(repo, folder=None, revision=None):
    if not REPO.fullmatch(repo):
        raise SeedError("the repo must look like author/model")
    if revision and not COMMIT.fullmatch(revision):
        raise SeedError("--revision must be a full 40-character commit ID")
    if folder and not Path(folder).is_dir():
        raise SeedError(f"{folder} is not a folder")
    key = load_key()
    entry = api.lookup(repo)
    if entry is None:
        raise SeedError(f"could not reach {API}")
    if entry is api.NOT_LISTED:
        src = Path(folder) if folder else local_copy(repo, revision, download=True)
        submit_new(repo, src, revision or (None if folder else src.name), key)
    else:
        src = Path(folder) if folder else local_copy(repo, entry.get("revision"), download=False)
        seed_listed(repo, src, key)


def plan_cache():
    """(status, repo, revision, folder, size, reason) for every model in the Hugging Face cache.
    The cache names large files by their SHA-256, so matching them against Hugging Face needs no hashing."""
    from huggingface_hub import constants

    cache = Path(constants.HF_HUB_CACHE)
    if not cache.is_dir():
        raise SeedError(f"no Hugging Face cache at {cache} (set HF_HUB_CACHE)")
    print(f"==> Checking models in {cache}")
    licenses = api.licenses()
    ids = [lic["id"] for lic in licenses["licenses"]]
    for snap in sorted(cache.glob("models--*/snapshots/*")):
        repo = snap.parent.parent.name.removeprefix("models--").replace("--", "/", 1)
        rev = snap.name

        def row(status, why, size="-", repo=repo, rev=rev, snap=snap):
            return status, repo, rev, snap, size, why

        try:
            _, card, remote = hub.model(repo, rev)
        except Exception:
            yield row("skip", "not readable on Hugging Face (gated? set HF_TOKEN)")
            continue
        lic = hub.license_id(card, licenses)
        size = sum((snap / p).stat().st_size for p in remote if (snap / p).exists())
        size = f"{size / 1e9:.1f} GB" if size >= 1e8 else f"{size / 1e6:.0f} MB"
        if lic not in ids and not hub.approved_license(repo, licenses):
            yield row("skip", f"license {lic}", size)
            continue
        missing = bad = 0
        for p, (want, is_lfs) in remote.items():
            local = snap / p
            if not local.exists():
                missing += is_lfs  # weights must all be here; a skipped README is fine
                continue
            # The snapshot link names the blob by its hash; huggingface_hub 2.x links that blob on to a
            # shared store under another name, so follow one level only.
            blob = local.readlink() if local.is_symlink() else local.resolve()
            bad += blob.name != want
        need = hub.required_file(lic, licenses)
        if bad:
            yield row("skip", f"{bad} files differ from Hugging Face", size)
            continue
        if missing:
            yield row("skip", f"partial download: {missing} weight files missing", size)
            continue
        if need and not (snap / need).exists():
            yield row("skip", f"{lic} copies must include {need}; download it too", size)
            continue
        entry = api.lookup(repo)
        if entry is None:
            yield row("skip", "could not reach Pirate Face", size)
        elif entry is api.NOT_LISTED:
            yield row("submit", "complete, not on Pirate Face yet", size)
        elif entry.get("revision") != rev:
            yield row("skip", "on Pirate Face at another revision", size)
        elif not entry.get("torrent"):
            yield row("skip", f"on Pirate Face; accept the license terms at {API}/{repo}", size)
        else:
            yield row("seed", "on Pirate Face; seed it", size)


def seed_cache():
    """Seed or submit every complete model in the Hugging Face cache, asking per model."""
    plan = []
    for r in plan_cache():
        print(f"    {r[0]:<7} {r[1]:<48} {r[4]:>9}  {r[5]}")
        plan.append(r)
    todo = [r for r in plan if r[0] in ("seed", "submit")]
    if not todo:
        print("\n    Nothing to seed or submit.")
        return
    key = load_key()
    needs, failed, later = [], [], []
    tries = 0
    for status, repo, rev, snap, size, _ in todo:
        if status == "submit" and tries >= HOURLY_LIMIT:
            later.append(repo)
            continue
        print()
        verb = "Seed" if status == "seed" else "Share"
        if not YES and not ask(f"==> {verb} {repo} ({size})? [y/N] ").lower().startswith("y"):
            continue
        try:
            if status == "seed":
                seed_listed(repo, snap, key)
            else:
                tries += 1
                submit_new(repo, snap, rev, key)
        except RateLimited:
            later.append(repo)
            tries = HOURLY_LIMIT
        except NeedsYou as e:
            needs.append(e)
        except Exception as e:
            print(f"error: {e}\n    {repo} skipped; continuing", file=sys.stderr)
            failed.append(repo)
    if needs:
        print("\n==> Needs you: no torrent client could be reached for these. Nothing was submitted.")
        print("    Open each .torrent in your torrent client and save to the folder shown. Then run")
        print("    this again without -y and press Enter when each one says Seeding.")
        for e in needs:
            print(f"    {e.repo}\n        open:    {e.torrent_path}\n        save to: {e.save_dir}")
    if failed:
        print(f"\n==> Not done (see the error above each one): {' '.join(failed)}")
    if later:
        print(
            f"\n==> Next hour: Pirate Face takes {HOURLY_LIMIT} submissions an hour. Run this again then for: {' '.join(later)}"
        )


def update_trackers():
    """Add this account's seeding tracker to every Pirate Face torrent already in the client. Trackers are
    outside the info dict: the infohash stays the same and nothing is downloaded again."""
    mine = api.trackers(load_key())[-1]
    if not re.fullmatch(r"https?://[^ ]+:6970/[^ ]+/announce", mine):
        raise SeedError(f"could not get your seeding tracker from {API}/api/trackers (key rejected or site down)")
    print("==> Adding your seeding tracker to Pirate Face torrents")
    try:
        clients.update_trackers(mine)
    except RuntimeError as e:
        raise SeedError(str(e)) from e
    print("    Done. Pirate Face counts your seeding from the next announce (within 10 minutes).")


def run(args):
    global YES
    YES = args.yes
    try:
        if args.cmd == "submit":
            submit(args)
        elif args.update_trackers:
            update_trackers()
        elif args.hf_cache:
            seed_cache()
        elif args.repo_id:
            seed(args.repo_id, args.folder, args.revision)
        else:
            raise SeedError("give a repo (author/model), --hf-cache or --update-trackers")
    except RateLimited as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(75)  # same code as package.sh, so scripts that loop over models can stop
    except (SeedError, package.LinkError) as e:
        sys.exit(f"error: {e}")
    except OSError as e:  # unreadable file, full disk, path too long: a message, not a stack trace
        hint = ""
        if os.name == "nt" and len(str(e.filename or "")) >= 260:
            hint = " (path over 260 characters: use a shorter --out, or turn on Windows long paths)"
        sys.exit(f"error: {e}{hint}")
