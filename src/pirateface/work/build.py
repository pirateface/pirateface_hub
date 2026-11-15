"""Builds one job's hybrid torrent by streaming its files, and checks the result against the job."""

import sys
import time

from pirateface.seed import hybrid
from pirateface.work import site, source
from pirateface.work.errors import WorkError

RENEW_SECONDS = 180  # the site drops a lease after 10 minutes without a renewal
REPORT_BYTES = 1 << 30


class Lease:
    """Renews the lease while files stream, so a long model keeps its job."""

    def __init__(self, key, lease_id):
        self.key, self.id, self.renewed = key, lease_id, time.monotonic()

    def renew_if_due(self):
        if time.monotonic() - self.renewed < RENEW_SECONDS:
            return
        status, body = site.renew(self.key, self.id)
        if status != 200:
            raise WorkError(f"lease lost ({status}): {(body or {}).get('error', 'request failed')}")
        self.renewed = time.monotonic()


def build(job, key, trackers):
    """Stream every file of the job, build the hybrid torrent. Returns (.torrent bytes, manifest, v1 infohash)."""
    repo, revision = job["repoId"], job["revision"]
    files = sorted(job["files"], key=lambda f: [s.encode() for s in f["path"].split("/")])
    total = sum(f["size"] for f in files)
    lease = Lease(key, job["leaseId"])
    sums, progress = {}, {"bytes": 0, "next": REPORT_BYTES}

    def on_read(n):
        lease.renew_if_due()
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
    for f in files:
        digest, size = sums.get(f["path"], (None, None))
        if size != f["size"]:
            raise WorkError(f"{f['path']} is {size} bytes from Hugging Face, not {f['size']}.")
        if f.get("sha256") and digest != f["sha256"]:
            raise WorkError(f"{f['path']} does not match its LFS SHA-256 at {revision[:12]}.")
    manifest = [{"path": f["path"], "sha256": sums[f["path"]][0]} for f in files]
    return data, manifest, v1
