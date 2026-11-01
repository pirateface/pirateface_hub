"""Models only on Pirate Face: the answers huggingface_hub gets, without network (Pirate Face and Hugging Face faked)."""

import types

import httpx2
import libtorrent as lt
import pytest
from huggingface_hub import constants

from pirateface import only_on_pirateface as oop
from pirateface.client import api

FILES = {"model.onnx": b"weights" * 100, "docs/README.md": b"# card\n"}


@pytest.fixture
def pf(tmp_path, monkeypatch):
    """Pirate Face lists repos under 'pirateface/' and 'gone/' as only on Pirate Face, with a real .torrent."""
    root = tmp_path / "m"
    for path, data in FILES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)
    t = lt.create_torrent(lt.list_files(str(root)), 16384, flags=lt.create_torrent.v1_only)
    lt.set_piece_hashes(t, str(tmp_path))
    torrent = lt.bencode(t.generate())
    entry = {"hf": False, "revision": "a" * 40, "files": [{"path": p, "sha256": "f" * 64} for p in FILES]}
    on_pf = ("pirateface/", "gone/")
    monkeypatch.setattr(
        api, "lookup", lambda repo: dict(entry, repoId=repo) if repo.startswith(on_pf) else api.NOT_LISTED
    )
    monkeypatch.setattr(api, "torrent_file", lambda repo: torrent)
    oop.model.cache_clear()
    yield
    oop.model.cache_clear()


def client(hf):
    """A client whose transport is wrapped by install(), with Hugging Face answered by hf(request)."""
    http = types.SimpleNamespace(_GLOBAL_CLIENT_FACTORY=lambda: httpx2.Client(transport=httpx2.MockTransport(hf)))
    http.set_client_factory = lambda f: setattr(http, "factory", f)
    oop.install(http)
    return http.factory()


def missing(request):
    return httpx2.Response(401, request=request)


def test_model_reads_sizes_from_the_torrent(pf):
    m = oop.model("gone/x")
    assert m["commit"] == "a" * 40
    assert {p: f[0] for p, f in m["files"].items()} == {p: len(d) for p, d in FILES.items()}


def test_repo_hf_doesnt_have_is_answered_from_pirate_face(pf):
    c = client(missing)
    info = c.get(f"{constants.ENDPOINT}/api/models/gone/x").json()
    assert info["id"] == "gone/x" and info["sha"] == "a" * 40
    tree = c.get(f"{constants.ENDPOINT}/api/models/gone/x/tree/main/docs").json()
    assert [f["path"] for f in tree] == ["docs/README.md"]
    head = c.head(f"{constants.ENDPOINT}/gone/x/resolve/main/model.onnx")
    assert head.headers["X-Linked-Etag"] == f'"{"f" * 64}"' and head.headers["X-Repo-Commit"] == "a" * 40
    assert c.get(f"{constants.ENDPOINT}/gone/x/resolve/main/nope.bin").headers["X-Error-Code"] == "EntryNotFound"
    assert c.get(f"{constants.ENDPOINT}/api/models/gone/x/revision/b{'0' * 39}").status_code == 404


def test_pirateface_namespace_never_asks_hugging_face(pf):
    asked = []
    c = client(lambda r: asked.append(r.url.path) or httpx2.Response(200, json={"id": "someone-else"}, request=r))
    assert c.get(f"{constants.ENDPOINT}/api/models/pirateface/x").json()["id"] == "pirateface/x"
    assert asked == []


def test_models_on_hugging_face_pass_through(pf):
    c = client(lambda r: httpx2.Response(200, json={"id": "org/model"}, request=r))
    assert c.get(f"{constants.ENDPOINT}/api/models/org/model").json() == {"id": "org/model"}
    assert c.get(f"{constants.ENDPOINT}/api/models/other/unknown").status_code == 200
