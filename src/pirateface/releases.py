"""Models Pirate Face publishes itself (not on Hugging Face) look like Hugging Face repos to huggingface_hub.

For repos in Pirate Face's namespace, huggingface_hub's requests to Hugging Face are answered from
Pirate Face's /api/torrents entry when it says hf: false: model info, the file tree, and each file's
fingerprint (SHA-256) with a redirect to Pirate Face's copy. Everything else goes to Hugging Face.
"""

import hashlib
import re
import urllib.parse

from pirateface.client import api

NAMESPACES = ("pirateface",)  # Pirate Face is the authority here, even if someone registers the name on HF
INFO = re.compile(r"/api/models/([\w.-]+/[\w.-]+)(?:/revision/([^/]+))?")
TREE = re.compile(r"/api/models/([\w.-]+/[\w.-]+)/tree/([^/]+)(?:/.*)?")
RESOLVE = re.compile(r"/([\w.-]+/[\w.-]+)/resolve/([^/]+)/(.+)")


def entry(repo_id):
    if repo_id.split("/")[0].lower() not in NAMESPACES:
        return None
    e = api._lookup(repo_id)
    return e if isinstance(e, dict) and e.get("hf") is False else None


def install(http):
    """Wrap the transport of every client huggingface_hub's client factory makes."""
    import httpx2

    class Transport(httpx2.BaseTransport):
        def __init__(self, inner, endpoint):
            self.inner, self.host = inner, urllib.parse.urlsplit(endpoint).hostname

        def handle_request(self, request):
            if request.url.host == self.host:
                path = urllib.parse.unquote(request.url.path)
                for pattern, answer in ((TREE, _tree), (INFO, _info), (RESOLVE, _resolve)):
                    m = pattern.fullmatch(path)
                    if m and (e := entry(m[1])):
                        status, headers, body = answer(e, *m.groups()[1:])
                        content = b"" if body is None else None
                        return httpx2.Response(status, headers=headers, json=body, content=content, request=request)
            return self.inner.handle_request(request)

        def close(self):
            self.inner.close()

    from huggingface_hub import constants

    make = http._GLOBAL_CLIENT_FACTORY

    def factory():
        client = make()
        client._transport = Transport(client._transport, constants.ENDPOINT)
        return client

    http.set_client_factory(factory)


def _commit(e, revision):
    """Pirate Face's revision as a commit hash (the infohash pins the exact files), or None if unknown."""
    commit = e["infohash"]
    return commit if revision in (None, "main", e["revision"], commit) else None


def _not_found(code, message):
    return 404, {"X-Error-Code": code, "X-Error-Message": message}, {"error": message}


def _info(e, revision=None):
    commit = _commit(e, revision)
    if not commit:
        return _not_found("RevisionNotFound", f"Invalid rev id: {revision}")
    org = e["repoId"].split("/")[0]
    files = [{"rfilename": f["path"], "size": f["size"]} for f in e["files"]]
    body = {"id": e["repoId"], "modelId": e["repoId"], "author": org, "sha": commit, "private": False}
    return (
        200,
        {},
        body | {"disabled": False, "gated": False, "siblings": files, "tags": [], "downloads": 0, "likes": 0},
    )


def _tree(e, revision):
    if not _commit(e, revision):
        return _not_found("RevisionNotFound", f"Invalid rev id: {revision}")
    return (
        200,
        {},
        [
            {
                "type": "file",
                "path": f["path"],
                "size": f["size"],
                "oid": hashlib.sha1(f["sha256"].encode()).hexdigest(),  # git blob id stand-in: a cache key only
                "lfs": {"oid": f["sha256"], "size": f["size"], "pointerSize": 134},
            }
            for f in e["files"]
        ],
    )


def _resolve(e, revision, filename):
    commit = _commit(e, revision)
    if not commit:
        return _not_found("RevisionNotFound", f"Invalid rev id: {revision}")
    f = next((f for f in e["files"] if f["path"] == filename), None)
    if f is None:
        return _not_found("EntryNotFound", f"{filename} is not in {e['repoId']}")
    # Same headers as Hugging Face's LFS answer: the torrent hook downloads it; plain HTTP follows Location.
    headers = {
        "Location": f["url"],
        "X-Repo-Commit": commit,
        "X-Linked-Etag": f'"{f["sha256"]}"',
        "X-Linked-Size": str(f["size"]),
        "ETag": f'"{f["sha256"]}"',
    }
    return 302, headers, None
