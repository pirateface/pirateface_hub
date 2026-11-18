"""Private local lease checkpoints and paths for a work lane."""

import json
import os

from pirateface.seed import user

STATE_FILE = user.KEY_FILE.parent / "work-lease.json"


def _path(name, kind="lease", suffix=".json"):
    return STATE_FILE.with_name(f"work-{kind}{'' if name is None else '-' + name}{suffix}")


def _write(path, data):
    """Write JSON to path atomically (temp file + rename), readable by this user only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
            f.write("\n")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def load(name=None):
    path = _path(name)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise OSError(f"could not read saved work lease at {path}: {e}") from e
    if not isinstance(state, dict) or not isinstance(state.get("job"), dict) or not state.get("leaseId"):
        raise OSError(f"saved work lease at {path} is invalid; remove it to start fresh")
    return state


def save(job, name=None):
    _write(_path(name), {"leaseId": job["leaseId"], "job": job})


def clear(name=None):
    """Delete the saved lease without deleting piece progress."""
    _path(name).unlink(missing_ok=True)


def blocks_path(name=None):
    """Path of the old file-boundary block hashes, removed when a lane upgrades."""
    return _path(name, "files", ".blocks")
