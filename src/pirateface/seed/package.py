"""Model folder -> links, checksums and a hybrid (v1 + v2) torrent."""

import contextlib
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
WINDOWS_RESERVED = re.compile(r"(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[0-9¹²³]|LPT[0-9¹²³])(\..*)?", re.IGNORECASE)
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
    except FileExistsError:
        # dest already holds an entry (a symlink, or a real file). Not a rights problem.
        raise LinkError(f"{dest} already exists; remove it and rerun") from None
    except OSError:
        try:
            dest.hardlink_to(target)
        except OSError as e:
            raise LinkError(
                f"could not link {dest.name} ({e}). On Windows, turn on Developer Mode "
                "(Settings > System > For developers), or use an --out folder on the same drive as the model"
            ) from e


def unsafe_path(rel, windows=True):
    """Why rel ("/"-separated) can't be written under a folder as-is, or None: a part that is empty, "." or ".."
    or holds a backslash would leave the folder (absolute and UNC forms included). With windows, names Windows
    can't store are refused too: there a drive letter, a stream or a device name is another way out."""
    for part in rel.split("/"):
        if part in ("", ".", "..") or "\\" in part:
            return f"{rel}: leaves its folder"
        if windows and (WINDOWS_RESERVED.fullmatch(part) or WINDOWS_INVALID.search(part)):
            return f"{rel}: not a valid file name on Windows"
    return None


def check_listing(data, paths):
    """Raise LinkError if a listed torrent (paths: torrent.files_in) has a path unsafe_path refuses on this OS or
    a symlink entry. Seeding links these paths into the seed folder and the torrent client writes them, so this
    runs before either. Windows-only names stay seedable elsewhere: the listing is already published."""
    files = lt.bdecode(data)[b"info"].get(b"files") or []
    problems = [why for p in paths if (why := unsafe_path(p, windows=os.name == "nt"))]
    problems += [
        "/".join(p.decode(errors="replace") for p in f[b"path"]) + ": a symlink"
        for f in files
        if b"l" in f.get(b"attr", b"")
    ]
    if problems:
        raise LinkError("Pirate Face's .torrent lists paths pf won't write:\n    " + "\n    ".join(problems[:20]))


def unportable(paths):
    """Why these torrent paths ("/"-separated) can't be downloaded as-is everywhere, or [].
    Names that differ only in case (file or folder) would overwrite each other on Windows and macOS,
    and names Windows can't store fail there. Both are refused, not renamed: renaming changes the files."""
    problems, seen = [], {}
    for rel in paths:
        if why := unsafe_path(rel):
            problems.append(why)
        parts = rel.split("/")
        for i in range(len(parts)):
            prefix = "/".join(parts[: i + 1])
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
    included, skipped, seen = [], [], set()
    for d, dirs, names in os.walk(src, followlinks=True):
        # Links are followed (the HF cache is links), but a folder reached twice, as through a link back up
        # the tree, is listed once: a loop would otherwise pack the model into the torrent many times.
        real = Path(d).resolve()
        if real in seen:
            dirs.clear()
            continue
        seen.add(real)
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


def make_torrent(save_dir, name, trackers, sums=None):
    """Hybrid v1 + v2 torrent of save_dir/name (see hybrid.py). Returns (bytes, v1 infohash, v2 infohash).
    sums: a dict to fill with each file's SHA-256, read in the same pass."""
    return hybrid.build(Path(save_dir, name), trackers, sums=sums)


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
