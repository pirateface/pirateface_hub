"""Which files of a model torrent can join a per-file swarm.

Pirate Face publishes a pure v2 torrent per file (by BEP 52 pieces root, /api/files/<root>). Every upload
with the same bytes shares it. The model torrent already holds each file's pieces root: nothing is hashed."""

from pathlib import Path

import libtorrent as lt

MIN_SIZE = 1 << 20  # configs and tokenizers aren't worth a swarm each


def local_files(data, save_dir):
    """(path, pieces root) of the torrent's files of MIN_SIZE or more that save_dir has whole. None if v1 only."""
    ti = lt.torrent_info(data)
    if ti.info_hashes().v2.is_all_zeros():
        return None
    fs = ti.files()
    found = []
    for i in range(fs.num_files()):
        if fs.file_flags(i) & fs.flag_pad_file:
            continue  # BEP 47 padding: zeros, with no pieces root of its own
        path, size = Path(save_dir, fs.file_path(i)), fs.file_size(i)
        if size >= MIN_SIZE and path.is_file() and path.stat().st_size == size:
            found.append((path, str(fs.root(i))))
    return found


def biggest_file(data):
    """The largest non-pad file's size in the torrent; 0 when every file is padding. Under MIN_SIZE, nothing
    in the torrent is worth a per-file swarm, however the files turn out locally."""
    fs = lt.torrent_info(data).files()
    return max(
        (fs.file_size(i) for i in range(fs.num_files()) if not (fs.file_flags(i) & fs.flag_pad_file)),
        default=0,
    )
