"""Keep magnet index seeders on the newest weekly build. Each build is a new torrent (new infohash), but it holds
every earlier shard byte for byte, so the newest build added over an older build's folder only downloads the new
shards. The older builds stay in the client and keep seeding."""

from pirateface.client import api
from pirateface.config import API
from pirateface.index import INDEX_REPO, NAME, is_build
from pirateface.seed import clients, package


def add_newest(mine):
    """Add the newest build, announcing to mine too, next to the builds in each client that seeds one.
    Safe to re-run: a client that already has it is left alone."""
    try:
        data = api.torrent_file(INDEX_REPO)
    except api.Unreachable:  # the site is down: nothing to add this time
        data = None
    if not data:
        print(f"    Magnet index: could not get the newest build from {API}; trying again next run.")
        return
    data = package.with_trackers(data, [*api.TRACKERS, mine])
    _, v1, v2, _ = package.info(data)
    for client in clients.running():
        folders = [folder for folder, urls, paths in client.torrents_named(NAME) if is_build(urls, paths)]
        if not folders:
            continue
        if client.has(v1):
            print(f"    {client.name}: already seeds the newest magnet index build.")
            continue
        client.add(data, folders[0])
        if isinstance(client, clients.QBittorrent) and v2:
            client.force_start(v2)  # a full queue would leave it waiting and never fetch the new shards
        print(f"    {client.name}: added the newest magnet index build in {folders[0]}. It finds the shards you have.")
