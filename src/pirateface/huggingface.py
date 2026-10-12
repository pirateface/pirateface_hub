"""Swap huggingface_hub's file download for a torrent download, checked against Hugging Face's fingerprint."""

import contextvars
import functools
import hashlib
import inspect
import shutil
import sys
from pathlib import Path

from pirateface import ui
from pirateface.client import torrent

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
        try:
            if _from_torrent(name, bound["etag"], dest):
                return None
        except torrent.Unavailable as e:
            ui.from_hf(name, str(e) or None)
        except Exception as e:
            ui.from_hf(name, str(e) or type(e).__name__)
        return original(*args, **kwargs)

    setattr(file_download, FUNC, patched)
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
    src = torrent.fetch_file(repo_id, filename)
    if not _matches(src, etag):
        raise ValueError("torrent copy didn't match Hugging Face's fingerprint")
    _place(src, dest)
    ui.from_swarm(filename, src.stat().st_size)
    return True


def _matches(path, etag):
    """HF etags are SHA-256 for LFS files, git blob SHA-1 otherwise."""
    etag = (etag or "").removeprefix("W/").strip('"')
    if len(etag) == 64:
        h = hashlib.sha256()
    elif len(etag) == 40:
        h = hashlib.sha1(b"blob %d\0" % path.stat().st_size)
    else:
        return False
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest() == etag


def _place(src, dest):
    """Copy, not hard-link: HF makes its blobs read-only, which would lock libtorrent out of a shared inode."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".pf.incomplete")
    try:
        shutil.copyfile(src, tmp)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
