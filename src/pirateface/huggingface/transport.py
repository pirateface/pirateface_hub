"""huggingface_hub HTTP transport that answers for repos only Pirate Face has."""

import re
import sys
import urllib.parse

from pirateface.client import api
from pirateface.huggingface import responses

# Hugging Face API paths answered for repos only Pirate Face has (pirateface/ is answered by Pirate Face first).
INFO = re.compile(r"/api/models/([\w.-]+/[\w.-]+)(?:/revision/([^/]+))?")
TREE = re.compile(r"/api/models/([\w.-]+/[\w.-]+)/tree/([^/]+)(?:/(.+))?")
RESOLVE = re.compile(r"/([\w.-]+/[\w.-]+)/resolve/([^/]+)/(.+)")
PF_NAMESPACE = "pirateface"


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
            for pattern, answer in (
                (TREE, responses.tree),
                (INFO, responses.info),
                (RESOLVE, responses.resolve),
            ):
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
