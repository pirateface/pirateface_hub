"""Builds one job's hybrid torrent by streaming its files, and checks the result against the job."""

import threading
import time

from pirateface import ui
from pirateface.seed import hybrid
from pirateface.work import site, source, state
from pirateface.work.errors import LeaseLost, WorkError
from pirateface.work.resumable_hash import ResumableSHA256

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
                raise LeaseLost(
                    f"the site has been unreachable for {RENEW_UNREACHABLE_SECONDS // 60} minutes; "
                    "the lease has lapsed and the request is back in the queue"
                )
            return  # within the grace window: keep renewing on the timer
        raise LeaseLost(f"lease lost ({status}): {(body or {}).get('error', 'request failed')}")

    def note(self, n):
        self.done += n
        if self._error is not None:
            raise self._error

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)


def build(job, key, trackers, name=None, blocks=None, max_download_rate=None, *, saved=None):
    """Stream a job with piece checkpoints or file-boundary checkpoints."""
    repo, revision = job["repoId"], job["revision"]
    files = sorted(job["files"], key=lambda f: hybrid.torrent_order(f["path"].split("/")))
    total = sum(f["size"] for f in files)
    plen, have_proofs = hybrid.piece_length(total), blocks is not None
    done, partial = saved if saved is not None else state.load_progress(job, plen, have_proofs, name)
    try:
        ResumableSHA256()
        piece_checkpoints = True
    except RuntimeError:
        piece_checkpoints = False
        print("    Using file-boundary checkpoints (resumable hashing unavailable).")
    if partial and not piece_checkpoints:
        print("    Discarding unfinished piece progress; the interrupted file will be read again.")
        partial = None
        state.save_files(job, plen, have_proofs, done, name)
    if blocks is not None:
        if not done and partial is None and state.blocks_path(name).exists() and state.blocks_path(name).stat().st_size:
            print("    Discarding incomplete block hashes; no finished file was checkpointed.")
        kept = [(f["path"], -(-f["size"] // hybrid.BLOCK)) for f in done]
        if partial:
            kept.append((partial["path"], partial["offset"] // hybrid.BLOCK))
        blocks.keep(kept)
        if partial:
            blocks.verify_partial(partial["path"], bytes.fromhex(partial["roots"]), plen)
    lease = Lease(key, job["leaseId"], total, name)
    lease.renew()  # confirm we hold the lease before streaming
    lease.start()
    progress = ui.work_progress(total)
    rate_limiter = source.RateLimiter(max_download_rate) if max_download_rate is not None else None
    sums = {f["path"]: (f["sha256"], f["size"]) for f in done}

    def on_read(n):
        lease.note(n)
        progress.update(n)

    def on_reuse(n):
        lease.note(n)
        ui.work_reuse(progress, n)

    def on_end(path, digest, size):
        sums[path] = (digest, size)

    def on_file(path, hashes):
        nonlocal partial
        size, *rest = hashes
        done.append({"path": path, "size": size, "sha256": sums[path][0], "hashes": [h.hex() for h in rest]})
        partial = None
        if blocks is not None:
            blocks.flush()
        state.save_files(job, plen, have_proofs, done, name)

    sizes = {f["path"]: f["size"] for f in files}

    def on_piece(path, offset, v1, roots, stream):
        nonlocal partial
        if offset >= sizes[path]:
            return  # the finished-file checkpoint follows; avoid an EOF Range request
        partial = {
            "path": path,
            "offset": offset,
            "v1": v1.hex(),
            "roots": roots.hex(),
            "sha256": stream.sha.checkpoint().hex(),
        }
        if blocks is not None:
            blocks.flush()
        state.save_files(job, plen, have_proofs, done, name, partial=partial)

    def opener(path):
        if not piece_checkpoints:
            return source.open_file(repo, revision, path, on_read, on_end, rate_limiter)
        if partial and partial["path"] == path:
            offset = partial["offset"]
            sha = ResumableSHA256.restore(bytes.fromhex(partial["sha256"]), offset)
        else:
            offset, sha = 0, ResumableSHA256()
        return source.open_file(repo, revision, path, on_read, on_end, rate_limiter, offset, sha)

    try:
        if done or partial:
            print(f"    resuming: {len(done)} of {len(files)} files already hashed")
            on_reuse(sum(f["size"] for f in done) + (partial["offset"] if partial else 0))
        finished = {f["path"]: (f["size"], *map(bytes.fromhex, f["hashes"])) for f in done}
        sources = [(f["path"].split("/"), lambda f=f: opener(f["path"])) for f in files]
        data, v1, _v2 = hybrid.build_from(
            repo.split("/")[1],
            sources,
            trackers,
            plen,
            blocks=blocks,
            done=finished,
            on_file=on_file,
            partial=partial,
            on_piece=on_piece if piece_checkpoints else None,
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
