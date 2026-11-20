"""Hybrid (BitTorrent v1 + v2, BEP 52) torrents in pure Python, laid out the way libtorrent's create_torrent does.

Layout rules libtorrent follows, and so this does:
- Files sorted by path (bytewise per component). Multi-file torrents get a BEP 47 pad file after every
  file that doesn't end on a piece boundary, including the last, so every file starts on a new piece.
  A pad is named `.pad/<n>-<length>`, n counting from 1. BEP 47 recommends the bare `.pad/<length>`,
  but two files needing the same padding would then share a path, and Transmission rejects a torrent
  with duplicate paths (it logs `duplicate path [.pad/135008]`, the add fails with `unrecognized
  info`); libtorrent 2.1.1 accepts and seeds the same torrent. Pad names are the one difference from
  libtorrent's `info` dictionary, whose create_torrent always writes the bare form; the v1 piece
  hashes are the same either way. A top-level `.pad` folder in the source is not listed, so a rebuild of
  an already-seeded folder produces the same torrent again instead of one that lists pad bytes.
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
    """About 2000 pieces, 256 KiB to 64 MiB (64 MiB keeps the .torrent under 3 MB up to ~3.9 TB)."""
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


def torrent_order(parts):
    """Sort key putting paths (lists of components) in torrent order: bytewise per component."""
    return [s.encode() for s in parts]


def from_folder(root, paths):
    """build_from() sources for the files paths (lists of components) under root, in torrent order."""
    return [(p, lambda p=p: Path(root).joinpath(*p).open("rb")) for p in sorted(paths, key=torrent_order)]


def files_under(root):
    """Relative paths (lists of components) of every file under root, following links.

    A top-level .pad directory is left out. Some clients write BEP 47 pad files there when they download,
    they are zeros the swarm serves anyway rather than model files, and listing them would put pad bytes
    in the file list: a folder that has already been seeded would then rebuild to a different infohash,
    and a pad's own path can duplicate a listed file's.
    """
    root = Path(root)
    paths = [list((d / f).relative_to(root).parts) for d, _, names in root.walk(follow_symlinks=True) for f in names]
    return [p for p in paths if p[0] != ".pad"]


def build(root, trackers, plen=None, sums=None, blocks=None):
    """Hybrid torrent of the folder root (its name is the torrent's name). Returns (bytes, v1 hex, v2 hex).
    sums: a dict to fill with each file's SHA-256, read in the same pass. blocks: as for build_from()."""
    root = Path(root)
    paths = files_under(root)
    if not paths:
        raise ValueError("no files to put in the torrent")
    if plen is None:
        plen = piece_length(sum(root.joinpath(*p).stat().st_size for p in paths))
    return build_from(root.absolute().name, from_folder(root, paths), trackers, plen, sums, blocks)


def build_from(
    name, sources, trackers, plen, sums=None, blocks=None, done=None, on_file=None, partial=None, on_piece=None
):
    """Hybrid torrent from sources: (path parts, opener) pairs in torrent order (from_folder sorts them).

    opener() is a context manager whose read(n) returns the next n bytes, or b"" at the end. Every read
    except the last must return exactly BLOCK bytes: each read is hashed as one v2 leaf. Nothing is kept,
    so a source can be a network stream. Returns (bytes, v1 hex, v2 hex). sums: as for build().
    blocks: a proofs.BlockHashes that keeps every block hash, for the block proofs after submitting.
    done: {path: (size, pieces root, piece layer, v1 piece hashes)} of files hashed by an earlier run, used
    instead of opening them (b"" for an absent root or layer). on_file(path, that tuple) is called as each
    file is finished on a piece boundary (every file once pads are added), so a run can be resumed there.
    partial: the saved offset, v1 hashes and v2 roots of an interrupted file. on_piece receives the completed
    piece state and stream at each piece boundary; the caller owns the stream's resumable whole-file hash.
    """
    if partial and sums is not None:
        raise ValueError("partial streams must provide their own resumable whole-file hash")
    per_piece = plen // BLOCK
    piece_pad = _merkle([ZERO] * per_piece, ZERO)
    v1 = _V1Pieces(plen)
    files, tree, layers, pad_index, padded = [], {}, {}, 0, len(sources) > 1
    for parts, opener in sources:
        path = "/".join(parts)
        if done and path in done:
            size, root, layer, pieces = done[path]
            v1.done += [pieces[i : i + 20] for i in range(0, len(pieces), 20)]
        else:
            first, size, leaves, piece_roots, root, layer = len(v1.done), 0, [], [], b"", b""
            if partial and partial["path"] == path:
                size = partial["offset"]
                pieces = bytes.fromhex(partial["v1"])
                roots = bytes.fromhex(partial["roots"])
                v1.done.extend(pieces[i : i + 20] for i in range(0, len(pieces), 20))
                piece_roots = [roots[i : i + 32] for i in range(0, len(roots), 32)]
            whole = hashlib.sha256() if sums is not None else None
            if blocks is not None:
                blocks.begin(path)
            with opener() as f:
                while block := f.read(BLOCK):
                    size += len(block)
                    v1.add(block)
                    if whole:
                        whole.update(block)
                    leaves.append(hashlib.sha256(block).digest())
                    if blocks is not None:
                        blocks.add(leaves[-1])
                    if len(leaves) == per_piece:
                        piece_roots.append(_merkle(leaves, ZERO))
                        leaves = []
                        if on_piece:
                            on_piece(path, size, b"".join(v1.done[first:]), b"".join(piece_roots), f)
            if whole:
                sums[path] = whole.hexdigest()
            if size > plen:
                if leaves:
                    piece_roots.append(_merkle(leaves + [ZERO] * (per_piece - len(leaves)), ZERO))
                root = _merkle(piece_roots, piece_pad)
                layer = b"".join(piece_roots)
            elif size:
                root = piece_roots[0] if piece_roots else _merkle(leaves, ZERO)
            v1.add(bytes(-size % plen if padded else 0))
            if on_file and (not v1.filled or not padded):
                pieces = b"".join(v1.done[first:])
                if v1.filled:
                    pieces += v1.hash.copy().digest()
                on_file(path, (size, root, layer, pieces))
        entry = {"length": size}
        if root:
            entry["pieces root"] = root
        if layer:
            layers[root] = layer
        node = tree
        for p in parts:
            node = node.setdefault(p, {})
        node[""] = entry
        files.append({"length": size, "path": parts})
        pad = -size % plen
        if pad and padded:
            pad_index += 1
            files.append({"attr": "p", "length": pad, "path": [".pad", f"{pad_index}-{pad}"]})
    info = {
        "file tree": tree,
        "files": files,
        "meta version": 2,
        "name": name,
        "piece length": plen,
        "pieces": v1.finish(),
    }
    torrent = {"announce": trackers[0], "announce-list": [[t] for t in trackers], "info": info, "piece layers": layers}
    encoded_info = bencode(info)
    return bencode(torrent), hashlib.sha1(encoded_info).hexdigest(), hashlib.sha256(encoded_info).hexdigest()
