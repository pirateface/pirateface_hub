"""Hybrid (BitTorrent v1 + v2, BEP 52) torrents in pure Python, byte-identical to libtorrent's create_torrent.

Layout rules libtorrent follows, and so this does:
- Files sorted by path (bytewise per component). Multi-file torrents get a BEP 47 pad file after every
  file that doesn't end on a piece boundary, including the last, so every file starts on a new piece.
- v2: each file has its own merkle tree of 16 KiB blocks. A file larger than one piece is padded to whole
  pieces (the last piece's leaves padded with zero hashes up to blocks-per-piece) and its piece layer is
  stored in "piece layers". A file of one piece or less hashes only its own blocks. Empty files have no root.
"""

import hashlib
from pathlib import Path

BLOCK = 16384
ZERO = bytes(32)
MIN_EXP, MAX_EXP, TARGET_PIECES = 18, 26, 2000


def bencode(x):
    if isinstance(x, int):
        return b"i%de" % x
    if isinstance(x, str):
        x = x.encode()
    if isinstance(x, bytes):
        return b"%d:%s" % (len(x), x)
    if isinstance(x, list):
        return b"l" + b"".join(map(bencode, x)) + b"e"
    return b"d" + b"".join(bencode(k) + bencode(x[k]) for k in sorted(x, key=_key)) + b"e"


def _key(k):
    return k.encode() if isinstance(k, str) else k


def piece_length(total_size):
    """About 2000 pieces, 256 KiB to 64 MiB (64 MiB keeps the .torrent under 3 MB up to ~10 TB)."""
    exp = MIN_EXP
    while exp < MAX_EXP and total_size / (1 << exp) > TARGET_PIECES:
        exp += 1
    return 1 << exp


def _merkle(layer, pad):
    """Pad the layer to a power of two, then hash pairs up to the root."""
    n = 1
    while n < len(layer):
        n *= 2
    layer = layer + [pad] * (n - len(layer))
    while len(layer) > 1:
        layer = [hashlib.sha256(layer[i] + layer[i + 1]).digest() for i in range(0, len(layer), 2)]
    return layer[0]


class _V1Pieces:
    """SHA-1 of every piece across the concatenated files and pad files."""

    def __init__(self, plen):
        self.plen, self.done, self.hash, self.filled = plen, [], hashlib.sha1(), 0

    def add(self, data):
        view = memoryview(data)
        while view:
            take = min(len(view), self.plen - self.filled)
            self.hash.update(view[:take])
            self.filled += take
            view = view[take:]
            if self.filled == self.plen:
                self.done.append(self.hash.digest())
                self.hash, self.filled = hashlib.sha1(), 0

    def finish(self):
        if self.filled:
            self.done.append(self.hash.digest())
        return b"".join(self.done)


def files_under(root):
    """Relative paths (lists of components) of every file under root, following links, in torrent order."""
    root = Path(root)
    paths = [list((d / f).relative_to(root).parts) for d, _, names in root.walk(follow_symlinks=True) for f in names]
    return sorted(paths, key=lambda p: [s.encode() for s in p])


def build(root, trackers, plen=None):
    """Hybrid torrent of the folder root (its name is the torrent's name). Returns (bytes, v1 hex, v2 hex)."""
    root = Path(root)
    paths = files_under(root)
    if not paths:
        raise ValueError("no files to put in the torrent")
    if plen is None:
        plen = piece_length(sum(root.joinpath(*p).stat().st_size for p in paths))
    per_piece = plen // BLOCK
    piece_pad = _merkle([ZERO] * per_piece, ZERO)
    v1 = _V1Pieces(plen)
    files, tree, layers = [], {}, {}
    for parts in paths:
        size, leaves, piece_roots = 0, [], []
        with root.joinpath(*parts).open("rb") as f:
            while block := f.read(BLOCK):
                size += len(block)
                v1.add(block)
                leaves.append(hashlib.sha256(block).digest())
                if len(leaves) == per_piece:
                    piece_roots.append(_merkle(leaves, ZERO))
                    leaves = []
        entry = {"length": size}
        if size > plen:
            if leaves:
                piece_roots.append(_merkle(leaves + [ZERO] * (per_piece - len(leaves)), ZERO))
            entry["pieces root"] = _merkle(piece_roots, piece_pad)
            layers[entry["pieces root"]] = b"".join(piece_roots)
        elif size:
            entry["pieces root"] = piece_roots[0] if piece_roots else _merkle(leaves, ZERO)
        node = tree
        for p in parts:
            node = node.setdefault(p, {})
        node[""] = entry
        files.append({"length": size, "path": parts})
        pad = -size % plen
        if pad and len(paths) > 1:
            files.append({"attr": "p", "length": pad, "path": [".pad", str(pad)]})
            v1.add(bytes(pad))
    info = {
        "file tree": tree,
        "files": files,
        "meta version": 2,
        "name": root.absolute().name,
        "piece length": plen,
        "pieces": v1.finish(),
    }
    torrent = {"announce": trackers[0], "announce-list": [[t] for t in trackers], "info": info, "piece layers": layers}
    encoded_info = bencode(info)
    return bencode(torrent), hashlib.sha1(encoded_info).hexdigest(), hashlib.sha256(encoded_info).hexdigest()
