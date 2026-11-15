"""Builds one job's hybrid torrent by streaming its files, and checks the result against the job."""

import sys
import time

from pirateface.seed import hybrid
from pirateface.work import site, source
from pirateface.work.errors import WorkError

RENEW_SECONDS = 10  # the rescue board reads this; the site drops a lease after 10 minutes without one
REPORT_BYTES = 1 << 30


class Lease:
    """Renews the lease while files stream, and reports bytes so the rescue board can draw the lane."""

    def __init__(self, key, lease_id, total, name=None):
        self.key, self.id, self.total, self.done, self.name = key, lease_id, total, 0, name
        self.renewed = 0.0

    def renew(self, force=False):
        if not force and time.monotonic() - self.renewed < RENEW_SECONDS:
            return
        status, body = site.renew(self.key, self.id, self.done, self.total, self.name)
        if status != 200:
            raise WorkError(f"lease lost ({status}): {(body or {}).get('error', 'request failed')}")
        self.renewed = time.monotonic()

    def note(self, n):
        self.done += n
        self.renew()


def build(job, key, trackers, name=None):
    """Stream every file of the job, build the hybrid torrent. Returns (.torrent bytes, manifest, v1 infohash)."""
    repo, revision = job["repoId"], job["revision"]
    files = sorted(job["files"], key=lambda f: [s.encode() for s in f["path"].split("/")])
    total = sum(f["size"] for f in files)
    lease = Lease(key, job["leaseId"], total, name)
    lease.renew(force=True)
    sums, progress = {}, {"bytes": 0, "next": REPORT_BYTES}

    def on_read(n):
        lease.note(n)
        progress["bytes"] += n
        if progress["bytes"] >= progress["next"]:
            progress["next"] += REPORT_BYTES
            print(f"    {progress['bytes'] / 1e9:.1f} of {total / 1e9:.1f} GB", file=sys.stderr)

    def on_end(path, digest, size):
        sums[path] = (digest, size)

    sources = [
        (f["path"].split("/"), lambda f=f: source.open_file(repo, revision, f["path"], on_read, on_end)) for f in files
    ]
    data, v1, _v2 = hybrid.build_from(repo.split("/")[1], sources, trackers, hybrid.piece_length(total))
    lease.renew(force=True)
    for f in files:
        digest, size = sums.get(f["path"], (None, None))
        if size != f["size"]:
            raise WorkError(f"{f['path']} is {size} bytes from Hugging Face, not {f['size']}.")
        if f.get("sha256") and digest != f["sha256"]:
            raise WorkError(f"{f['path']} does not match its LFS SHA-256 at {revision[:12]}.")
    manifest = [{"path": f["path"], "sha256": sums[f["path"]][0]} for f in files]
    return data, manifest, v1
