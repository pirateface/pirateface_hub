"""Atomic storage for mid-file work checkpoints.

A checkpoint belongs to one specific pinned job and stores hashes only, never
model bytes.
"""

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path

from pirateface.seed import hybrid


class DifferentJob(ValueError):
    """A lane already has a checkpoint for another pinned job."""


def job_identity(job):
    """Hash the immutable job details, excluding lease and worker identifiers."""
    files = sorted((f["path"], f["size"], f.get("sha256")) for f in job["files"])
    plen = hybrid.piece_length(sum(f["size"] for f in job["files"]))
    data = json.dumps(["pf-progress-v1", job["repoId"], job["revision"], files, plen], separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


class ProgressStore:
    """A single-job SQLite checkpoint; transactions commit piece and state together."""

    def __init__(self, path, identity):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise OSError("progress checkpoint is not a regular file")
            if os.name != "nt" and file_stat.st_mode & 0o077:
                raise OSError("progress checkpoint must not be readable or writable by other users")
        finally:
            os.close(fd)
        self.db = sqlite3.connect(self.path)
        try:
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("PRAGMA synchronous=FULL")
            with self.db:
                self.db.execute("CREATE TABLE IF NOT EXISTS meta (identity TEXT NOT NULL)")
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS pieces (path TEXT NOT NULL, offset INTEGER NOT NULL, "
                    "length INTEGER NOT NULL, v1 BLOB NOT NULL, leaves BLOB NOT NULL, "
                    "sha_state BLOB NOT NULL, checksum BLOB NOT NULL, PRIMARY KEY(path, offset))"
                )
                existing = self.db.execute("SELECT identity FROM meta").fetchall()
                if not existing:
                    self.db.execute("INSERT INTO meta VALUES (?)", (identity,))
                elif existing != [(identity,)]:
                    raise DifferentJob("checkpoint belongs to a different model revision or file list")
        except BaseException:
            self.db.close()
            raise

    def append(self, path, offset, length, v1, leaves, sha_state):
        """Commit a piece atomically. A 32-byte final digest marks a completed file."""
        if offset < 0 or length <= 0 or len(v1) != 20:
            raise ValueError("invalid checkpoint piece")
        if len(leaves) != ((length + 16383) // 16384) * 32 or len(sha_state) not in (32, 40):
            raise ValueError("invalid checkpoint hashes")
        if len(sha_state) == 40 and length % 16384:
            raise ValueError("a partial piece cannot carry intermediate hash state")
        with self.db:
            previous = self.db.execute(
                "SELECT COALESCE(MAX(offset + length), 0) FROM pieces WHERE path = ?", (path,)
            ).fetchone()[0]
            if offset != previous:
                raise ValueError("checkpoint pieces must be contiguous")
            checksum = self._checksum(path, offset, length, v1, leaves, sha_state)
            self.db.execute(
                "INSERT INTO pieces VALUES (?, ?, ?, ?, ?, ?, ?)",
                (path, offset, length, v1, leaves, sha_state, checksum),
            )

    @staticmethod
    def _checksum(path, offset, length, v1, leaves, sha_state):
        digest = hashlib.sha256()
        digest.update(json.dumps([path, offset, length], separators=(",", ":")).encode())
        digest.update(v1 + leaves + sha_state)
        return digest.digest()

    def pieces(self, path):
        rows = self.db.execute(
            "SELECT offset, length, v1, leaves, sha_state, checksum FROM pieces WHERE path = ? ORDER BY offset", (path,)
        )
        for offset, length, v1, leaves, sha_state, checksum in rows:
            if self._checksum(path, offset, length, v1, leaves, sha_state) != checksum:
                raise ValueError(f"corrupt checkpoint for {path}")
            yield offset, length, v1, leaves, sha_state

    def discard_file(self, path):
        """Forget a file's hashes when its complete digest fails validation."""
        with self.db:
            self.db.execute("DELETE FROM pieces WHERE path = ?", (path,))

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
