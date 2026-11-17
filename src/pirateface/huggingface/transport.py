"""huggingface_hub HTTP transport that answers in Hugging Face's place: for repos only Pirate Face has, and for
any repo Pirate Face lists while Hugging Face does not answer."""

import re
import sys
import urllib.parse

from pirateface import ui
from pirateface.client import api
from pirateface.huggingface import responses
from pirateface.index import mirror

# Hugging Face API paths answered for repos only Pirate Face has (pirateface/ is answered by Pirate Face first).
INFO = re.compile(r"/api/models/([\w.-]+/[\w.-]+)(?:/revision/([^/]+))?")
TREE = re.compile(r"/api/models/([\w.-]+/[\w.-]+)/tree/([^/]+)(?:/(.+))?")
RESOLVE = re.compile(r"/([\w.-]+/[\w.-]+)/resolve/([^/]+)/(.+)")
PF_NAMESPACE = "pirateface"
# (repo_id, path) -> the listing that file's metadata came from, so the hook checks the bytes against it.
ANSWERED = {}
_announced = set()


def listing(repo_id):
    """The site's listing for the repo, or the cached index's while the site is down; None if neither has it."""
    try:
        return api.listed(repo_id)
    except api.Unreachable:
        return mirror.listing(repo_id)


def install():
    """Answer for repos only Pirate Face has, in huggingface_hub's HTTP client."""
    try:
        from huggingface_hub.utils import _http

        _wrap(_http)
    except (ImportError, AttributeError) as e:
        print(f"[pirateface] Pirate Face transport disabled: {e}", file=sys.stderr, flush=True)


def _wrap(http):
    import httpx2
    from huggingface_hub import constants

    class Transport(httpx2.BaseTransport):
        def __init__(self, inner):
            self.inner, self.host = inner, urllib.parse.urlsplit(constants.ENDPOINT).hostname

        def handle_request(self, request):
            raw_path = request.url.path if request.url.host == self.host else ""
            if raw_path and (answered := self._answer(request, raw_path, api.pf_only, PF_NAMESPACE)):
                return answered
            try:
                response = self.inner.handle_request(request)
            except httpx2.TransportError:
                # No answer at all (DNS, refused, timeout, proxy): Pirate Face answers for what it lists.
                if raw_path and (answered := self._answer(request, raw_path, listing)):
                    return answered
                raise
            # Hugging Face says "repository not found" as a 401 (no token) or 404 RepositoryNotFound (with one):
            # an answer, so only a repo only Pirate Face has is answered. A 5xx or 408 is no answer at all.
            missing = response.status_code == 401 or response.headers.get("X-Error-Code") == "RepositoryNotFound"
            down = response.status_code == 408 or response.status_code >= 500
            find = listing if down else api.pf_only if missing else None
            if raw_path and find and (answered := self._answer(request, raw_path, find)):
                response.close()
                return answered
            return response

        def _answer(self, request, raw_path, find, namespace=None):
            for pattern, answer in (
                (TREE, responses.tree),
                (INFO, responses.info),
                (RESOLVE, responses.resolve),
            ):
                m = pattern.fullmatch(raw_path)
                if not m or (namespace is not None and m[1].split("/")[0].lower() != namespace):
                    continue
                # Match on the raw path so a / inside a revision (refs/pr/1 arrives as refs%2Fpr%2F1) stays
                # one segment, then unquote the captures.
                repo = urllib.parse.unquote(m[1])
                rest = [urllib.parse.unquote(g) if g else g for g in m.groups()[1:]]
                try:
                    found = find(repo)
                except api.Unreachable:
                    continue
                if found:
                    if find is listing and repo not in _announced:
                        _announced.add(repo)
                        ui.note(
                            "hf",
                            f"{repo}  Hugging Face did not answer: commit {found['commit'][:12]} and its files from {found['trust']}",
                        )
                    status, headers, body = answer(found, request.method, *rest)
                    if pattern is RESOLVE and status < 400:
                        ANSWERED[(repo, rest[1])] = found
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
