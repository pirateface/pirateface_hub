"""Private local checkpoints for a lane, so pf work can resume after interruption: the leased job, and the files
of that job already hashed (file boundaries only: a file cut off part way is read again from its start)."""

import json
import os
import socket

from pirateface.seed import user
from pirateface.seed.hybrid import BLOCK

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


def find(lease_id):
    """Find a saved job by lease id, including checkpoints saved under another worker name.

    The third value is True/False when this host can tell whether its owner process is alive, or None when unknown.
    """
    for path in STATE_FILE.parent.glob("work-lease*.json"):
        try:
            saved = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(saved, dict) or saved.get("leaseId") != lease_id:
            continue
        if not isinstance(saved.get("job"), dict):
            raise OSError(f"saved work lease at {path} is invalid; remove it to start fresh")
        name = None if path == STATE_FILE else path.name.removeprefix("work-lease-").removesuffix(".json")
        pid, host = saved.get("pid"), saved.get("host")
        if not isinstance(pid, int) or isinstance(pid, bool) or host != socket.gethostname():
            return saved, name, None
        if pid == os.getpid():
            return saved, name, True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return saved, name, False
        except PermissionError:
            return saved, name, True
        except OSError:
            return saved, name, None
        return saved, name, True
    return None


def save(job, name=None):
    _write(
        _path(name),
        {"leaseId": job["leaseId"], "job": job, "pid": os.getpid(), "host": socket.gethostname()},
    )


def clear(name=None, files=True):
    """Delete the saved lease and, unless files is False, the finished files (kept when the same job may come back)."""
    _path(name).unlink(missing_ok=True)
    if files:
        _path(name, "files").unlink(missing_ok=True)
        blocks_path(name).unlink(missing_ok=True)


def blocks_path(name=None):
    """Where the finished files' 16 KiB block hashes are kept for the block proofs, 32 bytes each, in file order."""
    return _path(name, "files", ".blocks")


def _job_key(job, plen, proofs):
    return [job["repoId"], job["revision"], plen, proofs]


def load_files(job, plen, proofs, name=None):
    """The files an earlier run of this job finished, as saved by save_files(). [] when there are none, or when the
    checkpoint is unreadable or for another repo, revision, piece length or file list: it is then thrown away."""
    path = _path(name, "files")
    try:
        saved = json.loads(path.read_text())
        sizes = {f["path"]: f["size"] for f in job["files"]}
        done = saved["files"]
        if saved["job"] == _job_key(job, plen, proofs) and all(sizes.get(f["path"]) == f["size"] for f in done):
            need = 32 * sum(-(-f["size"] // BLOCK) for f in done)
            if not proofs or blocks_path(name).stat().st_size >= need:
                return done
    except (OSError, ValueError, KeyError, TypeError):
        pass
    path.unlink(missing_ok=True)
    return []


def save_files(job, plen, proofs, done, name=None):
    _write(_path(name, "files"), {"job": _job_key(job, plen, proofs), "files": done})
