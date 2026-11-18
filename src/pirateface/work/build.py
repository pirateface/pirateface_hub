"""Builds one job's hybrid torrent by streaming its files, and checks the result against the job."""

import threading
import time

from pirateface import ui
from pirateface.seed import hybrid
from pirateface.work import site, source
from pirateface.work.errors import WorkError

RENEW_SECONDS = 10  # the rescue board reads this; the site drops a lease after 10 minutes without one
RENEW_UNREACHABLE_SECONDS = 300  # half the lease lapse: beyond this a lost lease can't be renewed


class Lease:
    """Renews the lease from a daemon thread every RENEW_SECONDS, so the stream's reads never block
    on the HTTPS call, and reports bytes so the rescue board can draw the lane. A lost lease stops the
    job: the thread records the error and the next note() raises it. A site that can't be reached is
    not a lost lease: renewals keep trying until the outage passes RENEW_UNREACHABLE_SECONDS."""

    def __init__(self, key, lease_id, total, name=None):
        self.key, self.id, self.total, self.done, self.name = key, lease_id, total, 0, name
        self._error = None
        self._down_since = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._renew_loop, daemon=True)

    def _renew_loop(self):
        while not self._stop.wait(RENEW_SECONDS):
            try:
                self.renew()
            except WorkError as e:
                self._error = e
                return

    def renew(self):
        """Renew the lease now: the thread on its timer, build once at each end of the job. A network
        error (status 0) is only fatal once the site has been unreachable for RENEW_UNREACHABLE_SECONDS;
        any real non-200 answer means the lease is gone."""
        status, body = site.renew(self.key, self.id, self.done, self.total, self.name)
        if status == 200:
            self._down_since = None
            return
        if status == 0:
            if self._down_since is None:
                self._down_since = time.monotonic()
            if time.monotonic() - self._down_since > RENEW_UNREACHABLE_SECONDS:
                raise WorkError(
                    f"the site has been unreachable for {RENEW_UNREACHABLE_SECONDS // 60} minutes; "
                    "the lease has lapsed and the request is back in the queue"
                )
            return  # within the grace window: keep renewing on the timer
        raise WorkError(f"lease lost ({status}): {(body or {}).get('error', 'request failed')}")

    def note(self, n):
        self.done += n
        if self._error is not None:
            raise self._error

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)


def build(job, key, trackers, name=None, blocks=None, max_download_rate=None):
    """Stream every file of the job, build the hybrid torrent. Returns (.torrent bytes, manifest, v1 infohash).
    blocks: a proofs.BlockHashes to keep the block hashes in, or None."""
    repo, revision = job["repoId"], job["revision"]
    files = sorted(job["files"], key=lambda f: hybrid.torrent_order(f["path"].split("/")))
    total = sum(f["size"] for f in files)
    lease = Lease(key, job["leaseId"], total, name)
    lease.renew()  # confirm we hold the lease before streaming
    lease.start()
    sums = {}
    progress = ui.work_progress(total)
    rate_limiter = source.RateLimiter(max_download_rate) if max_download_rate is not None else None

    def on_read(n):
        lease.note(n)
        progress.update(n)

    def on_end(path, digest, size):
        sums[path] = (digest, size)

    sources = [
        (
            f["path"].split("/"),
            lambda f=f: source.open_file(repo, revision, f["path"], on_read, on_end, rate_limiter),
        )
        for f in files
    ]
    try:
        data, v1, _v2 = hybrid.build_from(
            repo.split("/")[1], sources, trackers, hybrid.piece_length(total), blocks=blocks
        )
    finally:
        lease.stop()
        progress.close()
    lease.renew()  # the last renewal before the result is submitted
    for f in files:
        digest, size = sums.get(f["path"], (None, None))
        if size != f["size"]:
            raise WorkError(f"{f['path']} is {size} bytes from Hugging Face, not {f['size']}.")
        if f.get("sha256") and digest != f["sha256"]:
            raise WorkError(f"{f['path']} does not match its LFS SHA-256 at {revision[:12]}.")
    manifest = [{"path": f["path"], "sha256": sums[f["path"]][0]} for f in files]
    return data, manifest, v1
