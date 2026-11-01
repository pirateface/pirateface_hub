"""Models that are only on Pirate Face (never on Hugging Face, or removed from it) still load through huggingface_hub.

When Hugging Face answers "repository not found", ask Pirate Face. If it lists the model with hf: false, answer
huggingface_hub's request from Pirate Face instead: model info, the file tree, and each file's fingerprint
(SHA-256) and size. Sizes and web seeds come from the published .torrent. The torrent hook then downloads the
files; plain HTTP follows the redirect to the web seed when there is one.
"""

import functools
import hashlib
import re
import urllib.parse

from pirateface.client import api

INFO = re.compile(r"/api/models/([\w.-]+/[\w.-]+)(?:/revision/([^/]+))?")
TREE = re.compile(r"/api/models/([\w.-]+/[\w.-]+)/tree/([^/]+)(?:/.*)?")
RESOLVE = re.compile(r"/([\w.-]+/[\w.-]+)/resolve/([^/]+)/(.+)")


@functools.cache
def model(repo_id):
    """{"repo_id", "commit", "files": {path: (size, sha256, url or None)}} if the model is only on Pirate Face, else None."""
    e = api._lookup(repo_id)
    if not (isinstance(e, dict) and e.get("hf") is False):
        return None
    data = api.torrent_file(repo_id)
    if not data:
        return None
    import libtorrent as lt

    ti = lt.torrent_info(lt.bdecode(data))
    fs = ti.files()
    multi = fs.num_files() > 1
    seeds = [s["url"] for s in ti.web_seeds()]
    sha = {f["path"]: f["sha256"] for f in e.get("files") or []}
    files = {}
    for i in range(fs.num_files()):
        if fs.file_flags(i) & fs.flag_pad_file:
            continue
        torrent_path = fs.file_path(i).replace("\\", "/")
        path = torrent_path.split("/", 1)[1] if multi else torrent_path
        if path in sha:
            # BEP 19: a base ending in "/" gets <torrent name>/<path> appended; otherwise it is the file itself.
            url = None
            if seeds:
                url = seeds[0] + urllib.parse.quote(torrent_path) if seeds[0].endswith("/") else seeds[0]
            files[path] = (fs.file_size(i), sha[path], url)
    return {"repo_id": e["repoId"], "commit": e["revision"], "files": files} if files else None


def install(http):
    """Wrap the transport of the clients huggingface_hub's client factory makes."""
    import httpx2
    from huggingface_hub import constants

    class Transport(httpx2.BaseTransport):
        def __init__(self, inner):
            self.inner, self.host = inner, urllib.parse.urlsplit(constants.ENDPOINT).hostname

        def handle_request(self, request):
            response = self.inner.handle_request(request)
            # Hugging Face says "repository not found" as a 401 (no token) or 404 RepositoryNotFound (with one).
            missing = response.status_code == 401 or response.headers.get("X-Error-Code") == "RepositoryNotFound"
            if request.url.host != self.host or not missing:
                return response
            path = urllib.parse.unquote(request.url.path)
            for pattern, answer in ((TREE, _tree), (INFO, _info), (RESOLVE, _resolve)):
                m = pattern.fullmatch(path)
                if m and (found := model(m[1])):
                    response.close()
                    status, headers, body = answer(found, request.method, *m.groups()[1:])
                    content = b"" if body is None else None
                    return httpx2.Response(status, headers=headers, json=body, content=content, request=request)
            return response

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


def _tree(found, _method, revision):
    if not _known(found, revision):
        return _not_found("RevisionNotFound", f"Invalid rev id: {revision}")
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
