"""Swap huggingface_hub's byte-fetching step for a torrent download.

huggingface_hub still does everything else (metadata, cache layout, symlinks,
locks). We only replace `_download_to_tmp_and_move`: if Pirate Face has a
torrent for the repo and the torrent's copy matches Hugging Face's own
fingerprint (the etag), we use it. Anything else falls back to the original.
"""

import contextvars
import functools
import hashlib
import os
import shutil
import sys
from pathlib import Path

from pirateface import torrent, ui

FUNC = "_download_to_tmp_and_move"
# The byte-fetching step doesn't get the repo name, so we note it on the way in.
OUTER = ("_hf_hub_download_to_cache_dir", "_hf_hub_download_to_local_dir")
_repo = contextvars.ContextVar("pirateface_repo", default=None)


def log(msg):
    print(f"[pirateface] {msg}", file=sys.stderr, flush=True)


def install(file_download):
    original = getattr(file_download, FUNC, None)
    if original is None:
        log(f"huggingface_hub has no {FUNC}; torrent downloads disabled")
        return
    if getattr(original, "_pirateface", False):
        return

    @functools.wraps(original)
    def patched(*args, **kwargs):
        bound = dict(zip(original.__code__.co_varnames, args), **kwargs)
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

    patched._pirateface = True
    setattr(file_download, FUNC, patched)
    _hide_token_nag()
    for name in OUTER:
        setattr(file_download, name, _remember_repo(getattr(file_download, name)))


def _hide_token_nag():
    """Hugging Face's server sends "set a HF_TOKEN" as an X-HF-Warning header, shown once per topic.
    Mark that topic as already shown. Every other server warning still gets through."""
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
    if repo_id is None or repo_type not in (None, "model"):  # models only for now
        return False
    src = torrent.fetch_file(repo_id, filename)
    if not _matches(src, etag):
        raise ValueError("torrent copy didn't match Hugging Face's fingerprint")
    _place(src, dest)
    ui.from_swarm(filename, src.stat().st_size)
    return True


def _matches(path, etag):
    """Compare against Hugging Face's etag: SHA-256 for LFS files, git blob SHA-1 otherwise."""
    etag = (etag or "").strip('"').removeprefix("W/").strip('"')
    if len(etag) == 64:
        h = hashlib.sha256()
    elif len(etag) == 40:
        h = hashlib.sha1(b"blob %d\0" % path.stat().st_size)
    else:
        return False
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest() == etag


def _place(src, dest):
    """Copy, don't hard-link: huggingface_hub makes its blobs read-only, and with a shared inode that
    would lock libtorrent out of its own file. Our copy is deleted when the process exits."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".pf.incomplete")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)
