"""Everything that talks to pirateface.co."""

import atexit
import contextlib
import functools
import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from importlib.metadata import version

from pirateface.config import API, flag

NOT_LISTED = object()
LICENSE_REQUIRED = object()

# One dead lookup per half-minute, not one per file: an outage costs 10s once, then fails fast.
UNREACHABLE_FOR = 30.0
_down_until = 0.0

# The site takes .torrent uploads up to 3 MB; the copy it serves adds its trackers and web seeds.
MAX_TORRENT_BYTES = 4_000_000


class Unreachable(Exception):
    """pirateface.co didn't answer: distinct from "this repo isn't listed"."""


def _read_torrent(url, timeout):
    """The .torrent at url, or None if it is over MAX_TORRENT_BYTES."""
    with urllib.request.urlopen(url, timeout=timeout) as r:
        data = r.read(MAX_TORRENT_BYTES + 1)
    return data if len(data) <= MAX_TORRENT_BYTES else None


def _mark_down():
    global _down_until
    _down_until = time.monotonic() + UNREACHABLE_FOR


@functools.cache
def lookup(repo_id, quant=None):
    """Pirate Face's entry for the repo, NOT_LISTED, LICENSE_REQUIRED, or None for an answer we cannot use.

    Raises Unreachable when the site cannot be reached at all, so callers can tell an outage
    from a 404 instead of treating both as "not on Pirate Face".
    quant: one of entry["quants"][i]["quant"], when the revision has a torrent per quant.
    """
    if time.monotonic() < _down_until:
        raise Unreachable(f"could not reach {API}")
    params = {"repo": repo_id, **({"quant": quant} if quant else {})}
    url = f"{API}/api/torrents?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return NOT_LISTED
        if e.code in (401, 403):
            try:
                json.load(e)  # the site's own licence answers are JSON
            except Exception:
                # An HTML 401/403 is a captive portal, proxy or firewall page in front of the site, not a
                # licence answer: telling the user to accept licence terms would send them to the wrong fix.
                _mark_down()
                raise Unreachable(f"could not reach {API}") from e
            return LICENSE_REQUIRED
        if e.code == 429 or e.code in (408, 425) or 300 <= e.code < 400 or e.code >= 500:
            # Rate limiting, a timeout, a redirect loop or a CDN/reverse proxy saying 5xx is an
            # outage, not a licensing answer.
            _mark_down()
            raise Unreachable(f"could not reach {API}") from e
        return None
    except Exception as e:
        # http.client.HTTPException (truncated body, bad status line) is not an OSError.
        _mark_down()
        raise Unreachable(f"could not reach {API}") from e
    try:
        return json.loads(body)
    except ValueError as e:
        # A 200 carrying HTML (captive portal, WAF interstitial) is an outage, not an answer.
        _mark_down()
        raise Unreachable(f"could not read {API}'s answer") from e


def lookup_soft(repo_id):
    """lookup(), with an unreachable site reported as None. Other answers (including the licence
    sentinel) pass through, so callers that print their own message can still tell them apart."""
    try:
        return lookup(repo_id)
    except Unreachable:
        return None


def lookup_magnet(repo_id):
    """The magnet, NOT_LISTED, LICENSE_REQUIRED, or None for an answer we cannot use."""
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
        return _read_torrent(urllib.parse.urljoin(API, path), 30)
    except urllib.error.HTTPError as e:
        if e.code == 429 or e.code in (408, 425) or 300 <= e.code < 400 or e.code >= 500:
            # Don't cache a rate-limit blip as "no .torrent": the same latch lookup() uses.
            _mark_down()
            raise Unreachable(f"could not reach {API}") from e
        # 403/404/410 and friends are answers about this torrent; keep the "no .torrent" answer
        # that pf_only() relies on.
        return None
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLError):
            return None  # this request's handshake failed; lookups to the site can still work
        _mark_down()
        raise Unreachable(f"could not reach {API}") from e
    except Exception as e:
        # Don't cache "no .torrent" for the process: a blip would hide a PF-only repo for good.
        _mark_down()
        raise Unreachable(f"could not reach {API}") from e


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


def file_root(root):
    """One file's own swarm, by BEP 52 pieces root (GET /api/files/<root>): name, infohashV2, siblings
    (the listed uploads holding it) and the path of its .torrent. None if not listed or unreachable."""
    if time.monotonic() < _down_until:
        return None
    try:
        with urllib.request.urlopen(f"{API}/api/files/{root}", timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError:
        return None  # an answer about this file (404: not listed)
    except Exception:
        _mark_down()  # no answer: the next lookups fail fast for UNREACHABLE_FOR, as lookup() does
        return None


def download(path):
    """A .torrent on the site (path as the API gives it), or None."""
    try:
        return _read_torrent(urllib.parse.urljoin(API, path), 30)
    except Exception:
        return None


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


def submit(key, body, path="/api/community-torrents"):
    """POST a community torrent. Returns (HTTP status, JSON answer)."""
    req = urllib.request.Request(
        f"{API}{path}",
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


def upgrade(key, body):
    """POST a hybrid rebuild of a v1-only torrent ({from, torrent, manifest}). Returns (HTTP status, JSON answer)."""
    return submit(key, body, "/api/torrents/upgrade")


def torrent_status(infohash):
    """Pirate Face's record of a torrent by v1 infohash ({infohash, repo, status, supersededBy}), or None."""
    try:
        with urllib.request.urlopen(f"{API}/api/torrents/{infohash}", timeout=15) as r:
            return json.load(r)
    except Exception:
        return None


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
