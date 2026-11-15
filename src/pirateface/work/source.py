"""Reads a file from Hugging Face at a pinned revision, as a stream of exact 16 KiB blocks."""

import hashlib
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from pirateface.client import api
from pirateface.work.errors import WorkError

HF = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
ATTEMPTS = 3


class Stream:
    """One file. read(n) returns exactly n bytes until the end (the builder hashes each read as one
    16 KiB leaf), hashes what it returns, and reports the total when it ends."""

    def __init__(self, resp, path, on_read, on_end):
        self.resp, self.path, self.on_read, self.on_end = resp, path, on_read, on_end
        self.sha, self.size, self.ended = hashlib.sha256(), 0, False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.resp.close()

    def read(self, n):
        buf = bytearray()
        while len(buf) < n:
            part = self.resp.read(n - len(buf))
            if not part:
                break
            buf += part
        if buf:
            self.size += len(buf)
            self.sha.update(buf)
            self.on_read(len(buf))
        elif not self.ended:
            self.ended = True
            self.on_end(self.path, self.sha.hexdigest(), self.size)
        return bytes(buf)


def open_file(repo, revision, path, on_read, on_end):
    """A Stream for one file. Retries the connection (not mid-stream failures)."""
    url = f"{HF}/{repo}/resolve/{urllib.parse.quote(revision)}/{urllib.parse.quote(path)}"
    headers = {"User-Agent": f"pirateface/{api.VERSION}"}
    if os.environ.get("HF_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["HF_TOKEN"]
    last = None
    for attempt in range(ATTEMPTS):
        try:
            resp = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60)
            return Stream(resp, path, on_read, on_end)
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(2**attempt)
    raise WorkError(f"could not open {path} on Hugging Face: {last}")
