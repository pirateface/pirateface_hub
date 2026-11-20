"""What Hugging Face says about a repo: commit, license and file fingerprints."""

import http.client
import json
import os
import urllib.parse
import urllib.request
from importlib.metadata import version


class _SameHostAuth(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but send the Hugging Face token only to the host it was meant for: LFS files
    redirect to a CDN on another host, which needs no token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new and urllib.parse.urlsplit(new.full_url)[:2] != urllib.parse.urlsplit(req.full_url)[:2]:
            new.remove_header("Authorization")
        return new


urlopen = urllib.request.build_opener(_SameHostAuth).open


def headers():
    """Headers for a request to Hugging Face: pf's User-Agent, and HF_TOKEN as a bearer token when set."""
    h = {"User-Agent": f"pirateface/{version('pirateface')}"}
    if os.environ.get("HF_TOKEN"):
        h["Authorization"] = "Bearer " + os.environ["HF_TOKEN"]
    return h


def _get(url):
    with urlopen(urllib.request.Request(url, headers=headers()), timeout=30) as r:
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


def license_problem(licenses):
    """What is wrong with this licence body, or None when it is the shape the functions below index: a dict
    with a licence list of dicts carrying a string id, hfNames as a list of strings and requiredFile present
    (a string or null), and a repoLicenses list of dicts carrying repoId and license. requiredFile has to be
    there: an absent key would otherwise read as "no file required" and pass silently. Naming the first
    fault beats a bare False, because a shape change on the site should say which field moved."""
    if not isinstance(licenses, dict):
        return f"the body is {type(licenses).__name__}, not an object"
    listed = licenses.get("licenses")
    if not isinstance(listed, list):
        return "licenses is not a list"
    for index, item in enumerate(listed):
        where = f"licenses[{index}]"
        if not isinstance(item, dict):
            return f"{where} is {type(item).__name__}, not an object"
        if not isinstance(item.get("id"), str):
            return f"{where} has no string id"
        names = item.get("hfNames")
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            return f"{where} has hfNames that are not a list of strings"
        if "requiredFile" not in item:
            return f"{where} has no requiredFile"
        if not isinstance(item["requiredFile"], (str, type(None))):
            return f"{where} has a requiredFile of {type(item['requiredFile']).__name__}, not a string or null"
    approved = licenses.get("repoLicenses")
    if not isinstance(approved, list):
        return "repoLicenses is not a list"
    for index, row in enumerate(approved):
        where = f"repoLicenses[{index}]"
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("repoId"), str)
            or not isinstance(row.get("license"), str)
        ):
            return f"{where} has no string repoId and license"
    return None


def licenses_or_fail():
    """The site's licence list, or the one SeedError that names it. The single place where fetching it
    and judging its shape happen, so pf seed and pf submit cannot drift apart."""
    from pirateface.client import api
    from pirateface.config import API
    from pirateface.seed import user

    try:
        licenses = api.licenses()
    except (OSError, ValueError, http.client.HTTPException, RecursionError) as e:
        # URLError, HTTPError, JSONDecodeError, a non-UTF-8 body, a bad status line, a truncated body,
        # a body nested deeply enough to exhaust the decoder
        raise user.SeedError(f"could not read the license list from {API}/api/licenses ({e})") from e
    problem = license_problem(licenses)
    if problem:
        raise user.SeedError(f"could not read the license list from {API}/api/licenses ({problem})")
    return licenses


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
