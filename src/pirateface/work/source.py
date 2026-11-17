"""Reads a file from Hugging Face at a pinned revision, as a stream of exact 16 KiB blocks."""

import hashlib
import http.client
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from pirateface.seed import hub
from pirateface.work.errors import WorkError

HF = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
ATTEMPTS = 3
# Waiting is the whole answer for these: the same set client/api.py treats as "an outage, not an answer".
WAIT_FOR = {408, 425, 429, 503}
MAX_WAIT = 300.0


class RateLimiter:
    """Paces streamed bytes to a shared maximum rate."""

    def __init__(self, bytes_per_second):
        self._bytes_per_second = bytes_per_second
        self._started = None
        self._bytes_read = 0

    def wait(self, count):
        if self._started is None:
            self._started = time.monotonic()
        self._bytes_read += count
        deadline = self._started + self._bytes_read / self._bytes_per_second
        delay = deadline - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def _wait(error, attempt):
    """Seconds to wait before trying again: Retry-After when Hugging Face sends one, else backoff."""
    header = getattr(error, "headers", None)
    after = header.get("Retry-After") if header is not None else None
    if after:
        try:
            secs = float(after)
        except (TypeError, ValueError):
            secs = 0.0  # an HTTP date, or junk
        if secs > 0:
            return min(secs, MAX_WAIT)
    return float(2**attempt)


def _open(url, headers, path, offset=0):
    """(response, the file's full length or None), starting at offset. Retries the connection; a 4xx other
    than a waitable one is raised at once, since retrying will not change it."""
    if offset:
        headers = headers | {"Range": f"bytes={offset}-"}
    last = None
    for attempt in range(ATTEMPTS):
        try:
            resp = hub.urlopen(urllib.request.Request(url, headers=headers), timeout=60)
        except urllib.error.HTTPError as e:
            if e.code not in WAIT_FOR and 400 <= e.code < 500:  # 401/403/404: retrying won't change it
                raise WorkError(f"{path}: Hugging Face refused the request ({e.code})") from e
            last = e
            if attempt < ATTEMPTS - 1:  # the last attempt has nothing left to wait for
                time.sleep(_wait(e, attempt))
            continue
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            if attempt < ATTEMPTS - 1:
                time.sleep(2**attempt)
            continue
        if offset and resp.status != 206:
            resp.close()
            raise WorkError(f"{path}: Hugging Face would not resume the download at byte {offset}")
        length = resp.headers.get("Content-Length")
        return resp, (offset + int(length) if length else None)
    if isinstance(last, urllib.error.HTTPError) and last.code in WAIT_FOR:
        why = "is rate limiting this run" if last.code == 429 else "is temporarily unavailable"
        raise WorkError(f"{path}: Hugging Face {why} ({last.code}) after {ATTEMPTS} attempts") from last
    raise WorkError(f"could not open {path} on Hugging Face: {last}")


class Stream:
    """One file. read(n) returns exactly n bytes until the end (the builder hashes each read as one
    16 KiB leaf), hashes what it returns, and reports the total when it ends. A connection that drops
    part way is reopened where it stopped (HTTP Range), so a long job survives a reset."""

    def __init__(self, url, headers, path, on_read, on_end, rate_limiter=None):
        self.url, self.headers, self.path, self.on_read, self.on_end = url, headers, path, on_read, on_end
        self.rate_limiter = rate_limiter
        self.sha, self.size, self.ended, self.drops = hashlib.sha256(), 0, False, 0
        self.resp, self.length = _open(url, headers, path)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.resp.close()

    def _reopen(self, at, error):
        self.drops += 1
        if self.drops > ATTEMPTS:
            raise WorkError(f"{self.path}: the connection to Hugging Face kept dropping ({error or 'ended early'})")
        self.resp.close()
        time.sleep(2**self.drops)
        self.resp, self.length = _open(self.url, self.headers, self.path, at)

    def read(self, n):
        buf = bytearray()
        while len(buf) < n:
            error = None
            try:
                part = self.resp.read(n - len(buf))
            except (OSError, http.client.HTTPException) as e:  # reset, timeout, IncompleteRead
                part, error = b"", e
            if part:
                buf += part
                self.drops = 0
                if self.rate_limiter is not None:
                    self.rate_limiter.wait(len(part))
                continue
            at = self.size + len(buf)
            if error is None and (self.length is None or at >= self.length):
                break  # the real end of the file
            self._reopen(at, error)
        if buf:
            self.size += len(buf)
            self.sha.update(buf)
            self.on_read(len(buf))
        elif not self.ended:
            self.ended = True
            self.on_end(self.path, self.sha.hexdigest(), self.size)
        return bytes(buf)


def open_file(repo, revision, path, on_read, on_end, rate_limiter=None):
    """A Stream for one file."""
    url = f"{HF}/{repo}/resolve/{urllib.parse.quote(revision)}/{urllib.parse.quote(path)}"
    return Stream(url, hub.headers(), path, on_read, on_end, rate_limiter)
