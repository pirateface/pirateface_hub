"""Private local checkpoints for a lane, so pf work can resume after interruption: the leased job, and the files
of that job already hashed (file boundaries only: a file cut off part way is read again from its start)."""

import contextlib
import json
import os

from pirateface.seed import user
from pirateface.seed.hybrid import BLOCK

STATE_FILE = user.KEY_FILE.parent / "work-lease.json"


class CheckpointLocked(Exception):
    """Another process currently owns this worker's checkpoint."""


def _path(name, kind="lease", suffix=".json"):
    return STATE_FILE.with_name(f"work-{kind}{'' if name is None else '-' + name}{suffix}")


def lease_path(name=None):
    """Path to this worker's saved lease checkpoint, for user-facing recovery messages."""
    return _path(name)


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


def _problem(state):
    """What is wrong with this saved lease, or None when it is the shape the worker indexes. Every consumer
    of a loaded lease reads `leaseId`, `job["repoId"]` (as an owner/name pair), `job["revision"]` and
    `job["files"]` (each entry a dict with a `path`) straight off, so a checkpoint missing or mistyping one
    used to raise KeyError, TypeError, IndexError or AttributeError out of the worker loop as a traceback.
    A file's size may be absent or null, because _job_size() reads both as "this lease does not say how big
    its files are", which is a work error it clears and pauses on, not corruption. Any other size is refused
    unless it is a whole number of bytes: a string reaches sum() as a TypeError, and a bool or a float is
    not a byte count even though both sum cleanly."""
    if not isinstance(state, dict):
        return f"the body is {type(state).__name__}, not an object"
    lease = state.get("leaseId")
    if not isinstance(lease, str) or not lease:
        return "it has no string leaseId"
    job = state.get("job")
    if not isinstance(job, dict):
        return "job is missing or not an object"
    for field in ("leaseId", "revision"):
        if not isinstance(job.get(field), str):
            return f"job.{field} is missing or not a string"
    repo = job.get("repoId")
    if not isinstance(repo, str):
        return "job.repoId is missing or not a string"
    owner, _, name = repo.partition("/")
    if not owner or not name:
        return "job.repoId is not an owner/name pair"
    files = job.get("files")
    if not isinstance(files, list):
        return "job.files is missing or not a list"
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            return f"job.files[{index}] is {type(item).__name__}, not an object"
        if not isinstance(item.get("path"), str):
            return f"job.files[{index}].path is missing or not a string"
        # Null is left alone on purpose: _job_size() reads it as "this lease does not say how big its
        # files are", which is a work error it clears and pauses on. A present size must be a whole number
        # of bytes: a string raises TypeError in sum(), and bool or float sum fine but are not byte counts.
        size = item.get("size")
        if size is not None and (isinstance(size, bool) or not isinstance(size, int)):
            return f"job.files[{index}].size is {type(size).__name__}, not a whole number of bytes"
    return None


def load(name=None):
    path = _path(name)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise OSError(f"could not read saved work lease at {path}: {e}") from e
    problem = _problem(state)
    if problem:
        raise OSError(f"saved work lease at {path} is invalid ({problem}); remove it to start fresh")
    return state


@contextlib.contextmanager
def lock(name=None):
    """Exclusively claim a worker checkpoint until this process exits the context.

    Keep the lock file on disk: unlinking a locked file can let another process lock a new inode at the same path.
    """
    path = _path(name, "lock", ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise CheckpointLocked(f"could not acquire checkpoint lock at {path}") from error
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise CheckpointLocked(f"could not acquire checkpoint lock at {path}") from error
        locked = True
        yield
    finally:
        if locked:
            with contextlib.suppress(OSError):
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def save(job, name=None):
    _write(_path(name), {"leaseId": job["leaseId"], "job": job})


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
