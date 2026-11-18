"""Everything that talks to pirateface.co."""

import atexit
import contextlib
import enum
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


class Answer(enum.Enum):
    """What the site said about a repo when it had no listing to give."""

    NOT_LISTED = "not listed"  # a 404
    LICENSE_REQUIRED = "licence terms to accept first"  # the site's own JSON 401/403
    UNUSABLE = "an answer this client cannot use"  # any other 4xx, or JSON that isn't an entry


NOT_LISTED, LICENSE_REQUIRED, UNUSABLE = Answer

# One dead lookup per half-minute, not one per file: an outage costs 10s once, then fails fast.
UNREACHABLE_FOR = 30.0
_down_until = 0.0

# The site takes .torrent uploads up to 3 MB; the copy it serves adds its trackers and web seeds.
MAX_TORRENT_BYTES = 4_000_000


class Unreachable(Exception):
    """pirateface.co didn't answer: distinct from "this repo isn't listed"."""


def _mark_down():
    global _down_until
    _down_until = time.monotonic() + UNREACHABLE_FOR


def request(url, timeout, *, method=None, headers=None, data=None):
    """One way to talk to the site: every request carries `User-Agent: pirateface/<version>`."""
    req = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": f"pirateface/{VERSION}", **(headers or {})},
        method=method,
    )
    return urllib.request.urlopen(req, timeout=timeout)


def _site_answer(fetch):
    """The memo policy for site answers: an answer is kept for the process, an outage never is. An outage
    (fetch raising Unreachable) starts the UNREACHABLE_FOR latch, during which uncached calls fail fast."""
    answers = {}

    @functools.wraps(fetch)
    def answer(*args):
        if args not in answers:
            if time.monotonic() < _down_until:
                raise Unreachable(f"could not reach {API}")
            try:
                answers[args] = fetch(*args)
            except Unreachable:
                _mark_down()
                raise
        return answers[args]

    return answer


def _get(url, timeout, limit=None, headers=None):
    """(HTTP status, body) of the site's answer at url; an error answer's body is b"" if it can't be read.

    Raises Unreachable when there is no answer: no connection, or rate limiting, a timeout, a redirect loop
    or a 5xx from a CDN or reverse proxy in front of the site."""
    try:
        with request(url, timeout, headers=headers) as r:
            return r.status, r.read(limit)
    except urllib.error.HTTPError as e:
        if e.code in (408, 425, 429) or 300 <= e.code < 400 or e.code >= 500:
            raise Unreachable(f"could not reach {API}") from e
        error = e
    except Exception as e:  # http.client.HTTPException (truncated body, bad status line) is not an OSError
        raise Unreachable(f"could not reach {API}") from e
    with contextlib.suppress(Exception):
        return error.code, error.read(limit)
    return error.code, b""


def _get_json(path, timeout, key=None, parse=json.loads):
    """parse(body) of the site's 2xx answer at path, sent as this account when key is given; None for no answer,
    an error status, or a body parse() rejects."""
    headers = None if key is None else {"Authorization": f"Bearer {key}"}
    with contextlib.suppress(Exception):
        status, body = _get(f"{API}{path}", timeout, headers=headers)
        return parse(body) if status < 400 else None
    return None


def _torrent(path):
    """The .torrent at path (as the API gives it), or None: an answer that there is none (403/404/410 and
    friends) or one over MAX_TORRENT_BYTES. Raises Unreachable."""
    status, data = _get(urllib.parse.urljoin(API, path), 30, MAX_TORRENT_BYTES + 1)
    return data if status < 400 and len(data) <= MAX_TORRENT_BYTES else None


def search_models(query):
    """Pirate Face's terminal model search (/s/<query>), as text lines, or None on failure."""
    path = urllib.parse.quote(query, safe="").replace("%20", "+")
    try:
        with request(f"{API}/s/{path}", 30) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


@_site_answer
def lookup(repo_id, quant=None):
    """Pirate Face's entry (a dict) for the repo, or an Answer: NOT_LISTED, LICENSE_REQUIRED or UNUSABLE.
    Raises Unreachable when the site cannot be reached at all, so an outage is never "not on Pirate Face".
    quant: one of entry["quants"][i]["quant"], when the revision has a torrent per quant."""
    params = {"repo": repo_id, **({"quant": quant} if quant else {})}
    status, body = _get(f"{API}/api/torrents?" + urllib.parse.urlencode(params), 10)
    if status == 404:
        return NOT_LISTED
    if status >= 400 and status not in (401, 403):
        return UNUSABLE
    try:
        entry = json.loads(body)
    except ValueError as e:
        # HTML from a captive portal, proxy or WAF in front of the site is an outage, not an answer: an HTML
        # 401/403 must not send the user to accept licence terms for the wrong fix.
        raise Unreachable(f"could not read {API}'s answer") from e
    if status in (401, 403):
        return LICENSE_REQUIRED
    return entry if isinstance(entry, dict) else UNUSABLE


def lookup_soft(repo_id):
    """lookup(), with no usable answer (an unreachable site or UNUSABLE) reported as None. The other answers
    pass through, so callers that print their own message can still tell them apart."""
    try:
        entry = lookup(repo_id)
    except Unreachable:
        return None
    return None if entry is UNUSABLE else entry


