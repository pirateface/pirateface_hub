"""Everything that talks to pirateface.co."""

import atexit
import contextlib
import functools
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

from pirateface.config import API, flag

NOT_LISTED = object()


@functools.cache
def lookup(repo_id):
    """Pirate Face's entry for the repo, NOT_LISTED, or None if unknown (network error)."""
    url = f"{API}/api/torrents?" + urllib.parse.urlencode({"repo": repo_id})
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
def torrent_file(repo_id):
    """The .torrent's bytes if Pirate Face publishes one for the repo, else None."""
    entry = lookup(repo_id)
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
