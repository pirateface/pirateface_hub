"""Keep downloading when Hugging Face can't be reached.

huggingface_hub asks Hugging Face for every file's commit and fingerprint before downloading, and
snapshot_download asks it for the file list. When Hugging Face is down or blocked, both fail. These
patches answer from Pirate Face instead (/api/torrents lists each file's SHA-256), so the torrent
download in huggingface.py can still run. With Hugging Face reachable, nothing here changes anything.
"""

import functools
import inspect
from pathlib import Path

from pirateface.client import api


def install(file_download):
    """File metadata: commit and fingerprint from Pirate Face."""
    original = file_download._get_metadata_or_catch_error

    @functools.wraps(original)
    def patched(**kwargs):
        result = original(**kwargs)
        if not _unreachable(result[-1]) or kwargs["repo_type"] not in (None, "model"):
            return result
        found = api.listing(kwargs["repo_id"], kwargs["revision"])
        sha256 = found and found.files.get(kwargs["filename"])
        if not sha256:
            return result
        url = file_download.hf_hub_url(
            kwargs["repo_id"], kwargs["filename"], revision=found.revision, endpoint=kwargs["endpoint"]
        )
        # The SHA-256 stands in for Hugging Face's etag. For small (non-LFS) files Hugging Face would use a
        # git SHA-1, so those land under a different blob name in the cache. huggingface._matches checks either.
        return url, sha256, found.revision, None, None, None

    file_download._get_metadata_or_catch_error = patched


def install_snapshot(snapshot_module):
    """snapshot_download: file list from Pirate Face."""
    original = snapshot_module.snapshot_download
    signature = inspect.signature(original)

    @functools.wraps(original)
    def patched(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except snapshot_module.LocalEntryNotFoundError as e:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            folder = _unreachable(e.__cause__) and _snapshot(**bound.arguments)
            if not folder:
                raise
            return folder

    snapshot_module.snapshot_download = patched


def _unreachable(error):
    """The errors huggingface_hub itself retries: no connection, timeouts, 429 and 5xx."""
    from huggingface_hub import file_download as fd

    return isinstance(error, fd._DEFAULT_RETRY_ON_EXCEPTIONS) or (
        isinstance(error, fd.HfHubHTTPError) and error.response.status_code in fd._DEFAULT_RETRY_ON_STATUS_CODES
    )


def _snapshot(*, repo_id, repo_type, revision, cache_dir, local_dir, **kw):
    """Download every file Pirate Face lists, one hf_hub_download each, and return the folder."""
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
