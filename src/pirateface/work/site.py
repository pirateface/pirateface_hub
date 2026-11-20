"""HTTP calls to the Pirate Face work endpoints: lease, renew, release and complete a job."""

import base64
import http.client
import json
import re

from pirateface.client import api
from pirateface.config import API

LEASE_ID = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.IGNORECASE)


def request(path, key, body, timeout=120):
    """(HTTP status, JSON answer or None). Errors come back as statuses, not exceptions: 0 when the
    site could not be reached at all."""
    try:
        return api.post(key, path, json.dumps(body).encode(), timeout, parse=lambda b: json.loads(b) if b else None)
    except (OSError, http.client.HTTPException) as e:  # DNS, refused, reset, timeout, truncated answer
        return 0, {"error": f"could not reach {API} ({e})"}


def transient(status):
    """A status worth asking again after a pause: no answer, a timeout, rate limiting or a server error."""
    return status == 0 or status in {408, 425, 429} or status >= 500


def held_lease_id(answer):
    """The lease id from a structured 409 answer, if present and valid."""
    if not isinstance(answer, dict):
        return None
    lease_id = answer.get("leaseId")
    return lease_id if isinstance(lease_id, str) and LEASE_ID.fullmatch(lease_id) else None


def lease(key, name=None, max_bytes=None):
    """Take one job. 200 with the job, 204 when nothing is queued. name is an optional lane label.
    max_bytes caps the model size in bytes (0 = no cap); absent means the server's default cap."""
    body = {}
    if name:
        body["name"] = name
    if max_bytes is not None:
        body["maxBytes"] = max_bytes
    return request("/api/work/lease", key, body)


def renew(key, lease_id, bytes_done=None, bytes_total=None, name=None):
    """Keep a lease. bytes_done and bytes_total are the progress the rescue board draws."""
    body = {"leaseId": lease_id}
    if name:
        body["name"] = name
    if bytes_done is not None:
        body["bytesDone"] = int(bytes_done)
        body["bytesTotal"] = int(bytes_total)
    return request("/api/work/lease", key, body, timeout=30)


def release(key, lease_id):
    """Give a lease back after a failed build, so the lane can lease again at once instead of after it lapses."""
    return request("/api/work/lease", key, {"leaseId": lease_id, "release": True}, timeout=30)


def complete(key, lease_id, torrent, manifest, block_proofs=None):
    """Post the .torrent (bytes) and the per-file SHA-256 manifest for a job, plus the block proof fields
    from proofs.answer()."""
    body = {"leaseId": lease_id, "torrent": base64.b64encode(torrent).decode(), "manifest": manifest}
    body |= block_proofs or {}
    return request("/api/work/complete", key, body)
