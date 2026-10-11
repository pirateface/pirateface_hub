import contextvars
import functools
import hashlib
import inspect
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pirateface import torrent, ui

FUNC = "_download_to_tmp_and_move"
META = "_get_metadata_or_catch_error"
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
    if getattr(original, "_pirateface", False):
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

    patched._pirateface = True
    setattr(file_download, FUNC, patched)
    setattr(file_download, META, _metadata_from_pirateface(file_download, getattr(file_download, META)))
    _hide_token_nag()
    for name in OUTER:
        setattr(file_download, name, _remember_repo(getattr(file_download, name)))


def _metadata_from_pirateface(file_download, original):
    """Ask Pirate Face for a file's commit and hash first, Hugging Face only if it doesn't have them."""

    @functools.wraps(original)
    def patched(**kwargs):
        found = None
        if not kwargs["local_files_only"] and kwargs["repo_type"] in (None, "model"):
            found = torrent.listing(kwargs["repo_id"], kwargs["revision"])
        if found is None:
            return original(**kwargs)
        sha256 = found.files.get(kwargs["filename"])
        if sha256 is None:  # not in the torrent; keep the snapshot on one commit
            return original(**{**kwargs, "revision": found.revision})
        url = file_download.hf_hub_url(
            kwargs["repo_id"], kwargs["filename"], revision=found.revision, endpoint=kwargs["endpoint"]
        )
        return url, sha256, found.revision, None, None, None

    return patched


def install_snapshot(snapshot_module):
    """snapshot_download lists files with separate Hugging Face calls. If those fail, list from Pirate Face."""
    original = snapshot_module.snapshot_download
    if getattr(original, "_pirateface", False):
        return
    signature = inspect.signature(original)

    @functools.wraps(original)
    def patched(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except snapshot_module.LocalEntryNotFoundError:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            folder = _snapshot_from_pirateface(snapshot_module, **bound.arguments)
            if folder is None:
                raise
            return folder

    patched._pirateface = True
    snapshot_module.snapshot_download = patched


def _snapshot_from_pirateface(snapshot_module, *, repo_id, repo_type, revision, cache_dir, local_dir, **kw):
    from huggingface_hub import constants, file_download
    from huggingface_hub.utils import filter_repo_objects

    if kw["local_files_only"] or constants.HF_HUB_OFFLINE or kw["dry_run"] or local_dir is not None:
        return None
    if repo_type not in (None, "model") or (found := torrent.listing(repo_id, revision)) is None:
        return None
    files = filter_repo_objects(found.files, allow_patterns=kw["allow_patterns"], ignore_patterns=kw["ignore_patterns"])
    get = functools.partial(
        file_download.hf_hub_download,
        repo_id,
        revision=found.revision,
        cache_dir=cache_dir,
        force_download=kw["force_download"],
        token=kw["token"],
        headers=kw["headers"],
        endpoint=kw["endpoint"],
    )
    with ThreadPoolExecutor(kw["max_workers"]) as pool:
        list(pool.map(get, files))
    storage = Path(cache_dir or constants.HF_HUB_CACHE) / file_download.repo_folder_name(
        repo_id=repo_id, repo_type="model"
    )
    file_download._cache_commit_hash_for_specific_revision(str(storage), revision or "main", found.revision)
    return str(storage / "snapshots" / found.revision)


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
