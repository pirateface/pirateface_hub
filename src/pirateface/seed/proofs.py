"""Block proofs: tie single 16 KiB blocks to the v2 piece hashes in the .torrent (BEP 52 hash trees).

While it hashes, the builder keeps every block hash in a temporary file: 32 bytes per 16 KiB block, about
280 MB for a 140 GB model, on disk and never all in memory (pf work streams the files and cannot read them
again). Once the .torrent is built, Pirate Face names random blocks. The answer is each block's sibling
hashes, from its leaf up to its piece hash. The site fetches those blocks from Hugging Face and folds them
up to the piece hash, so a torrent whose bytes differ from Hugging Face's is caught one block at a time.
The temporary file is deleted when the submission ends. PF_NO_BLOCK_PROOFS=1 turns the proofs off.
"""

import base64
import contextlib
import hashlib
import tempfile

from pirateface.client import api
from pirateface.config import flag
from pirateface.seed.hybrid import BLOCK, ZERO

MAX_BODY_BYTES = 4_300_000  # the site's request cap, under its host's 4.5 MB limit


class BlockHashes:
    """Every block hash of one build, in torrent order, in a temporary file deleted on close."""

    def __init__(self):
        self._file = tempfile.TemporaryFile(prefix="pf-blocks-")  # noqa: SIM115  # closed by __exit__
        self._files = {}  # path -> [index of its first block hash, block count]
        self._count = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._file.close()

    def begin(self, path):
        self._files[path] = self._current = [self._count, 0]

    def add(self, digest):
        self._file.write(digest)
        self._count += 1
        self._current[1] += 1

    def _levels(self, first, count, width):
        """The hash tree over count block hashes from first, padded with zero hashes to width: leaves first."""
        self._file.seek(32 * first)
        data = self._file.read(32 * count)
        layer = [data[i : i + 32] for i in range(0, len(data), 32)] + [ZERO] * (width - count)
        levels = [layer]
        while len(layer) > 1:
            layer = [hashlib.sha256(layer[i] + layer[i + 1]).digest() for i in range(0, len(layer), 2)]
            levels.append(layer)
        return levels

    def siblings(self, picks, plen):
        """The sibling hashes, leaf to piece hash, of each (path, block) in picks, concatenated. The tree is
        the block's piece for a file longer than one piece, else the whole file (its pieces root). The site
        sends picks in torrent order, so blocks of one piece follow each other and share one tree."""
        self._file.flush()
        per_piece, out, tree, levels = plen // BLOCK, bytearray(), None, []
        for path, block in picks:
            first, count = self._files[path]
            if not 0 <= block < count:
                raise ValueError(f"the site asked for block {block} of {path}, which has {count}")
            if count > per_piece:
                base, width = block - block % per_piece, per_piece
            else:
                base, width = 0, 1 << (count - 1).bit_length()
            if tree != (path, base):
                tree, levels = (path, base), self._levels(first + base, min(width, count - base), width)
            index = block - base
            for level in levels[:-1]:
                out += level[index ^ 1]
                index //= 2
        return bytes(out)


def sidecar():
    """A BlockHashes for one build, or None (as a context) when PF_NO_BLOCK_PROOFS is set."""
    return contextlib.nullcontext() if flag("PF_NO_BLOCK_PROOFS") else BlockHashes()


def answer(key, torrent, blocks):
    """Ask the site which blocks to prove for this .torrent (bytes) and answer: base64 sibling hashes for the
    blockProofs field. None when proofs are off or cannot be sent: the submission then goes without them."""
    if blocks is None:
        return None
    encoded = base64.b64encode(torrent).decode()
    try:
        status, challenge = api.submit(key, {"torrent": encoded}, "/api/block-challenge")
        if status != 200:
            raise ValueError(f"HTTP {status}: {(challenge or {}).get('error', 'no challenge')}")
        picks = [(b["path"], b["block"]) for b in challenge["blocks"]]
        proof = base64.b64encode(blocks.siblings(picks, challenge["pieceLength"])).decode()
    except Exception as e:  # proofs are optional on the site: say why and submit without them
        print(f"    block proofs skipped ({e})")
        return None
    if len(encoded) + len(proof) > MAX_BODY_BYTES:
        print("    block proofs skipped (the .torrent and proofs are over the upload limit)")
        return None
    print(f"    block proofs: {len(picks)} blocks")
    return proof
