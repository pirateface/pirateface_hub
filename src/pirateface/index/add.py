"""pf index --add: add every magnet in the cached index to qBittorrent with the user's seeding tracker on each.

Mirroring the catalogue used to mean adding each index magnet by hand and then `pf seed --update-trackers`.
This does it in one command: it mirrors the newest build (as `pf index --sync` does), reads the same magnets
the index lookup would answer with, and adds each to qBittorrent with the account's keyed tracker attached at
add time, so a full mirror earns seed hours. Client-only: no server change, older clients are unaffected.
"""

import urllib.parse

from pirateface.config import API
from pirateface.index import mirror
from pirateface.seed import clients, user
from pirateface.seed.user import SeedError

BATCH = 100  # magnets per add call, so 32k of them stay a handful of HTTP requests, not one huge body


def run(yes):
    user.YES = yes
    mirror.sync()  # get the current build, exactly what `pf index --sync` fetches
    magnets = mirror.magnets()
    if not magnets:
        print("the cached index lists no magnets; nothing to add.")
        return 0
    mine = user.seeding_tracker()
    if not mine:
        raise SeedError(f"could not get your seeding tracker from {API}/api/trackers (key rejected or site down)")
    try:
        qbt = clients.QBittorrent()
    except clients.LoginRefused as e:
        raise SeedError(str(e)) from e
    except Exception as e:
        raise SeedError(f"qBittorrent is not reachable; configure its Web UI ({clients.HELP})") from e
    present = qbt.all_infohashes()
    todo = [(magnet, infohash) for magnet, infohash in magnets if infohash.casefold() not in present]
    already = len(magnets) - len(todo)
    if not todo:
        print(f"added 0, already had {already}.")
        return 0
    print(f"==> Adding {len(todo)} magnets from the index build {mirror.build()} to qBittorrent")
    if (
        not user.YES
        and user.ask(
            f"    Each model downloads in full, so this needs a lot of disk. Type yes to add all {len(todo)}: "
        )
        != "yes"
    ):
        print("Nothing added.")
        return 1
    added = 0
    for start in range(0, len(todo), BATCH):
        qbt.add_magnets([_with_tracker(magnet, mine) for magnet, _ in todo[start : start + BATCH]])
        added = min(added + BATCH, len(todo))
        print(f"    added {added} of {len(todo)}")
    print(f"added {added}, already had {already}.")
    return 0


def _with_tracker(magnet, tracker):
    """The magnet announcing to `tracker` too, so the seeding tracker is attached at add time and the client
    never has to be touched again for it."""
    return magnet + "&tr=" + urllib.parse.quote(tracker, safe="")
