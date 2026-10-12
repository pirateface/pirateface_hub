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
def _lookup(repo_id):
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
    entry = _lookup(repo_id)
    return entry.get("magnet") if isinstance(entry, dict) else entry


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
