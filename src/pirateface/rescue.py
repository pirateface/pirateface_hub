"""Ask Pirate Face to rescue a model it has no torrent for.

Sends only the model id to POST /api/rescue; Pirate Face's own workers build the torrent from
Hugging Face. Runs in the background and never raises: a failed request just means no rescue.
On by default in a terminal (PF_NO_RESCUE=1 turns it off); when stderr is not a terminal
(CI, Docker builds, logs) nobody sees the notice, so it only runs with PF_RESCUE=1.
"""

import atexit
import json
import os
import sys
import threading
import urllib.request

API = os.environ.get("PF_API", "https://pirateface.co")
_threads = []


def enabled():
    if os.environ.get("PF_NO_RESCUE") == "1":
        return False
    return sys.stderr.isatty() or os.environ.get("PF_RESCUE") == "1"


def _send(repo_id):
    req = urllib.request.Request(f"{API}/api/rescue", data=json.dumps({"repoId": repo_id}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception:
        pass


def request(repo_id):
    """Start the request in the background. Returns True if one was sent."""
    if not enabled():
        return False
    t = threading.Thread(target=_send, args=(repo_id,), daemon=True)
    t.start()
    _threads.append(t)
    return True


@atexit.register
def _wait():
    """A script that exits right away still gets its request out, for at most 2 s."""
    for t in _threads:
        t.join(timeout=2)
