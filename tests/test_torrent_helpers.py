"""Tests for the pure helpers in pirateface.client.torrent (no network/libtorrent state)."""

from pirateface.client import torrent


def test_web_seed_url_appends_path_for_trailing_slash():
    t = {b"url-list": [b"http://example.com/base/"]}
    assert torrent.web_seed_url(t, "model/config.json") == "http://example.com/base/model/config.json"


def test_web_seed_url_returns_seed_when_no_trailing_slash():
    t = {b"url-list": [b"http://example.com/file.bin"]}
    assert torrent.web_seed_url(t, "anything") == "http://example.com/file.bin"


def test_web_seed_url_none_when_no_seeds():
    t = {b"url-list": []}
    assert torrent.web_seed_url(t, "x") is None


def test_files_in_single_file():
    import libtorrent as lt
    info = {b"name": b"model.bin", b"length": 42}
    data = lt.bencode({b"info": info})
    assert torrent.files_in(data) == [("model.bin", 42, None)]


def test_files_in_multi_file_skips_pad_files():
    import libtorrent as lt
    info = {
        b"name": b"model",
        b"files": [
            {b"path": [b"config.json"], b"length": 100},
            {b"path": [b"pad"], b"length": 16, b"attr": b"p"},
            {b"path": [b"vocab.txt"], b"length": 200},
        ],
    }
    data = lt.bencode({b"info": info})
    out = torrent.files_in(data)
    paths = [p for p, _, _ in out]
    assert "config.json" in paths
    assert "vocab.txt" in paths
    assert "pad" not in paths
