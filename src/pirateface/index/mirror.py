"""A cached copy of Pirate Face's magnet index: model id -> magnet, readable while the site is down.

The index is a torrent of weekly shards (``shards/YYYY-MM-DD.txt``, one bare magnet per line) plus, from
the first build that drops something, ``shards/removed-YYYY-MM-DD.txt`` with one v1 infohash per line.
pf mirrors those shards into PF_CACHE/index/shards/ and answers lookups from that copy when pirateface.co
cannot be reached, so a model can still be downloaded instead of falling back to Hugging Face.

A file fetched through this copy is checked by the hook against the Hugging Face fingerprint the download
already holds -- that is what ties the bytes to a repo and a revision. libtorrent hashes every piece before
it counts it downloaded, and the fingerprint is compared in full before the download replaces the cached
file; without a fingerprint the file is left to Hugging Face.
"""

import base64
import contextlib
import functools
import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from pirateface import config
from pirateface.client import api, torrent
from pirateface.index import FILE, INDEX_REPO

# The web seed in an index magnet is a URL libtorrent may fetch pieces from, so only the site's own (or
# the configured mirror's) host is accepted: a shard cannot point a lookup at somebody else's server.
WS_HOSTS = {"pirateface.co"}
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_HASH = re.compile(r"[0-9a-fA-F]{40}")
_COMMIT = re.compile(r"[0-9a-f]{40}")

# Carried in the package as the fallback for a copy that has never synced: the site names the current build,
# and this is the 2026-11-09 build for a client that has not asked yet.
BOOTSTRAP_MAGNET = (
    "magnet:?xt=urn:btih:5f47f5c8edd0ffde0142229bb1fbe111f33d773f"
    "&xt=urn:btmh:12207d3e6dfe08b37a6ea047fb9f4541b183fa95e18cd5660eb007f8ea47e4b4d663"
    "&tr=udp%3A%2F%2Ftracker.pirateface.co%3A6969%2Fannounce"
    "&tr=http%3A%2F%2Ftracker.pirateface.co%3A6969%2Fannounce"
)

DIR = Path(config.CACHE_DIR) / "index"
SHARDS = DIR / "shards"
STATE = DIR / "state.json"

_SHARD = re.compile(r"^(\d{4}-\d{2}-\d{2})\.txt$")
_REMOVED = re.compile(r"^removed-(\d{4}-\d{2}-\d{2})\.txt$")
_sync_lock = threading.Lock()


@dataclass(frozen=True)
class Entry:
    """What the index says about one repo: the magnet the swarm is joined with, and where it came from."""

    magnet: str
    infohash: str
    build: str  # the shard date this magnet was last written in
    commit: str | None  # from the web seed's path, when it names a full commit


def web_seed(magnet):
    """(repo id, commit or None) from a magnet's web seed (ws=https://pirateface.co/api/ws/OWNER/NAME/REV/), or None.

    The shards hold bare magnets with no repo name on them: the web seed's path is the only place the
    model id appears, so a magnet without one cannot be looked up and is skipped.

    A shard therefore decides which swarm a repo id resolves to, and the web seed in that magnet is one
    libtorrent may fetch pieces from. So the host must be the site's own (or the configured mirror's) and
    the name must be a plain owner/name: a shard cannot point a lookup at another server, at `..`, or at a
    truncated path.
    """
    try:
        ws = urllib.parse.parse_qs(urllib.parse.urlparse(magnet).query).get("ws") or []
        url = urllib.parse.urlparse(ws[0]) if ws else None
    except ValueError:
        return None  # a line urllib cannot parse (an unmatched "[" in the host, say) is skipped like any other
    if url is None or len(ws) > 1:
        return None  # libtorrent honours every ws= in a magnet; a second one would be an unchecked server
    if url.scheme not in ("http", "https") or url.hostname not in _ws_hosts():
        return None
    parts = [part for part in url.path.split("/") if part]
    if len(parts) < 4 or parts[0] != "api" or parts[1] != "ws":
        return None
    repo = f"{parts[2]}/{parts[3]}"
    commit = parts[4] if len(parts) > 4 and _COMMIT.fullmatch(parts[4]) else None
    return (repo, commit) if _REPO.fullmatch(repo) else None


def _ws_hosts():
    """pirateface.co, plus the host of PF_API so a self-hosted mirror's shards still map."""
    try:
        return WS_HOSTS | {urllib.parse.urlsplit(config.API).hostname or ""}
    except ValueError:
        return WS_HOSTS  # a malformed PF_API must not take the index down with it


