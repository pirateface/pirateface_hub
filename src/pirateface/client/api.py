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
    """The model's commit and files {path: (size, sha256, url)} if it is only on Pirate Face, else None."""
    e = lookup(repo_id)
    if not (isinstance(e, dict) and e.get("hf") is False):
        return None
    data = torrent_file(repo_id)
    if not data:
        return None
    import libtorrent as lt

    torrent = lt.bdecode(data)
    info = torrent[b"info"]
    name = info[b"name"].decode()
    if b"files" in info:  # multi-file: <name>/<path>; BEP 47 pad files left out
        listed = [
            ("/".join(p.decode() for p in f[b"path"]), f[b"length"])
            for f in info[b"files"]
            if b"p" not in f.get(b"attr", b"")
        ]
        listed = [(path, size, f"{name}/{path}") for path, size in listed]
    else:
        listed = [(name, info[b"length"], name)]
    seeds = torrent.get(b"url-list") or []
    seed = (seeds if isinstance(seeds, bytes) else seeds[0] if seeds else b"").decode()
    sha = {f["path"]: f["sha256"] for f in e.get("files") or []}
    files = {}
    for path, size, seed_path in listed:
        if path in sha:
            # BEP 19: a base ending in "/" gets <torrent name>/<path> appended; otherwise it is the file itself.
            url = (seed + urllib.parse.quote(seed_path) if seed.endswith("/") else seed) or None
            files[path] = (size, sha[path], url)
    return {"repo_id": e["repoId"], "commit": e["revision"], "files": files} if files else None


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
