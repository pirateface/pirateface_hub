"""Model folder -> links, checksums and a hybrid (v1 + v2) torrent."""

import concurrent.futures
import contextlib
import hashlib
import os
import re
import urllib.parse
from pathlib import Path

import libtorrent as lt

from pirateface.config import SEEDS
from pirateface.seed import hybrid

# Hidden paths (.git, .cache, .env), keys and partial downloads stay out of the torrent.
SKIP = re.compile(r"(^|/)(\.|__pycache__/|id_rsa)|\.(pem|key|incomplete|lock|part)$")


# Names Windows can't store: reserved device names (with any extension), <>:"\\|?* and control
# characters, and a trailing dot or space.
WINDOWS_RESERVED = re.compile(r"(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\..*)?", re.IGNORECASE)
WINDOWS_INVALID = re.compile(r'[<>:"\\|?*\x00-\x1f]|[. ]$')


def seeds_folder(repo, v1):
    """The seed folder for a repo: author--model, as the Hugging Face cache names it, so two authors can
    publish models of the same name. An old <model> folder that already holds this same torrent keeps its
    place (its .torrent has this v1 infohash): nothing is renamed or moved, so a client seeding it is fine."""
    old, new = Path(repo.split("/")[1]), Path(repo.replace("/", "--"))
    for tor in Path(SEEDS, old).glob("*.torrent"):
        with contextlib.suppress(Exception):  # unreadable or not a torrent: not this one
            if str(lt.torrent_info(tor.read_bytes()).info_hashes().v1) == v1:
                return old
    return new


class LinkError(Exception):
    pass


def link(target, dest):
    """Symlink dest to target; a hard link where symlinks need rights (Windows without Developer Mode).
    A real file already at dest is kept: it is the file itself, or one the torrent client downloaded."""
    if dest.exists() and not dest.is_symlink():
        return
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


def clear_links(dest):
    """Remove the links an earlier run left under dest, and the folders that empties. Real files stay:
    dest can hold the user's files, or files their torrent client downloaded."""
    if not dest.is_dir() or dest.is_symlink():
        return
    for d, _, names in os.walk(dest, topdown=False):
        for n in names:
            if Path(d, n).is_symlink():
                Path(d, n).unlink()
        with contextlib.suppress(OSError):
            Path(d).rmdir()


def stage(src, dest):
    """Link the model files in src into dest. Returns (included paths, skipped paths)."""
    clear_links(dest)
    included, skipped = [], []
    for d, _, names in os.walk(src, followlinks=True):
        for n in names:
            rel = Path(d, n).relative_to(src).as_posix()
            (skipped if SKIP.search(rel) else included).append(rel)
    problems = unportable(sorted(included))
    if problems:
        raise LinkError("rename or remove these files first:\n    " + "\n    ".join(problems[:20]))
    if Path(src).resolve() != Path(dest).resolve():
        # src is dest: the files are already where the torrent is built from; linking would be self-links.
        for rel in sorted(included):
            link(Path(src, rel).absolute(), dest / rel)
    return sorted(included), sorted(skipped)


def link_existing(src, dest, paths):
    """Link the files of a listed torrent that src already has. Returns how many."""
    if Path(src).resolve() == Path(dest).resolve():
        # src is dest: the files (or the links to them) are already where the client seeds from.
        return sum(1 for p in paths if Path(src, p).is_file())
    clear_links(dest)
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