def infohash_of(magnet):
    """The magnet's v1 infohash as lowercase hex (btih is hex or base32), or None."""
    try:
        xts = urllib.parse.parse_qs(urllib.parse.urlparse(magnet).query).get("xt", ())
    except ValueError:
        return None  # an unparseable line is skipped, not allowed to take the whole index down
    for xt in xts:
        digest = xt.removeprefix("urn:btih:")
        if digest == xt:
            continue
        if len(digest) == 32:
            with contextlib.suppress(ValueError):
                return base64.b32decode(digest.upper()).hex()
        elif len(digest) == 40:
            return digest.lower()
    return None


def dates():
    """Every build date in the cache, oldest first.

    A build that only drops models ships a removals file and no shard, so both names count: taking dates
    from the shards alone would ignore exactly the builds that publish removals and nothing else.
    """
    if not SHARDS.is_dir():
        return []
    found = set()
    try:
        entries = list(SHARDS.iterdir())
    except OSError:
        return []  # unreadable: answer nothing rather than raise out of every lookup
    for path in entries:
        match = _SHARD.match(path.name) or _REMOVED.match(path.name)
        if match:
            found.add(match[1])
    return sorted(found)


def build():
    """The newest build date in the cache: the build this copy of the index is. None before any sync."""
    known = dates()
    return known[-1] if known else None


@functools.cache
def _parsed():
    """({repo_id: Entry}, removals_lost): every build in date order, each date's removals applied right
    after that shard.

    A removal names an infohash, not a repo: it retires that torrent. A repo the site drops and later
    publishes again under a new infohash stays listed, which is what the 2026-11-16 build does with all
    224 hashes the 2026-11-09 build listed -- they are repacks, not models that must disappear.

    Order matters. A removal deletes what is known at that date, so a model dropped in one build and
    added back in a later one stays listed; applying every removal at the end would delete the re-add.
    Within one shard the last line wins, which is what the index page documents for a re-upload.

    A removals list is never traded away for anything. If one cannot be read, this copy of the index
    cannot say what is still allowed and answers nothing at all, so the files come from Hugging Face
    until a sync replaces the file -- the alternative is resurrecting a model the site removed. A shard
    that cannot be read only costs that build's additions, which is the safe direction, and its removals
    still apply. The two come back together so a reader can never see one without the other.
    """
    found = {}
    for date in dates():
        gone = _removed_hashes(SHARDS / f"removed-{date}.txt")
        if gone is None:
            return {}, True
        for line in _lines(SHARDS / f"{date}.txt") or []:
            magnet = line.strip()
            seed = web_seed(magnet) if magnet else None
            infohash = infohash_of(magnet) if seed else None
            if seed and infohash:
                found[seed[0]] = Entry(magnet, infohash, date, seed[1])
        for repo_id in [repo for repo, entry in found.items() if entry.infohash in gone]:
            del found[repo_id]
    return found, False


def broken():
    """True when a removals list could not be read: this copy answers nothing and Hugging Face gets every
    file, until a sync replaces the unreadable file."""
    return _parsed()[1]


def _removed_hashes(path):
    """The infohashes a removals file names (empty when there is none), or None when it cannot be trusted.

    Every non-blank line must be exactly 40 hex characters. A stray character makes the whole file
    unreadable instead of yielding a different hash: searching each line for a hex run shifts the window,
    so `9` followed by a hash reads as a hash that names nothing and the removal is quietly missed.
    """
    lines = _lines(path)
    if lines is None:
        return None
    found = set()
    for line in lines:
        text = line.strip()
        if not text:
            continue
        if not _HASH.fullmatch(text):
            return None
        found.add(text.lower())
    return found


def _lines(path):
    """A shard's lines, [] when there is no such file, None when it exists but cannot be read.

    One unreadable or half-written file must not take the whole index down -- a missing entry only costs a
    fallback to Hugging Face, while a raised exception would cost every entry -- but it must not be read as
    an empty file either: that would apply a build without its removals.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        return None


def count():
    """How many repos the cached index lists."""
    return len(_parsed()[0])


def resolve(repo_id):
    """The cached index's Entry for the repo, or None when this copy of the index does not list it."""
    return _parsed()[0].get(repo_id)


def listing(repo_id):
    """The repo as the cached index names it, in api.listed()'s shape, or None: the commit is the one in the
    magnet's web seed (.../api/ws/OWNER/NAME/COMMIT/), the file list comes from the swarm.

    There is no sha256 per file (None): the magnet's infohash commits to every piece, and libtorrent counts a
    piece downloaded only once its hash matches, so the torrent's own hashes are the check.
    """
    entry = resolve(repo_id)
    if entry is None or entry.commit is None:
        return None
    try:
        files = torrent.swarm_files(repo_id, magnet=entry.magnet, where="the cached index")
    except (torrent.Unavailable, TimeoutError):
        return None
    return {
        "repo_id": repo_id,
        "commit": entry.commit,
        "files": {path: (size, None, None) for path, size in files},
        "trust": f"the cached index (build {entry.build}): infohash {entry.infohash}",
    }


