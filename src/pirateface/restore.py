"""pf restore: upload .torrent files to restore missing metadata on Pirate Face."""

import sys
from pathlib import Path

from pirateface.client import api
from pirateface.seed import command

MAX_TORRENT_BYTES = 3_000_000


def _collect(paths):
    seen, files = set(), []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            candidates = sorted(p.rglob("*.torrent"))
        elif p.is_file():
            candidates = [p]
        else:
            print(f"not found: {raw}", file=sys.stderr)
            continue
        for c in candidates:
            rp = c.resolve()
            if rp not in seen:
                seen.add(rp)
                files.append(c)
    return files


def run(args):
    command.YES = args.yes
    files = _collect(args.paths)
    if not files:
        sys.exit("error: no .torrent files found")
    try:
        key = command.load_key()
    except command.SeedError as e:
        sys.exit(f"error: {e}")

    for path in files:
        data = path.read_bytes()
        if len(data) > MAX_TORRENT_BYTES:
            print(f"too large (>3 MB): {path}")
            continue
        status, body = api.restore(key, data)
        if status == 200:
            print(f"restored: {path.name} -> {body.get('repoId', '')} ({body.get('infohash', '')})")
        elif status == 404:
            print(f"not listed: {path.name}")
        elif status == 409:
            print(f"already has metadata: {path.name}")
        elif status in (401, 403):
            sys.exit(f"error: {body.get('error', 'authentication failed')}")
        else:
            print(f"error ({status}): {path.name} - {body.get('error', 'request failed')}")