@_site_answer
def torrent_file(repo_id, quant=None):
    """The .torrent's bytes if Pirate Face publishes one for the repo (or that quant), else None."""
    entry = lookup(repo_id, quant)
    path = entry.get("torrent") if isinstance(entry, dict) else None
    if not path:
        return None
    try:
        return _torrent(path)  # "no .torrent" is an answer pf_only() relies on; an outage is never kept
    except Unreachable as e:
        if isinstance(getattr(e.__cause__, "reason", None), ssl.SSLError):
            return None  # this request's handshake failed; lookups to the site can still work
        raise


def pf_only(repo_id):
    """listed(repo_id) if only Pirate Face has the repo, else None."""
    entry = lookup(repo_id)
    return listed(repo_id) if isinstance(entry, dict) and entry.get("hf") is False else None


@functools.cache
def listed(repo_id):
    """The listed commit and files {path: (size, sha256, url)}: the torrent's files the listing has a sha256
    for. None if the repo isn't listed, has no .torrent (a licence-gated listing carries none) or no sha256s."""
    entry = lookup(repo_id)
    if not isinstance(entry, dict):
        return None
    data = torrent_file(repo_id)
    if not data:
        return None
    from pirateface.client import torrent  # torrent imports api

    sha = {f["path"]: f["sha256"] for f in entry.get("files") or []}
    try:
        files = {path: (size, sha[path], url) for path, size, url in torrent.files_in(data) if path in sha}
    except torrent.Unsafe:
        return None  # a listing the client won't write: same answer as "not only on Pirate Face"
    found = {"repo_id": entry["repoId"], "commit": entry["revision"], "files": files}
    return found | {"trust": "Pirate Face's listing"} if files else None


def file_root(root):
    """One file's own swarm, by BEP 52 pieces root (GET /api/files/<root>): name, infohashV2, siblings
    (the listed uploads holding it) and the path of its .torrent. None if not listed or unreachable."""
    if time.monotonic() < _down_until:
        return None
    try:
        with request(f"{API}/api/files/{root}", 15) as r:
            return json.load(r)
    except urllib.error.HTTPError:
        return None  # an answer about this file (404: not listed)
    except Exception:
        _mark_down()  # no answer: the next lookups fail fast for UNREACHABLE_FOR, as lookup() does
        return None


def download(path):
    """A .torrent on the site (path as the API gives it), or None."""
    with contextlib.suppress(Exception):
        return _torrent(path)
    return None


# Fallback when the site doesn't answer; normally replaced by /api/trackers.
TRACKERS = ["udp://tracker.pirateface.co:6969/announce", "http://tracker.pirateface.co:6969/announce"]


def trackers(key):
    """Public trackers plus, last, this account's seeding tracker (from the site, so changes need no release)."""
    lines = _get_json("/api/trackers", 15, key, parse=lambda body: body.decode().split()) or []
    return [t for t in lines if t.startswith(("udp://", "http://", "https://"))] or TRACKERS


@functools.cache
def licenses():
    """The licenses Pirate Face lists, and repos approved one by one."""
    with request(f"{API}/api/licenses", 30) as r:
        return json.load(r)


VERSION = version("pirateface")


def min_client_version():
    """The oldest pirateface Pirate Face takes submissions from (GET /api/cli), or None if it doesn't say."""
    answer = _get_json("/api/cli", 15)  # None from an older site without /api/cli, or offline
    minimum = answer.get("minVersion") if isinstance(answer, dict) else None
    return minimum if isinstance(minimum, str) and re.fullmatch(r"\d+(\.\d+)*", minimum) else None


def _release(v):
    return [int(x) for x in re.match(r"\d+(?:\.\d+)*", v).group().split(".")]


def older(a, b):
    """a < b, comparing release numbers like 0.1.2 (a suffix such as .dev1 is ignored)."""
    return _release(a) < _release(b)


def post(key, path, data, timeout, content_type="application/json"):
    """POST data to the site as this account. Returns (HTTP status, JSON answer, or None for an empty body); an
    error status comes back with the site's JSON or {"error": reason}. No answer at all raises."""
    headers = {"Authorization": f"Bearer {key}", "Content-Type": content_type}
    try:
        with request(f"{API}{path}", timeout, method="POST", headers=headers, data=data) as r:
            body = r.read()
            return r.status, json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {"error": e.reason}


def submit(key, body, path="/api/community-torrents"):
    """POST a community torrent. Returns (HTTP status, JSON answer)."""
    return post(key, path, json.dumps(body).encode(), 120)


def upgrade(key, body):
    """POST a hybrid rebuild of a v1-only torrent ({from, torrent, manifest}). Returns (HTTP status, JSON answer)."""
    return submit(key, body, "/api/torrents/upgrade")


def torrent_status(infohash):
    """Pirate Face's record of a torrent by v1 infohash ({infohash, repo, status, supersededBy}), or None."""
    return _get_json(f"/api/torrents/{infohash}", 15)


def my_torrents(key):
    """This account's listed v1-only torrents as [{repo, infohash}], for pf seed --upgrade --mine.
    None if the site did not answer (key rejected or unreachable)."""
    answer = _get_json("/api/torrents?mine=1", 30, key)
    return answer.get("torrents") if isinstance(answer, dict) else None


def restore(key, data):
    """POST raw .torrent bytes to /api/torrents/restore. Returns (HTTP status, JSON answer)."""
    return post(key, "/api/torrents/restore", data, 60, "application/x-bittorrent")


_rescues = []


def _send_rescue(repo_id):
    with contextlib.suppress(Exception):
        request(
            f"{API}/api/rescue",
            10,
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"repoId": repo_id}).encode(),
        ).close()


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
