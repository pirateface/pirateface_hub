"""pf work: the loop. Takes a job, builds it, submits it; --once takes one job and exits."""

import sys
import time

from pirateface.client import api
from pirateface.config import API
from pirateface.seed import user
from pirateface.work import build, site
from pirateface.work.errors import WorkError

MAX_TORRENT_BYTES = 3_000_000
LEASE_LAPSE = 600  # seconds the site keeps a failed job's lease before the request goes back to the queue


def _consent():
    print(f"""
==> pf work: you build the torrent for one queued model at a time. Its files are streamed from
    Hugging Face through memory and not saved. The torrent is listed on Pirate Face under your
    account. You earn 1 point per 10 GB of bytes this check verifies. Terms: {API}/terms#model-torrent-submissions""")
    if not user.YES and user.ask("    Type yes to start: ") != "yes":
        raise user.SeedError("not started")


def _work_once(key, trackers, name, max_bytes=None):
    """One job. Returns None if nothing was queued, else True if listed, False if the site refused it."""
    status, job = site.lease(key, name, max_bytes)
    if status == 204:
        return None
    if status == 0:  # the site could not be reached: network error, not a refusal
        raise WorkError(job["error"])
    if status != 200:
        raise user.SeedError(f"lease refused ({status}): {(job or {}).get('error', 'request failed')}")
    total = sum(f["size"] for f in job["files"])
    print(f"==> Leased {job['repoId']} at {job['revision'][:12]}: {len(job['files'])} files, {total / 1e9:.2f} GB")
    # A failed build raises: the lease stays with this account until it lapses (10 minutes), so a retry
    # now would be refused. The request then goes back to the queue.
    data, manifest, infohash = build.build(job, key, trackers, name)
    if len(data) > MAX_TORRENT_BYTES:
        raise user.SeedError("the .torrent is over the 3 MB upload limit")
    status, answer = site.complete(key, job["leaseId"], data, manifest)
    if status == 200:
        # already_listed: the same files are listed under another infohash, which the site returns.
        if (answer or {}).get("status") == "already_listed":
            print(f"    Already listed: {API}/{job['repoId']} ({answer.get('infohash', '')})")
        else:
            print(f"    Listed: {API}/{job['repoId']} ({infohash})")
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
    while True:
        try:
            listed = _work_once(key, trackers, args.name, max_bytes)
        except WorkError as e:
            if args.once:
                sys.exit(f"error: {e}. The lease lapses after 10 minutes; the request then returns to the queue.")
            # One failed job or one network blip should not stop a worker. Wait out the lease so the
            # next job is fresh, then keep going.
            print(f"error: {e}. Taking the next job in {LEASE_LAPSE // 60} minutes.", file=sys.stderr)
            time.sleep(LEASE_LAPSE)
            continue
        except user.SeedError as e:
            sys.exit(
                f"error: {e}. The lease lapses after 10 minutes without renewal; the request then returns to the queue."
            )
        if listed is None:
            if args.once:
                print("No work is queued right now.")
                return 0
            print(f"Nothing queued. Checking again in {args.idle} s.")
            time.sleep(args.idle)
            continue
        if args.once:
            return 0 if listed else 1
