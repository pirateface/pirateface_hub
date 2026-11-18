"""pf work: the loop. Takes a job, builds it, submits it; --once takes one job and exits."""

import sys
import time

from pirateface import ui
from pirateface.client import api
from pirateface.config import API, MAX_UPLOAD_BYTES
from pirateface.seed import proofs, user
from pirateface.work import build, site, state
from pirateface.work.errors import Oversize, WorkError, WorkUnreachable

WORK_BACKOFF_MIN = 30  # seconds between lease polls while the site can't be reached
WORK_BACKOFF_MAX = 300  # cap: the site keeps a lease for 10 minutes, so a 5-minute pause is safe


def _consent():
    print(f"""
==> pf work: you build the torrent for one queued model at a time. Its files are streamed from
    Hugging Face through memory; no model bytes are written to disk. Only their 16 KiB block hashes
    are kept in a temporary file until the torrent is submitted. A private checkpoint with the lease
    and file metadata is saved so an interrupted run can resume. The torrent is listed on Pirate Face under your
    account. Verified work earns points: see {API}/how-it-works.
    Terms: {API}/terms#model-torrent-submissions""")
    if not user.YES and user.ask("    Type yes to start: ") != "yes":
        raise user.SeedError("not started")


def _job_size(job, max_bytes):
    sizes = [f.get("size") for f in job["files"]]
    if None in sizes:
        # Refused with or without a cap: a lease that does not say how big it is cannot be checked against one,
        # and it cannot even be totalled for the line printed below.
        raise Oversize(f"{job['repoId']}: the lease does not say how big its files are")
    total = sum(sizes)
    if max_bytes and total > max_bytes:
        # The site can lease a job past the cap it was asked for (4,970 GB for a 20 GiB cap, seen live), so the
        # client refuses it rather than streaming gigabytes nobody asked for. The lease is kept to lapse on its
        # own: given back, the same row would be offered again at once. This costs the wait, not the data.
        raise Oversize(
            f"{job['repoId']} is {total / 1e9:.2f} GB, over the {max_bytes / 1024**3:.0f} GiB cap this run "
            f"asked for: not downloaded. Raise --max-size, or leave it off, to take jobs this size"
        )
    return total


def _process_job(job, key, trackers, name, max_bytes=None, max_download_rate=None):
    total = _job_size(job, max_bytes)
    ui.aligned(
        [
            ("leased", job["repoId"]),
            ("revision", job["revision"][:12]),
            ("files", str(len(job["files"]))),
            ("size", f"{total / 1e9:.2f} GB"),
        ]
    )
    state.save(job, name)
    # A failed build raises after giving the lease back, so the next lease call takes a job at once.
    try:
        with proofs.sidecar() as blocks:
            data, manifest, infohash = build.build(job, key, trackers, name, blocks, max_download_rate)
            if len(data) > MAX_UPLOAD_BYTES:
                raise user.SeedError("the .torrent is over the 3 MB upload limit")
            block_proofs = proofs.answer(key, data, blocks)
    except WorkUnreachable:
        raise
    except (WorkError, user.SeedError):
        state.clear(name)
        site.release(key, job["leaseId"])  # best effort: unanswered, the lease lapses on its own
        raise
    status, answer = site.complete(key, job["leaseId"], data, manifest, block_proofs)
    if site.transient(status):
        raise WorkUnreachable(
            f"could not submit the torrent: {(answer or {}).get('error', 'the site could not be reached')}"
        )
    if status == 200:
        state.clear(name)
        # already_listed: the same files are listed under another infohash, which the site returns.
        if (answer or {}).get("status") == "already_listed":
            print(f"    Already listed: {API}/{job['repoId']} ({answer.get('infohash', '')})")
        else:
            ui.aligned([("listed", f"{API}/{job['repoId']}"), ("infohash", infohash)])
        return True
    state.clear(name)
    print(f"    Not listed ({status}): {(answer or {}).get('error', 'request failed')}")
    return False


def _work_once(key, trackers, name, max_bytes=None, max_download_rate=None):
    """Take one new job. Returns None if none was queued, else True if listed, False if refused."""
    status, job = site.lease(key, name, max_bytes)
    if status == 204:
        return None
    # 409: this lane still holds a lease (one being given back, or one that lapses soon). Ask again later.
    if status == 409 or site.transient(status):
        raise WorkUnreachable(f"lease refused ({status}): {(job or {}).get('error', 'request failed')}")
    if status != 200:
        raise user.SeedError(f"lease refused ({status}): {(job or {}).get('error', 'request failed')}")
    return _process_job(job, key, trackers, name, max_bytes, max_download_rate)


def _resume_once(key, trackers, name, saved, max_bytes=None, max_download_rate=None):
    """Renew and process the saved job. Returns None if the saved lease expired."""
    try:
        _job_size(saved["job"], max_bytes)
    except Oversize:
        state.clear(name)
        raise
    status, answer = site.renew(key, saved["leaseId"])
    if status == 410:
        state.clear(name)
        print("Saved lease expired; taking a new job.", file=sys.stderr)
        return None
    if site.transient(status):
        raise WorkUnreachable((answer or {}).get("error", f"lease renewal failed ({status})"))
    if status != 200:
        raise user.SeedError(f"could not resume lease ({status}): {(answer or {}).get('error', 'request failed')}")
    print(f"Resuming {saved['job'].get('repoId', 'saved job')}.")
    return _process_job(saved["job"], key, trackers, name, max_bytes, max_download_rate)


def run(args):
    user.YES = args.yes
    try:
        key = user.load_key()
        _consent()
    except user.SeedError as e:
        sys.exit(f"error: {e}")
    trackers = api.trackers(key)
    max_bytes = None if args.max_size is None else args.max_size * 1024**3
    max_download_rate = None if args.max_download_rate is None else args.max_download_rate * 1_000_000
    try:
        pending = state.load(args.name)
    except OSError as e:
        sys.exit(f"error: {e}")
    backoff = WORK_BACKOFF_MIN
    while True:
        try:
            if pending:
                listed = _resume_once(key, trackers, args.name, pending, max_bytes, max_download_rate)
                if listed is None:
                    pending = None
                    continue
                pending = None
            else:
                listed = _work_once(key, trackers, args.name, max_bytes, max_download_rate)
        except (WorkUnreachable, Oversize) as e:
            try:
                pending = state.load(args.name)
            except OSError as state_error:
                sys.exit(f"error: {state_error}")
            if args.once:
                sys.exit(f"error: {e}")
            # None is a failed job: the site is down or busy, this lane still holds a lease, or the site offered
            # a row this run won't take. Pause and ask again, doubling up to the cap.
            print(f"error: {e}. Checking again in {backoff} s.", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, WORK_BACKOFF_MAX)
            continue
        except WorkError as e:
            pending = None
            if args.once:
                sys.exit(f"error: {e}")
            # Ask for another job: the failed lease was given back.
            print(f"error: {e}. Taking the next job.", file=sys.stderr)
            continue
        except user.SeedError as e:
            sys.exit(
                f"error: {e}. The lease lapses after 10 minutes without renewal; the request then returns to the queue."
            )
        backoff = WORK_BACKOFF_MIN  # the site answered: reset the network-error backoff
        if listed is None:
            if args.once:
                print("No work is queued right now.")
                return 0
            print(f"Nothing queued. Checking again in {args.idle} s.")
            time.sleep(args.idle)
            continue
        if args.once:
            return 0 if listed else 1
