"""Private local lease and file-boundary checkpoints for a work lane."""

import hashlib
import json
import os

from pirateface.seed import user
from pirateface.seed.hybrid import BLOCK, torrent_order

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


def clear(name=None, files=False):
    """Delete the saved lease and, when requested, its file-boundary hashes."""
    _path(name).unlink(missing_ok=True)
    if files:
        clear_files(name)


def blocks_path(name=None):
    """Path of a lane's file-boundary block hashes."""
    return _path(name, "files", ".blocks")


def clear_files(name=None):
    """Remove file-boundary hashes without touching the lease or piece checkpoint."""
    _path(name, "files").unlink(missing_ok=True)
    blocks_path(name).unlink(missing_ok=True)


def _job_key(job, plen, proofs):
    files = sorted(
        ([f["path"], f["size"], f.get("sha256")] for f in job["files"]),
        key=lambda f: torrent_order(f[0].split("/")),
    )
    return [job["repoId"], job["revision"], plen, proofs, files]


def _partial_checksum(partial):
    values = [partial[k] for k in ("path", "offset", "v1", "roots", "sha256")]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def load_progress(job, plen, proofs, name=None):
    """Load completed files and the partial file; discard progress for a different job."""
    path = _path(name, "files")
    if not path.exists():
        return [], None
    try:
        saved = json.loads(path.read_text())
        done = saved["files"]
        expected = _job_key(job, plen, proofs)
        saved_key = saved["job"]
        if (
            isinstance(saved_key, list)
            and len(saved_key) == 4
            and isinstance(saved_key[0], str)
            and isinstance(saved_key[1], str)
            and isinstance(saved_key[2], int)
            and isinstance(saved_key[3], bool)
            and isinstance(done, list)
        ):
            clear_files(name)
            print("    Discarded the older file-only checkpoint; progress will be saved in the new format.")
            return [], None
        if not isinstance(saved_key, list) or len(saved_key) != 5:
            raise ValueError("checkpoint version cannot be validated")
        if (saved_key[0], saved_key[1], saved_key[4]) != (expected[0], expected[1], expected[4]):
            clear_files(name)
            print("    Discarded progress for a different model, revision or file list.")
            return [], None
        if saved_key != expected or not isinstance(done, list):
            raise ValueError("checkpoint uses a different piece length or proof configuration")
        entries = expected[-1]
        if len(done) > len(entries) or any(
            f["path"] != entry[0] or f["size"] != entry[1] for f, entry in zip(done, entries, strict=False)
        ):
            raise ValueError("saved files do not match the job")
        partial = saved.get("partial")
        if partial is not None:
            if len(done) >= len(entries) or not isinstance(partial, dict):
                raise ValueError("invalid partial-file checkpoint")
            if partial.get("checksum") != _partial_checksum(partial):
                raise ValueError("partial-file checkpoint checksum does not match")
            size = partial["offset"]
            if partial["path"] != entries[len(done)][0] or not isinstance(size, int):
                raise ValueError("partial file does not match the job")
            if size <= 0 or size >= entries[len(done)][1] or size % plen:
                raise ValueError("invalid partial-file offset")
            if len(bytes.fromhex(partial["v1"])) != (size // plen) * 20:
                raise ValueError("invalid partial v1 hashes")
            if len(bytes.fromhex(partial["roots"])) != (size // plen) * 32:
                raise ValueError("invalid partial v2 roots")
            if len(bytes.fromhex(partial["sha256"])) != 40:
                raise ValueError("invalid partial SHA-256 state")
        need = 32 * (sum(-(-f["size"] // BLOCK) for f in done) + (partial["offset"] // BLOCK if partial else 0))
        if proofs and need and blocks_path(name).stat().st_size < need:
            raise ValueError("saved block hashes are incomplete")
        return done, partial
    except (OSError, KeyError, TypeError, ValueError) as e:
        raise ValueError(f"could not use file checkpoint {path}: {e}; use --clear-progress to discard it") from e


def save_files(job, plen, proofs, done, name=None, partial=None):
    if partial is not None:
        partial = {**partial, "checksum": _partial_checksum(partial)}
    _write(_path(name, "files"), {"job": _job_key(job, plen, proofs), "files": done, "partial": partial})
