"""Hugging Face API answers for repos only Pirate Face has: model info, file tree, file metadata."""

import hashlib


def known(found, revision):
    return revision in (None, "main", found["commit"])


def not_found(code, message):
    return 404, {"X-Error-Code": code, "X-Error-Message": message}, {"error": message}


def info(found, _method, revision=None):
    if not known(found, revision):
        return not_found("RevisionNotFound", f"Invalid rev id: {revision}")
    siblings = [{"rfilename": p, "size": f[0]} for p, f in found["files"].items()]
    repo_id = found["repo_id"]
    body = {"id": repo_id, "modelId": repo_id, "author": repo_id.split("/")[0], "sha": found["commit"]}
    return 200, {}, body | {"private": False, "disabled": False, "gated": False, "siblings": siblings}


def tree(found, _method, revision, folder=None):
    if not known(found, revision):
        return not_found("RevisionNotFound", f"Invalid rev id: {revision}")
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


def resolve(found, method, revision, filename):
    if not known(found, revision):
        return not_found("RevisionNotFound", f"Invalid rev id: {revision}")
    if filename not in found["files"]:
        return not_found("EntryNotFound", f"{filename} is not in this model")
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
    return not_found("EntryNotFound", f"{filename} is only available over BitTorrent")
