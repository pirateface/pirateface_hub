"""Model folder -> links, checksums and a hybrid (v1 + v2) torrent."""

import concurrent.futures
import hashlib
import os
import re
import shutil
import urllib.parse
from pathlib import Path, PurePosixPath

import libtorrent as lt

from pirateface.seed import hybrid

# Hidden paths (.git, .cache, .env), keys and partial downloads stay out of the torrent.
SKIP = re.compile(r"(^|/)(\.|__pycache__/|id_rsa)|\.(pem|key|incomplete|lock|part)$")


# Names Windows can't store: reserved device names (with any extension), <>:"\\|?* and control
# characters, and a trailing dot or space.
WINDOWS_RESERVED = re.compile(r"(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\..*)?", re.IGNORECASE)
WINDOWS_INVALID = re.compile(r'[<>:"\\|?*\x00-\x1f]|[. ]$')


class LinkError(Exception):
    pass


def link(target, dest):
    """Symlink dest to target; a hard link where symlinks need rights (Windows without Developer Mode)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.symlink_to(target)
    except OSError:
        try:
            dest.hardlink_to(target)
        except OSError as e:
            raise LinkError(
                f"could not link {dest.name} ({e}). On Windows, turn on Developer Mode "
                "(Settings > System > For developers), or use an --out folder on the same drive as the model"
            ) from e


def unportable(paths):
    """Why these torrent paths ("/"-separated) can't be downloaded as-is everywhere, or [].
    Names that differ only in case (file or folder) would overwrite each other on Windows and macOS,
    and names Windows can't store fail there. Both are refused, not renamed: renaming changes the files."""
    problems, seen = [], {}
    for rel in paths:
        parts = rel.split("/")
        for i, part in enumerate(parts):
            prefix = "/".join(parts[: i + 1])
            if WINDOWS_RESERVED.fullmatch(part) or WINDOWS_INVALID.search(part):
                problems.append(f"{prefix}: not a valid file name on Windows")
            if seen.setdefault(prefix.casefold(), prefix) != prefix:
                problems.append(f"{prefix} and {seen[prefix.casefold()]}: names differ only in case")
    return list(dict.fromkeys(problems))


def stage(src, dest):
    """Link the model files in src into dest. Returns (included paths, skipped paths)."""
    shutil.rmtree(dest, ignore_errors=True)
    included, skipped = [], []
    for d, _, names in os.walk(src, followlinks=True):
        for n in names:
            rel = Path(d, n).relative_to(src).as_posix()
            (skipped if SKIP.search(rel) else included).append(rel)
    problems = unportable(sorted(included))
    if problems:
        raise LinkError("rename or remove these files first:\n    " + "\n    ".join(problems[:20]))
    for rel in sorted(included):
        link(Path(src, rel).absolute(), dest / rel)
    return sorted(included), sorted(skipped)


def stage_files(files, dest):
    """Link selected source files into dest under explicit torrent-relative paths."""
    shutil.rmtree(dest, ignore_errors=True)
    included, skipped = [], []
    for rel, source in files.items():
        path = PurePosixPath(rel)
        if path.is_absolute() or "\\" in rel or any(part in ("", ".", "..") for part in path.parts):
            raise LinkError(f"invalid torrent path: {rel!r}")
        (skipped if SKIP.search(rel) else included).append((rel, Path(source)))
    problems = unportable(sorted(rel for rel, _ in included))
    if problems:
        raise LinkError("rename or remove these files first:\n    " + "\n    ".join(problems[:20]))
    for rel, source in sorted(included):
        if not source.is_file():
            raise LinkError(f"source file is missing: {source}")
        link(source.absolute(), dest / rel)
    return sorted(rel for rel, _ in included), sorted(skipped)


def link_existing(src, dest, paths):
    """Link the files of a listed torrent that src already has. Returns how many."""
    shutil.rmtree(dest, ignore_errors=True)
    found = [p for p in paths if Path(src, p).is_file()]
    for p in found:
        link(Path(src, p).absolute(), dest / p)
    return len(found)


def checksums(root, paths):
    """{path: sha256}"""

    def one(p):
        with Path(root, p).open("rb") as f:
            return p, hashlib.file_digest(f, "sha256").hexdigest()

    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        return dict(pool.map(one, paths))


def make_torrent(save_dir, name, trackers):
    """Hybrid v1 + v2 torrent of save_dir/name (see hybrid.py). Returns (bytes, v1 infohash, v2 infohash)."""
    return hybrid.build(Path(save_dir, name), trackers)


def with_trackers(data, trackers):
    """The same torrent (same infohash) announcing to these trackers."""
    t = lt.bdecode(data)
    t[b"announce"] = trackers[0].encode()
    t[b"announce-list"] = [[url.encode()] for url in trackers]
    return lt.bencode(t)


def info(data):
    """(name, v1 infohash, v2 infohash or None, single file?)"""
    ti = lt.torrent_info(data)
    v2 = ti.info_hashes().v2
    return ti.name(), str(ti.info_hashes().v1), None if v2.is_all_zeros() else str(v2), ti.num_files() == 1


def magnet(v1, v2, name=None, trackers=()):
    q = urllib.parse.quote
    m = f"magnet:?xt=urn:btih:{v1}" + (f"&xt=urn:btmh:1220{v2}" if v2 else "")
    if name:
        m += "&dn=" + q(name, safe="")
    return m + "".join("&tr=" + q(t, safe="") for t in trackers)
