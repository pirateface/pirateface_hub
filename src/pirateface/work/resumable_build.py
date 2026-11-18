"""Hybrid torrent builder with resumable per-piece checkpoints."""

import hashlib

from pirateface.seed import hybrid
from pirateface.work.errors import WorkError
from pirateface.work.resumable_hash import ResumableSHA256


def build(repo, files, trackers, store, opener, blocks, note, note_reused=None):
    total = sum(f["size"] for f in files)
    plen = hybrid.piece_length(total)
    per_piece = plen // hybrid.BLOCK
    piece_pad = hybrid._merkle([hybrid.ZERO] * per_piece, hybrid.ZERO)
    v1, torrent_files, tree, layers, sums = [], [], {}, {}, {}
    pad_index = 0
    for file in files:
        path, size = file["path"], file["size"]
        parts = path.split("/")
        if blocks is not None:
            blocks.begin(path)
        roots, last_leaves, offset, digest = [], [], 0, None
        saved = store.pieces(path)
        sha = ResumableSHA256()

        def record(
            piece_offset, length, piece_v1, raw_leaves, sha_state, restore=True, path=path, size=size, roots=roots
        ):
            nonlocal offset, last_leaves, digest, sha
            if piece_offset != offset or length <= 0 or length > plen or offset + length > size:
                raise WorkError(f"invalid checkpoint offset for {path}")
            if len(piece_v1) != 20 or len(raw_leaves) != ((length + hybrid.BLOCK - 1) // hybrid.BLOCK) * 32:
                raise WorkError(f"invalid checkpoint hashes for {path}")
            last_leaves = [raw_leaves[i : i + 32] for i in range(0, len(raw_leaves), 32)]
            if blocks is not None:
                for leaf in last_leaves:
                    blocks.add(leaf)
            v1.append(piece_v1)
            offset += length
            if len(sha_state) == 32:
                if offset != size:
                    raise WorkError(f"invalid final checkpoint for {path}")
                digest = sha_state.hex()
            elif len(sha_state) == 40 and length == plen and offset < size:
                if restore:
                    sha = ResumableSHA256.restore(sha_state, offset)
            else:
                raise WorkError(f"invalid checkpoint hash state for {path}")
            if size > plen:
                roots.append(hybrid._merkle(last_leaves + [hybrid.ZERO] * (per_piece - len(last_leaves)), hybrid.ZERO))

        for piece in saved:
            record(*piece)
        (note_reused or note)(offset)
        if offset < size:
            with opener(path, offset, sha) as stream:
                while offset < size:
                    start = offset
                    length = min(plen, size - offset)
                    piece_hash, leaves, remaining = hashlib.sha1(), [], length
                    while remaining:
                        data = stream.read(min(hybrid.BLOCK, remaining))
                        if not data or len(data) != min(hybrid.BLOCK, remaining):
                            raise WorkError(f"{path}: file ended before its declared size")
                        piece_hash.update(data)
                        leaves.append(hashlib.sha256(data).digest())
                        remaining -= len(data)
                    final = start + length == size
                    if final:
                        if stream.read(1):
                            raise WorkError(f"{path}: file exceeds its declared size")
                        if len(files) > 1:
                            piece_hash.update(bytes(-length % plen))
                        sha_state = bytes.fromhex(sha.hexdigest())
                        if file.get("sha256") and sha_state.hex() != file["sha256"]:
                            store.discard_file(path)
                            raise WorkError(f"{path}: downloaded bytes do not match the expected SHA-256")
                    else:
                        sha_state = sha.checkpoint()
                    raw_leaves = b"".join(leaves)
                    store.append(path, start, length, piece_hash.digest(), raw_leaves, sha_state)
                    record(start, length, piece_hash.digest(), raw_leaves, sha_state, restore=False)
        if size == 0:
            with opener(path, 0, sha) as stream:
                if stream.read(1):
                    raise WorkError(f"{path}: file exceeds its declared size")
            digest = sha.hexdigest()
        if digest is None or (file.get("sha256") and digest != file["sha256"]):
            store.discard_file(path)
            raise WorkError(f"{path}: saved or downloaded bytes do not match the expected SHA-256")
        sums[path] = digest
        entry = {"length": size}
        if size > plen:
            entry["pieces root"] = hybrid._merkle(roots, piece_pad)
            layers[entry["pieces root"]] = b"".join(roots)
        elif size:
            entry["pieces root"] = hybrid._merkle(last_leaves, hybrid.ZERO)
        node = tree
        for part in parts:
            node = node.setdefault(part, {})
        node[""] = entry
        torrent_files.append({"length": size, "path": parts})
        pad = -size % plen
        if pad and len(files) > 1:
            pad_index += 1
            torrent_files.append({"attr": "p", "length": pad, "path": [".pad", f"{pad_index}-{pad}"]})
    info = {
        "file tree": tree,
        "files": torrent_files,
        "meta version": 2,
        "name": repo.split("/")[1],
        "piece length": plen,
        "pieces": b"".join(v1),
    }
    torrent = {"announce": trackers[0], "announce-list": [[t] for t in trackers], "info": info, "piece layers": layers}
    encoded_info = hybrid.bencode(info)
    data = hybrid.bencode(torrent)
    manifest = [{"path": f["path"], "sha256": sums[f["path"]]} for f in files]
    return data, manifest, hashlib.sha1(encoded_info).hexdigest()
