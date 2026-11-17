"""What a .torrent's file list is allowed to contain, shared by the download and the seeding side.

A listed path is written under a folder: the torrent's own folder in the download cache, the seed folder for
`pf seed`. Both sides ask the same questions of it here, so a listing one of them refuses is refused by both.
"""

import os
import re
from pathlib import PurePosixPath

import libtorrent as lt

# Names Windows can't store: reserved device names (with any extension), <>:"\|?* and control
# characters, and a trailing dot or space.
WINDOWS_RESERVED = re.compile(r"(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[0-9¹²³]|LPT[0-9¹²³])(\..*)?", re.IGNORECASE)
WINDOWS_INVALID = re.compile(r'[<>:"\\|?*\x00-\x1f]|[. ]$')


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


def collisions(paths):
    """Paths that would land on one file: one name once case is folded, or a file that is also a folder."""
    files = {}
    for p in paths:
        if (first := files.setdefault(p.casefold(), p)) != p:
            yield f"{first} and {p}: one name once case is folded"
    for p in files.values():
        for folder in PurePosixPath(p).parents:
            if folder == PurePosixPath("."):  # the root: not a folder any listing can name
                break
            if file := files.get(str(folder).casefold()):
                yield f"{file} and {p}: one is a file, the other is a folder"
                break


def info_problems(info, paths):
    """Why this torrent's info dict and its paths (see torrent.files_in) can't be written under a folder as-is,
    or []. Windows-only names are refused on Windows only: a listing that is already published stays usable
    where the names are legal. Case-folded collisions are refused even where this disk can store both files:
    see `collisions` for which pairs.
    """
    windows = os.name == "nt"
    paths = list(paths)  # read twice below, and by the caller's iterator otherwise
    problems = [why for p in paths if (why := unsafe_path(p, windows=windows))]
    problems += collisions(paths)
    problems += [
        "/".join(p.decode(errors="replace") for p in f[b"path"]) + ": a symlink"
        for f in info.get(b"files") or []
        if b"l" in f.get(b"attr", b"")
    ]
    return problems


def listing_problems(data, paths):
    """info_problems() for a .torrent's bytes."""
    return info_problems(lt.bdecode(data)[b"info"], paths)
