import atexit
import contextlib
import json
import threading
import urllib.request

from pirateface.config import API, flag

_threads = []


def _send(repo_id):
    req = urllib.request.Request(
        f"{API}/api/rescue",
        data=json.dumps({"repoId": repo_id}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with contextlib.suppress(Exception):
        urllib.request.urlopen(req, timeout=10).close()


def request(repo_id):
    if flag("PF_NO_RESCUE"):
        return False
    t = threading.Thread(target=_send, args=(repo_id,), daemon=True)
    t.start()
    _threads.append(t)
    return True


@atexit.register
def _wait():
    for t in _threads:
        t.join(timeout=2)
