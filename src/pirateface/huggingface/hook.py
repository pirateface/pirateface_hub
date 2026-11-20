"""Swap huggingface_hub's file download for a torrent download, checked against Hugging Face's fingerprint."""

import contextlib
import contextvars
import functools
import hashlib
import inspect
import re
import sys
from pathlib import Path

from pirateface import config, ui
from pirateface.client import api, torrent
from pirateface.huggingface import transport
from pirateface.index import mirror

FUNC = "_download_to_tmp_and_move"
# FUNC isn't passed the repo id, so the outer functions record it here.
OUTER = ("_hf_hub_download_to_cache_dir", "_hf_hub_download_to_local_dir")
_repo = contextvars.ContextVar("pirateface_repo", default=None)


def log(msg):
    print(f"[pirateface] {msg}", file=sys.stderr, flush=True)


def install(file_download):
    original = getattr(file_download, FUNC, None)
    if original is None:
        log(f"huggingface_hub has no {FUNC}; torrent downloads disabled")
        return

    signature = inspect.signature(original)

    @functools.wraps(original)
    def patched(*args, **kwargs):
        bound = signature.bind(*args, **kwargs).arguments
        dest = Path(bound["destination_path"])
        if dest.exists() and not bound.get("force_download"):
            return original(*args, **kwargs)
        name = bound["filename"]
        if config.flag("PF_HF_FIRST"):
            try:
                return original(*args, **kwargs)
            except Exception:
                log(f"{name}: Hugging Face failed; the file is coming from the swarm")
                with contextlib.suppress(Exception):
                    if _from_torrent(name, bound["etag"], dest):
                        return None
                raise
        try:
            if _from_torrent(name, bound["etag"], dest):
                return None
        except torrent.Unavailable as e:
            ui.from_hf(name, str(e) or None)
        except Exception as e:
            ui.from_hf(name, str(e) or type(e).__name__)
        return original(*args, **kwargs)

    setattr(file_download, FUNC, patched)
    transport.install()
    _hide_token_nag()
    for name in OUTER:
        setattr(file_download, name, _remember_repo(getattr(file_download, name)))


def _hide_token_nag():
    """Silence the "set a HF_TOKEN" server warning only; other server warnings still show."""
    try:
        from huggingface_hub.utils import _http

        _http._WARNED_TOPICS.add("unauthenticated")
    except (ImportError, AttributeError):
        pass


def _remember_repo(outer):
    @functools.wraps(outer)
    def wrapper(*args, **kwargs):
        token = _repo.set((kwargs.get("repo_type"), kwargs.get("repo_id")))
        try:
            return outer(*args, **kwargs)
        finally:
            _repo.reset(token)

    return wrapper


def _from_torrent(filename, etag, dest):
    repo_type, repo_id = _repo.get() or (None, None)
    if repo_id is None or repo_type not in (None, "model"):
        return False
    # Set when Pirate Face answered for Hugging Face: the etag is then the listing's sha256 (or the cached
    # index's stand-in), and there is no Hugging Face to finish the file if the swarm stalls.
    answered = transport.ANSWERED.get((repo_id, filename))
    try:
        pf_only = api.pf_only(repo_id) is not None or answered is not None
    except api.Unreadable:
        # The site answered and we could not read it. Say so once per repo, then try the cached index
        # and, failing that, let Hugging Face serve the file.
        transport.report_unreadable(repo_id, "trying the cached index")
        return _from_index(repo_id, filename, dest, etag, answered)
    except api.Unreachable:
        # The site is down. The cached index may still name this repo's swarm, so try that before
        # handing the file to Hugging Face.
        return _from_index(repo_id, filename, dest, etag, answered)
    src = torrent.fetch_file(repo_id, filename, config.PF_ONLY_STALL_TIMEOUT if pf_only else config.STALL_TIMEOUT)
    if not _place_checked(src, dest, etag):
        raise ValueError("torrent copy didn't match Hugging Face's fingerprint")
    ui.from_swarm(filename, src.stat().st_size, "Pirate Face" if pf_only else "Hugging Face")
    return True


def _from_index(repo_id, filename, dest, etag, answered=None):
    """Fetch the file from the swarm the cached index names, when Pirate Face cannot be reached.

    The check that decides is the Hugging Face fingerprint the download already holds: it is what ties
    these bytes to a repo and a revision. _place_checked writes the copy beside `dest`, compares that
    fingerprint, and only replaces `dest` when it matches. Without a fingerprint there is nothing to
    compare, so such a download is left to Hugging Face.

    `answered`: the cached index's own listing stood in for Hugging Face, so there is no fingerprint, only a
    cache key. The check is then the torrent's piece hashes, which the index's infohash commits to.
    """
    entry = mirror.resolve(repo_id)
    if entry is None:
        raise torrent.Unavailable(
            "the cached index lost a removals list, so it is not being used"
            if mirror.broken()
            else "not in the cached index"
        )
    torrent_checked = answered is not None and answered["files"][filename][1] is None
    # Checked before the download, and on the shape _place_checked can compare -- it compares against
    # hexdigest(), which is lowercase, so normalise here rather than fetching a file that cannot match.
    etag = (etag or "").removeprefix("W/").strip('"').lower()
    if not torrent_checked and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", etag):
        raise torrent.Unavailable("Hugging Face gave no fingerprint to check the index's copy against")
    try:
        # Hugging Face can take the file if the swarm stalls, unless the index stood in for it.
        stall = config.PF_ONLY_STALL_TIMEOUT if answered else config.STALL_TIMEOUT
        src = torrent.fetch_file(repo_id, filename, stall, magnet=entry.magnet)
    except torrent.NotInTorrent:
        # The repo is in the index but its torrent carries only part of it: say so, before Hugging Face
        # ends up filling the rest.
        raise torrent.Unavailable(
            f"the cached index's copy of {repo_id} (build {entry.build}) does not have this file"
        ) from None
    if not _place_checked(src, dest, etag, torrent_checked):
        raise torrent.Unavailable("the cached index's copy is not the revision Hugging Face named")
    checked_by = "the torrent's own hashes" if torrent_checked else "Hugging Face"
    ui.from_index(filename, src.stat().st_size, entry.build, checked_by)
    return True


def _place_checked(src, dest, etag, torrent_checked=False):
    """Copy src to dest, hashing it on the way, and move it into place only if the fingerprint matches:
    one read of the file instead of one to check and one to copy. HF etags are SHA-256 for LFS files, git blob
    SHA-1 otherwise. Copy, not hard-link: HF makes its blobs read-only, which would lock libtorrent out of a
    shared inode.

    torrent_checked: there is no fingerprint (the cached index stood in for Hugging Face), and libtorrent counting
    a piece only once it matches the torrent's hash is the check, so the bytes are copied as they are."""
    etag = (etag or "").removeprefix("W/").strip('"')
    if torrent_checked:
        h = None
    elif len(etag) == 64:
        h = hashlib.sha256()
    elif len(etag) == 40:
        h = hashlib.sha1(b"blob %d\0" % src.stat().st_size)
    else:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".pf.incomplete")
    try:
        with src.open("rb") as f, tmp.open("wb") as out:
            while chunk := f.read(1 << 20):
                if h is not None:
                    h.update(chunk)
                out.write(chunk)
        if h is not None and h.hexdigest() != etag:
            return False
        tmp.replace(dest)
        return True
    finally:
        tmp.unlink(missing_ok=True)