def state():
    """What the last sync recorded: build date, magnet, shard names and when it ran. {} if unknown."""
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(**fields):
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(STATE.name + ".part")
    try:
        tmp.write_text(json.dumps(fields, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(STATE)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _digest(path):
    """sha256 of a cached file, or None if it cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _unchanged(path, size, recorded):
    """Whether the cached file is the copy this build names.

    The size alone misses a file rewritten at the same size, so a sync records each file's sha256 and
    compares that too. A copy mirrored before digests existed has none recorded: it is judged by size and
    has a digest recorded on the next sync, so a rewrite at that size is caught from then on. A file that
    cannot be read is never the copy this build names -- otherwise `pf index` tells the user to run a sync
    that would keep the very file it is complaining about.
    """
    try:
        if not path.is_file() or path.stat().st_size != size:
            return False
    except OSError:
        return False
    digest = _digest(path)
    if digest is None:
        return False  # unreadable: refetch it rather than trust the size
    return recorded is None or digest == recorded


def published():
    """The build the site says is current, as (magnet, infohash), or None when it does not answer.

    /api/torrents answers for the index now, so the site is the first place to look: a client asks once
    per sync and gets everything else over the torrent. The magnet carried in the package is only the
    starting point for a copy that has never synced.
    """
    try:
        entry = api.lookup(INDEX_REPO)
    except api.Unreachable:
        return None
    if not (isinstance(entry, dict) and entry.get("magnet")):
        return None
    return entry["magnet"], entry.get("infohash") or infohash_of(entry["magnet"])


def _remote_magnet():
    """The build to join: the site's, else the magnet from the last sync, else the one in the package."""
    found = published()
    if found:
        return found[0]
    return state().get("magnet") or BOOTSTRAP_MAGNET


def sync():
    """Mirror the index torrent's shards into PF_CACHE/index/shards/ and return the build date.

    Needs no API: the bootstrap magnet carries its own trackers. The shards are copied out of the
    torrent's folder because torrent._cleanup deletes that folder at exit.

    Each date's removals file is fetched before that date's shard, so a mirror that dies halfway leaves
    every shard it did write next to its removals. The other order would leave the models a build dropped
    listed -- and downloadable -- until someone noticed.

    A cached file is replaced when it is not the copy this build names -- wrong size, or (once a digest
    has been recorded) different bytes at the same size -- not merely when it is missing. That is how a
    copy that was truncated, corrupted or left unreadable gets healed, which is what `pf index` tells the
    user to do about it. The temp file carries the pid, so two processes syncing at once cannot share one.
    """
    with _sync_lock:
        magnet = _remote_magnet()
        files = []
        for path, size in torrent.swarm_files(INDEX_REPO, magnet=magnet, where="Pirate Face"):
            if not FILE.fullmatch(path):  # LICENSE and anything else a build carries are not lookups
                continue
            name = Path(path).name
            removed, shard = _REMOVED.match(name), _SHARD.match(name)
            if removed:
                files.append((removed[1], 0, path, size))  # removals first, for that date
            elif shard:
                files.append((shard[1], 1, path, size))
        SHARDS.mkdir(parents=True, exist_ok=True)
        digests = dict(state().get("digests") or {})
        try:
            for _, _, name, size in sorted(files):
                dest = SHARDS / Path(name).name
                if _unchanged(dest, size, digests.get(dest.name)):
                    # Judged by size alone: record a digest now, so a rewrite at that size is caught instead
                    # of kept for the life of the copy. Guarded rather than setdefault, which would hash the
                    # file again on every sync.
                    if dest.name not in digests:
                        digests[dest.name] = _digest(dest)
                    continue
                src = torrent.fetch_file(INDEX_REPO, name, magnet=magnet)
                tmp = dest.with_name(f"{dest.name}.{os.getpid()}.part")
                try:
                    shutil.copyfile(src, tmp)
                    tmp.replace(dest)
                except BaseException:
                    tmp.unlink(missing_ok=True)
                    raise
                digests[dest.name] = _digest(dest)
        finally:
            # However far it got, what resolve() reads must be what is on disk now.
            _parsed.cache_clear()
        date = build()
        shards = [path.name for path in SHARDS.iterdir() if _SHARD.match(path.name) or _REMOVED.match(path.name)]
        _write_state(
            build=date,
            magnet=magnet,
            shards=shards,
            digests={name: digests[name] for name in shards if digests.get(name)},
            synced=time.time(),
        )
        return date
