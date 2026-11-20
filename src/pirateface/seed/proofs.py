"""Block proofs: tie single 16 KiB blocks to the v2 piece hashes in the .torrent (BEP 52 hash trees).

While it hashes, the builder keeps every block hash in a file: 32 bytes per 16 KiB block, about
280 MB for a 140 GB model, on disk and never all in memory (pf work streams the files and cannot read them
again). Once the .torrent is built, Pirate Face names random blocks. The answer is each block's sibling
hashes, from its leaf up to its piece hash, uploaded in parts of their own so the .torrent's upload limit
never applies to them; the submission names the part count. The site fetches those blocks from Hugging Face and folds them
up to the piece hash, so a torrent whose bytes differ from Hugging Face's is caught one block at a time.
The file is deleted when the submission ends; pf work keeps it in its checkpoint until the job ends.
PF_NO_BLOCK_PROOFS=1 turns the proofs off.
"""

import base64
import contextlib
import hashlib
import os
import tempfile

from pirateface.client import api
from pirateface.config import flag
from pirateface.seed.hybrid import BLOCK, ZERO

PART_BYTES = 1 << 20  # sibling hashes per upload: the most the site takes in one part


class BlockHashes:
    """Every block hash of one build, in the order the files were read, in a file (temporary unless path is given)."""

    def __init__(self, path=None):
        """path: a file kept across runs (pf work's checkpoint, which deletes it), else a temporary file."""
        if path is None:
            self._file = tempfile.TemporaryFile(prefix="pf-blocks-")  # noqa: SIM115  # closed by __exit__
        else:
            self._file = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600), "a+b")
        self._files = {}  # path -> [index of its first block hash, block count]
        self._count = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._file.close()

    def begin(self, path):
        if path not in self._files:
            self._files[path] = [self._count, 0]
        self._current = self._files[path]

    def add(self, digest):
        self._file.write(digest)
        self._count += 1
        self._current[1] += 1

    def keep(self, files):
        """Start after the hashes an earlier run left in the file for files, [(path, block count)] in the order
        they were written, and drop anything after them."""
        self._file.truncate(32 * sum(count for _, count in files))
        for path, count in files:
            self._files[path] = [self._count, count]
            self._count += count

    def flush(self):
        self._file.flush()

    def verify_partial(self, path, roots, plen):
        """Check saved partial-file block hashes against their stored piece roots."""
        first, count = self._files[path]
        per_piece = plen // BLOCK
        if count % per_piece or len(roots) != count // per_piece * 32:
            raise ValueError(f"invalid saved block hashes for {path}")
        self.flush()
        for piece in range(count // per_piece):
            actual = self._levels(first + piece * per_piece, per_piece, per_piece)[-1][0]
            if actual != roots[piece * 32 : (piece + 1) * 32]:
                raise ValueError(f"saved block hashes are corrupt for {path}")

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
        self.flush()
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


def sidecar(path=None):
    """A BlockHashes for one build, or None (as a context) when PF_NO_BLOCK_PROOFS is set. path: as for BlockHashes."""
    return contextlib.nullcontext() if flag("PF_NO_BLOCK_PROOFS") else BlockHashes(path)


def answer(key, torrent, blocks):
    """Ask the site which blocks to prove for this .torrent (bytes), upload the answer in parts, and return the
    fields to add to the submission ({"blockProofParts": n}). {} when proofs are off or the upload failed: the
    submission then goes without them."""
    if blocks is None:
        return {}
    try:
        status, challenge = api.submit(key, {"torrent": base64.b64encode(torrent).decode()}, "/api/block-challenge")
        if status != 200:
            raise ValueError(f"HTTP {status}: {(challenge or {}).get('error', 'no challenge')}")
        picks = [(b["path"], b["block"]) for b in challenge["blocks"]]
        siblings = blocks.siblings(picks, challenge["pieceLength"])
        parts = [siblings[i : i + PART_BYTES] for i in range(0, len(siblings), PART_BYTES)] or [b""]
        for n, part in enumerate(parts):
            body = {"challengeId": challenge["challengeId"], "part": n, "siblings": base64.b64encode(part).decode()}
            status, reply = api.submit(key, body, "/api/block-proofs")
            if status != 200:
                raise ValueError(f"part {n}: HTTP {status}: {(reply or {}).get('error', 'not saved')}")
    except Exception as e:  # proofs are optional on the site: say why and submit without them
        print(f"    block proofs skipped ({e})")
        return {}
    print(f"    block proofs: {len(picks)} blocks")
    return {"blockProofParts": len(parts)}
