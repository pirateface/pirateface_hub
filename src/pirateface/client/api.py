"""Everything that talks to pirateface.co."""

import atexit
import contextlib
import functools
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from importlib.metadata import version

from pirateface.config import API, flag

NOT_LISTED = object()


@functools.cache
def lookup(repo_id, quant=None):
    """Pirate Face's entry for the repo, NOT_LISTED, or None if unknown (network error).
    quant: one of entry["quants"][i]["quant"], when the revision has a torrent per quant."""
    url = f"{API}/api/torrents?" + urllib.parse.urlencode({"repo": repo_id, **({"quant": quant} if quant else {})})
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return NOT_LISTED if e.code == 404 else None
    except Exception:
        return None


def lookup_magnet(repo_id):
    """The magnet, NOT_LISTED, or None if unknown (network error, or license terms to accept first)."""
    entry = lookup(repo_id)
    return entry.get("magnet") if isinstance(entry, dict) else entry


@functools.cache
def torrent_file(repo_id, quant=None):
    """The .torrent's bytes if Pirate Face publishes one for the repo (or that quant), else None."""
    entry = lookup(repo_id, quant)
    path = entry.get("torrent") if isinstance(entry, dict) else None
    if not path:
        return None
    try:
        with urllib.request.urlopen(urllib.parse.urljoin(API, path), timeout=30) as r:
            return r.read()
    except Exception:
        return None


@functools.cache
def pf_only(repo_id):
    """The repo's commit and files {path: (size, sha256, url)} if only Pirate Face has it, else None."""
    entry = lookup(repo_id)
    if not (isinstance(entry, dict) and entry.get("hf") is False):
        return None
    data = torrent_file(repo_id)
    if not data:
        return None
    from pirateface.client import torrent  # torrent imports api

    sha = {f["path"]: f["sha256"] for f in entry.get("files") or []}
    files = {path: (size, sha[path], url) for path, size, url in torrent.files_in(data) if path in sha}
    return {"repo_id": entry["repoId"], "commit": entry["revision"], "files": files} if files else None


# Fallback when the site doesn't answer; normally replaced by /api/trackers.
TRACKERS = ["udp://tracker.pirateface.co:6969/announce", "http://tracker.pirateface.co:6969/announce"]


def trackers(key):
    """Public trackers plus, last, this account's seeding tracker (from the site, so changes need no release)."""
    req = urllib.request.Request(f"{API}/api/trackers", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            lines = r.read().decode().split()
    except Exception:
        return TRACKERS
    return [t for t in lines if t.startswith(("udp://", "http://", "https://"))] or TRACKERS


@functools.cache
def licenses():
    """The licenses Pirate Face lists, and repos approved one by one."""
    with urllib.request.urlopen(f"{API}/api/licenses", timeout=30) as r:
        return json.load(r)


VERSION = version("pirateface")


def min_client_version():
    """The oldest pirateface Pirate Face takes submissions from (GET /api/cli), or None if it doesn't say."""
    try:
        with urllib.request.urlopen(f"{API}/api/cli", timeout=15) as r:
            minimum = json.load(r).get("minVersion")
    except Exception:
        return None  # an older site without /api/cli, or offline: the submission itself will say
    return minimum if isinstance(minimum, str) and re.fullmatch(r"\d+(\.\d+)*", minimum) else None


def _release(v):
    return [int(x) for x in re.match(r"\d+(?:\.\d+)*", v).group().split(".")]


def older(a, b):
    """a < b, comparing release numbers like 0.1.2 (a suffix such as .dev1 is ignored)."""
    return _release(a) < _release(b)


def submit(key, body):
    """POST a community torrent. Returns (HTTP status, JSON answer)."""
    req = urllib.request.Request(
        f"{API}/api/community-torrents",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": f"pirateface/{VERSION}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {"error": e.reason}


def restore(key, data):
    """POST raw .torrent bytes to /api/torrents/restore. Returns (HTTP status, JSON answer)."""
    req = urllib.request.Request(
        f"{API}/api/torrents/restore",
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/x-bittorrent",
            "User-Agent": f"pirateface/{VERSION}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {"error": e.reason}


_rescues = []


def _send_rescue(repo_id):
    req = urllib.request.Request(
        f"{API}/api/rescue",
        data=json.dumps({"repoId": repo_id}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with contextlib.suppress(Exception):
        urllib.request.urlopen(req, timeout=10).close()


def request_rescue(repo_id):
    """Ask Pirate Face to add a model it doesn't have yet, in the background."""
    if flag("PF_NO_RESCUE"):
        return False
    t = threading.Thread(target=_send_rescue, args=(repo_id,), daemon=True)
    t.start()
    _rescues.append(t)
    return True


@atexit.register
def _wait_for_rescues():
    for t in _rescues:
        t.join(timeout=2)
