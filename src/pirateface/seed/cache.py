"""pf seed --hf-cache: class every model in the Hugging Face cache as seed, submit or skip, then do it, asking per model."""

import re
import sys
from pathlib import Path

from pirateface.client import api
from pirateface.config import API
from pirateface.seed import command, hub, submit, user

HOURLY_LIMIT = 200  # submissions per account per hour, enforced by the site
SHARD = re.compile(r"-\d{5}-of-(\d{5})\.gguf$")
EXTRA = re.compile(r"(?:^|/)(?:mmproj|imatrix|mtp|dspark)(?=[-_.])", re.IGNORECASE)  # GGUF extras, not quants


def plan_cache():
    """(status, repo, revision, folder, size, reason) for every model in the Hugging Face cache.
    The cache names large files by their SHA-256, so matching them against Hugging Face needs no hashing."""
    from huggingface_hub import constants

    cache = Path(constants.HF_HUB_CACHE)
    if not cache.is_dir():
        raise user.SeedError(f"no Hugging Face cache at {cache} (set HF_HUB_CACHE)")
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
        # A GGUF repo holds many quants: one quant here is complete when every shard of it is.
        gguf = [p for p in remote if p.endswith(".gguf") and (snap / p).exists()]
        missing = sum(
            not (snap / SHARD.sub(f"-{i:05d}-of-{m[1]}.gguf", p)).exists()
            for p in gguf
            if (m := SHARD.search(p))
            for i in range(1, int(m[1]) + 1)
        )
        bad = 0
        for p, (want, is_lfs) in remote.items():
            local = snap / p
            if not local.exists():
                # Weights must be here (other GGUF quants need not be); a skipped README is fine.
                missing += is_lfs and not (gguf and p.endswith(".gguf"))
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
        elif any(
            remote[p][0] not in {f["sha256"] for q in entry.get("quants") or [entry] for f in q["files"]}
            for p in remote
            if command.WEIGHT.search(p) and not EXTRA.search(p) and (snap / p).exists()
        ):
            yield row("submit", "a quant Pirate Face doesn't list yet", size)
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
    key = user.load_key()
    needs, failed, later = [], [], []
    tries = 0
    for status, repo, rev, snap, size, _ in todo:
        if status == "submit" and tries >= HOURLY_LIMIT:
            later.append(repo)
            continue
        print()
        verb = "Seed" if status == "seed" else "Share"
        if not user.YES and not user.ask(f"==> {verb} {repo} ({size})? [y/N] ").lower().startswith("y"):
            continue
        try:
            if status == "seed":
                command.seed_listed(repo, snap, key)
            else:
                tries += 1
                submit.submit_new(repo, snap, rev, key)
        except user.RateLimited:
            later.append(repo)
            tries = HOURLY_LIMIT
        except user.NeedsYou as e:
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
