"""What pf seed and pf submit ask of the user: the -y flag, prompts, the account key, and the errors they stop with."""

import getpass
import os
import re
import sys
from pathlib import Path

from pirateface.client import api
from pirateface.config import API
from pirateface.seed import clients

# %APPDATA% on Windows, else $XDG_CONFIG_HOME or ~/.config (where package.sh kept it).
CONFIG = os.environ.get("APPDATA") if os.name == "nt" else os.environ.get("XDG_CONFIG_HOME")
KEY_FILE = Path(CONFIG or Path.home() / ".config") / "pirateface" / "key"
YES = False  # -y: never prompt. Set by command.run and restore.run.


class SeedError(Exception):
    pass


class RateLimited(SeedError):
    pass


class NeedsYou(SeedError):
    """-y and no torrent client: nothing was submitted; the user has to open the .torrent."""

    def __init__(self, repo, torrent_path, save_dir):
        super().__init__(f"no torrent client answered. {clients.HELP}")
        self.repo, self.torrent_path, self.save_dir = repo, torrent_path, save_dir


def ask(prompt, hidden=False):
    if YES:
        raise SeedError(f"needs an answer to {prompt.strip()!r}; rerun without -y")
    if not sys.stdin.isatty():
        raise SeedError("no terminal for prompts (agents: pass -y with the user's OK)")
    return (getpass.getpass if hidden else input)(prompt).strip()


def load_key():
    """Account key from /account, saved for next time."""
    key = read_key()
    if key:
        return key
    if YES:
        raise SeedError(f"set PIRATEFACE_KEY (from {API}/settings)")
    print(f"\n==> Your Pirate Face key: {API}/settings -> Community torrent key")
    key = ask("    Paste it (hidden): ", hidden=True)
    if not key.startswith("pfct_"):
        raise SeedError("that doesn't look like a Pirate Face key (starts with pfct_)")
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.touch(mode=0o600)
    KEY_FILE.write_text(key + "\n")
    print(f"    Saved to {KEY_FILE}")
    return key


def read_key():
    """The saved account key, or None. Never prompts."""
    return os.environ.get("PIRATEFACE_KEY") or (KEY_FILE.read_text().strip() if KEY_FILE.exists() else "") or None


SEEDING_TRACKER = re.compile(r"^https?://[^ ]+:6970/[^ ]+/announce$")


def seeding_tracker():
    """This account's keyed seeding tracker (the :6970 URL /api/trackers returns last), or None when
    there is no saved key or the site did not answer. Trackers are outside the info dict, so adding one
    never changes an infohash or re-downloads anything."""
    key = read_key()
    if not key:
        return None
    try:
        mine = api.trackers(key)[-1]
    except Exception:
        return None
    return mine if SEEDING_TRACKER.fullmatch(mine) else None
