"""Private local checkpoint for a leased work item, so pf work can resume after interruption."""

import json
import os

from pirateface.seed import user

STATE_FILE = user.KEY_FILE.parent / "work-lease.json"


def load():
    if not STATE_FILE.exists():
        return None
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise OSError(f"could not read saved work lease at {STATE_FILE}: {e}") from e
    if not isinstance(state, dict) or not isinstance(state.get("job"), dict) or not state.get("leaseId"):
        raise OSError(f"saved work lease at {STATE_FILE} is invalid; remove it to start fresh")
    return state


def save(job):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(f".{os.getpid()}.tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"leaseId": job["leaseId"], "job": job}, f)
            f.write("\n")
        temp.replace(STATE_FILE)
    finally:
        temp.unlink(missing_ok=True)


def clear():
    STATE_FILE.unlink(missing_ok=True)
