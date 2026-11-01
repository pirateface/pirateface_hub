"""Swap huggingface_hub's file download for a torrent download, checked against Hugging Face's fingerprint."""

import contextvars
import functools
import hashlib
import inspect
import re
import shutil
import sys
import urllib.parse
from pathlib import Path

from pirateface import config, ui
from pirateface.client import api, torrent

FUNC = "_download_to_tmp_and_move"
# FUNC isn't passed the repo id, so the outer functions record it here.
OUTER = ("_hf_hub_download_to_cache_dir", "_hf_hub_download_to_local_dir")
# Hugging Face API paths answered for repos only Pirate Face has (pirateface/ is answered by Pirate Face first).
INFO = re.compile(r"/api/models/([\w.-]+/[\w.-]+)(?:/revision/([^/]+))?")
TREE = re.compile(r"/api/models/([\w.-]+/[\w.-]+)/tree/([^/]+)(?:/(.+))?")
RESOLVE = re.compile(r"/([\w.-]+/[\w.-]+)/resolve/([^/]+)/(.+)")
PF_NAMESPACE = "pirateface"
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
    _install_transport()
    _hide_token_nag()
    for name in OUTER:
        setattr(file_download, name, _remember_repo(getattr(file_download, name)))


def _install_transport():
    """Answer for repos only Pirate Face has, in huggingface_hub's HTTP client."""
    try:
        from huggingface_hub.utils import _http

        _wrap_transport(_http)
    except (ImportError, AttributeError) as e:
        log(f"Pirate Face transport disabled: {e}")


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
    pf_only = api.pf_only(repo_id) is not None
    src = torrent.fetch_file(repo_id, filename, config.PF_ONLY_STALL_TIMEOUT if pf_only else config.STALL_TIMEOUT)
    if not _matches(src, etag):
        raise ValueError("torrent copy didn't match Hugging Face's fingerprint")
    _place(src, dest)
    ui.from_swarm(filename, src.stat().st_size, "Pirate Face" if pf_only else "Hugging Face")
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


def _wrap_transport(http):
    import httpx2
    from huggingface_hub import constants

    class Transport(httpx2.BaseTransport):
        def __init__(self, inner):
            self.inner, self.host = inner, urllib.parse.urlsplit(constants.ENDPOINT).hostname

        def handle_request(self, request):
            path = urllib.parse.unquote(request.url.path) if request.url.host == self.host else ""
            if path and (answered := self._answer(request, path, PF_NAMESPACE)):
                return answered
            response = self.inner.handle_request(request)
            # Hugging Face says "repository not found" as a 401 (no token) or 404 RepositoryNotFound (with one).
            missing = response.status_code == 401 or response.headers.get("X-Error-Code") == "RepositoryNotFound"
            if path and missing and (answered := self._answer(request, path)):
                response.close()
                return answered
            return response

        def _answer(self, request, path, namespace=None):
            for pattern, answer in ((TREE, _tree), (INFO, _info), (RESOLVE, _resolve)):
                m = pattern.fullmatch(path)
                if (
                    m
                    and (namespace is None or m[1].split("/")[0].lower() == namespace)
                    and (found := api.pf_only(m[1]))
                ):
                    status, headers, body = answer(found, request.method, *m.groups()[1:])
                    if body is None:
                        return httpx2.Response(status, headers=headers, request=request)
                    return httpx2.Response(status, headers=headers, json=body, request=request)
            return None

        def close(self):
            self.inner.close()

    make = http._GLOBAL_CLIENT_FACTORY

    def factory():
        client = make()
        client._transport = Transport(client._transport)
        return client

    http.set_client_factory(factory)


def _known(found, revision):
    return revision in (None, "main", found["commit"])


def _not_found(code, message):
    return 404, {"X-Error-Code": code, "X-Error-Message": message}, {"error": message}


def _info(found, _method, revision=None):
    if not _known(found, revision):
        return _not_found("RevisionNotFound", f"Invalid rev id: {revision}")
    siblings = [{"rfilename": p, "size": f[0]} for p, f in found["files"].items()]
    repo_id = found["repo_id"]
    info = {"id": repo_id, "modelId": repo_id, "author": repo_id.split("/")[0], "sha": found["commit"]}
    return 200, {}, info | {"private": False, "disabled": False, "gated": False, "siblings": siblings}


def _tree(found, _method, revision, folder=None):
    if not _known(found, revision):
        return _not_found("RevisionNotFound", f"Invalid rev id: {revision}")
    prefix = f"{folder.strip('/')}/" if folder else ""
    return (
        200,
        {},
        [
            {
                "type": "file",
                "path": path,
                "size": size,
                "oid": hashlib.sha1(sha256.encode()).hexdigest(),  # git blob id stand-in: only a cache key
                "lfs": {"oid": sha256, "size": size, "pointerSize": 134},
            }
            for path, (size, sha256, _) in found["files"].items()
            if path.startswith(prefix)
        ],
    )


def _resolve(found, method, revision, filename):
    if not _known(found, revision):
        return _not_found("RevisionNotFound", f"Invalid rev id: {revision}")
    if filename not in found["files"]:
        return _not_found("EntryNotFound", f"{filename} is not in this model")
    size, sha256, url = found["files"][filename]
    headers = {
        "X-Repo-Commit": found["commit"],
        "X-Linked-Etag": f'"{sha256}"',
        "X-Linked-Size": str(size),
        "ETag": f'"{sha256}"',
    }
    if url:  # same shape as Hugging Face's LFS answer: a redirect to where the bytes are
        return 302, headers | {"Location": url}, None
    if method == "HEAD":
        return 200, headers | {"Content-Length": str(size)}, None
    return _not_found("EntryNotFound", f"{filename} is only available over BitTorrent")
