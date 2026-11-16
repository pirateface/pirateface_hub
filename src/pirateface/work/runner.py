"""pf work: the loop. Takes a job, builds it, submits it; --once takes one job and exits."""

import sys
import time

from pirateface import ui
from pirateface.client import api
from pirateface.config import API
from pirateface.seed import user
from pirateface.work import build, site
from pirateface.work.errors import WorkError, WorkUnreachable

MAX_TORRENT_BYTES = 3_000_000
WORK_BACKOFF_MIN = 30  # seconds between lease polls while the site can't be reached
WORK_BACKOFF_MAX = 300  # cap: the site keeps a lease for 10 minutes, so a 5-minute pause is safe


def _consent():
    print(f"""
==> pf work: you build the torrent for one queued model at a time. Its files are streamed from
    Hugging Face through memory and not saved. The torrent is listed on Pirate Face under your
    account. Verified work earns points: see {API}/how-it-works.
    Terms: {API}/terms#model-torrent-submissions""")
    if not user.YES and user.ask("    Type yes to start: ") != "yes":
        raise user.SeedError("not started")


def _work_once(key, trackers, name, max_bytes=None):
    """One job. Returns None if nothing was queued, else True if listed, False if the site refused it."""
    status, job = site.lease(key, name, max_bytes)
    if status == 204:
        return None
    if status == 0:  # the site could not be reached: network error, not a refusal
        raise WorkUnreachable(job["error"])
    if status != 200:
        raise user.SeedError(f"lease refused ({status}): {(job or {}).get('error', 'request failed')}")
    total = sum(f["size"] for f in job["files"])
    ui.aligned(
        [
            ("leased", job["repoId"]),
            ("revision", job["revision"][:12]),
            ("files", str(len(job["files"]))),
            ("size", f"{total / 1e9:.2f} GB"),
        ]
    )
    # A failed build raises. The failed job's row stays leased to this account until it lapses, so the
    # next lease call gets a different job; the request then goes back to the queue.
    data, manifest, infohash = build.build(job, key, trackers, name)
    if len(data) > MAX_TORRENT_BYTES:
        raise user.SeedError("the .torrent is over the 3 MB upload limit")
    status, answer = site.complete(key, job["leaseId"], data, manifest)
    if status == 0:  # the site could not be reached: the torrent is built but not submitted
        raise WorkError(f"could not submit the torrent: {answer.get('error', 'the site could not be reached')}")
    if status == 200:
        # already_listed: the same files are listed under another infohash, which the site returns.
        if (answer or {}).get("status") == "already_listed":
            print(f"    Already listed: {API}/{job['repoId']} ({answer.get('infohash', '')})")
        else:
            ui.aligned([("listed", f"{API}/{job['repoId']}"), ("infohash", infohash)])
        return True
    print(f"    Not listed ({status}): {(answer or {}).get('error', 'request failed')}")
    return False


def run(args):
    user.YES = args.yes
    try:
        key = user.load_key()
        _consent()
    except user.SeedError as e:
        sys.exit(f"error: {e}")
    trackers = api.trackers(key)
    max_bytes = None if args.max_size is None else args.max_size * 1024**3
    backoff = WORK_BACKOFF_MIN
    while True:
        try:
            listed = _work_once(key, trackers, args.name, max_bytes)
        except WorkUnreachable as e:
            if args.once:
                sys.exit(f"error: {e}")
            print(f"error: {e}. Checking again in {backoff} s.", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, WORK_BACKOFF_MAX)
            continue
        except WorkError as e:
            if args.once:
                sys.exit(f"error: {e}")
            # A failed job's row stays leased to this account until it lapses, so the next lease call
            # gets a different job: take it at once.
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
