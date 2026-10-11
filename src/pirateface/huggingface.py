"""Everything we change inside huggingface_hub."""

import contextvars
import functools
import hashlib
import inspect
import shutil
import sys
from pathlib import Path

from pirateface import ui
from pirateface.client import api, torrent

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
    setattr(file_download, META, _metadata_fallback(file_download, getattr(file_download, META)))
    _hide_token_nag()
    for name in OUTER:
        setattr(file_download, name, _remember_repo(getattr(file_download, name)))


def _unreachable(error):
    """The errors huggingface_hub itself retries: no connection, timeouts, 429 and 5xx."""
    from huggingface_hub import file_download as fd

    return isinstance(error, fd._DEFAULT_RETRY_ON_EXCEPTIONS) or (
        isinstance(error, fd.HfHubHTTPError) and error.response.status_code in fd._DEFAULT_RETRY_ON_STATUS_CODES
    )


def _metadata_fallback(file_download, original):
    """When Hugging Face can't be reached, take the file's commit and hash from Pirate Face."""

    @functools.wraps(original)
    def patched(**kwargs):
        result = original(**kwargs)
        if not _unreachable(result[-1]) or kwargs["repo_type"] not in (None, "model"):
            return result
        found = api.listing(kwargs["repo_id"], kwargs["revision"])
        etag = found and found.files.get(kwargs["filename"])
        if not etag:
            return result
        url = file_download.hf_hub_url(
            kwargs["repo_id"], kwargs["filename"], revision=found.revision, endpoint=kwargs["endpoint"]
        )
        return url, etag, found.revision, None, None, None

    return patched


def install_snapshot(snapshot_module):
    """When Hugging Face can't be reached, snapshot_download lists the files from Pirate Face."""
    original = snapshot_module.snapshot_download
    signature = inspect.signature(original)

    @functools.wraps(original)
    def patched(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except snapshot_module.LocalEntryNotFoundError as e:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            folder = _unreachable(e.__cause__) and _snapshot_from_pirateface(**bound.arguments)
            if not folder:
                raise
            return folder

    snapshot_module.snapshot_download = patched


def _snapshot_from_pirateface(*, repo_id, repo_type, revision, cache_dir, local_dir, **kw):
    from huggingface_hub import constants, file_download
    from huggingface_hub.utils import filter_repo_objects

    if repo_type not in (None, "model") or (found := api.listing(repo_id, revision)) is None:
        return None
    for filename in filter_repo_objects(
        found.files, allow_patterns=kw["allow_patterns"], ignore_patterns=kw["ignore_patterns"]
    ):
        file_download.hf_hub_download(
            repo_id,
            filename,
            revision=found.revision,
            cache_dir=cache_dir,
            local_dir=local_dir,
            force_download=kw["force_download"],
            token=kw["token"],
            headers=kw["headers"],
            endpoint=kw["endpoint"],
        )
    if local_dir is not None:
        return str(Path(local_dir).resolve())
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
