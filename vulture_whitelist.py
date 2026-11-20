"""Names vulture reports as unused that code outside src/ uses. Only vulture reads this file ([tool.vulture])."""
# ruff: noqa: B018

from vulture.whitelist_utils import Whitelist

from pirateface import cli
from pirateface.client import api, torrent

_ = Whitelist()

cli.main  # entry point: [project.scripts] pf and pirateface
api._wait_for_rescues  # @atexit.register: runs at interpreter exit
torrent._cleanup  # @atexit.register: runs at interpreter exit
_.flags  # libtorrent add_torrent_params.flags, read by libtorrent (client/torrent.py)
_.addheaders  # urllib OpenerDirector.addheaders, read by urllib (seed/clients.py)
_.bar_format  # tqdm bar_format, read by tqdm on each redraw (ui.py)
_.initial  # tqdm's initial count, read when formatting the average rate (ui.py)
_.last_print_n  # tqdm's previous count, read when updating its rate (ui.py)
