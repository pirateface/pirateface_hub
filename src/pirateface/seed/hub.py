"""What Hugging Face says about a repo: commit, license and file fingerprints."""

import json
import os
import urllib.parse
import urllib.request
from importlib.metadata import version


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": f"pirateface/{version('pirateface')}"})
    if os.environ.get("HF_TOKEN"):
        req.add_header("Authorization", "Bearer " + os.environ["HF_TOKEN"])
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def hidden(path):
    return any(part.startswith(".") for part in path.split("/"))


def model(repo, revision):
    """(commit, model card data, {path: (fingerprint, is LFS)}). Raises if Hugging Face can't say.
    Plain HTTP on purpose: huggingface_hub would answer from Pirate Face for repos only it has.
    blobs=true lists LFS hashes even for gated repos."""
    rev = urllib.parse.quote(revision or "main", safe="")
    data = _get(f"https://huggingface.co/api/models/{repo}/revision/{rev}?blobs=true")
    files = {
        s["rfilename"]: (s["lfs"]["sha256"], True) if s.get("lfs") else (s.get("blobId"), False)
        for s in data.get("siblings") or []
        if not hidden(s["rfilename"])
    }
    return data["sha"], data.get("cardData") or {}, files


def license_id(card, licenses):
    """The card's license as a Pirate Face license id, else the card's first license, else "-"."""
    raw = card.get("license")
    found = [str(x).strip().lower() for x in (raw if isinstance(raw, list) else [raw]) if str(x or "").strip()]
    # Hugging Face tags custom licenses "other" and names them in license_name.
    names = {n: lic["id"] for lic in licenses["licenses"] for n in lic["hfNames"]}
    named = names.get(str(card.get("license_name") or "").strip().lower())
    if "other" in found and named:
        found.append(named)
    ids = [lic["id"] for lic in licenses["licenses"]]
    return next((x for x in ids if x in found), found[0] if found else "-")


def approved_license(repo, licenses):
    """The license of a repo Pirate Face approved one by one, else ""."""
    return next((r["license"] for r in licenses["repoLicenses"] if r["repoId"].lower() == repo.lower()), "")


def required_file(license, licenses):
    return next((lic["requiredFile"] for lic in licenses["licenses"] if lic["id"] == license), None)
