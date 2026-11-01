"""Serve models that are only on Pirate Face through huggingface_hub, as if Hugging Face had them."""

import functools
import hashlib
import re
import urllib.parse

from pirateface.client import api

INFO = re.compile(r"/api/models/([\w.-]+/[\w.-]+)(?:/revision/([^/]+))?")
TREE = re.compile(r"/api/models/([\w.-]+/[\w.-]+)/tree/([^/]+)(?:/(.+))?")
RESOLVE = re.compile(r"/([\w.-]+/[\w.-]+)/resolve/([^/]+)/(.+)")
PF_NAMESPACE = "pirateface"


@functools.cache
def model(repo_id):
    """The model's commit and files {path: (size, sha256, url)} if it is only on Pirate Face, else None."""
    e = api.lookup(repo_id)
    if not (isinstance(e, dict) and e.get("hf") is False):
        return None
    data = api.torrent_file(repo_id)
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


def install(http):
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
                if m and (namespace is None or m[1].split("/")[0].lower() == namespace) and (found := model(m[1])):
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
