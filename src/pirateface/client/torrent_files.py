"""Reading a .torrent file: its files and their web seed URLs."""

import urllib.parse


def files(data):
    """(path, size, web seed URL or None) for each file in a .torrent, BEP 47 pad files left out."""
    import libtorrent as lt

    torrent = lt.bdecode(data)
    info = torrent[b"info"]
    name = info[b"name"].decode()
    if b"files" not in info:
        return [(name, info[b"length"], web_seed_url(torrent, name))]
    files = []
    for f in info[b"files"]:
        if b"p" in f.get(b"attr", b""):
            continue
        path = "/".join(part.decode() for part in f[b"path"])
        files.append((path, f[b"length"], web_seed_url(torrent, f"{name}/{path}")))
    return files


def web_seed_url(torrent, path_in_torrent):
    """BEP 19: a base ending in "/" gets the file's path appended; otherwise it is the file itself."""
    seeds = torrent.get(b"url-list") or []
    seed = (seeds if isinstance(seeds, bytes) else seeds[0] if seeds else b"").decode()
    if not seed:
        return None
    return seed + urllib.parse.quote(path_in_torrent) if seed.endswith("/") else seed
