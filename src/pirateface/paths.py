"""Names Windows can't store, shared by the folder being packaged and the torrent listing being read."""

import re

# Names Windows can't store: reserved device names (with any extension), <>:"\\|?* and control
# characters, and a trailing dot or space.
WINDOWS_RESERVED = re.compile(r"(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\..*)?", re.IGNORECASE)
WINDOWS_INVALID = re.compile(r'[<>:"\\|?*\x00-\x1f]|[. ]$')
