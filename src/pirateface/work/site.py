"""HTTP calls to the Pirate Face work endpoints: lease, renew and complete a job."""

import base64
import json
import urllib.error
import urllib.request

from pirateface.client import api
from pirateface.config import API


def request(method, path, key, body=None, timeout=120):
    """(HTTP status, JSON answer or None). Errors come back as statuses, not exceptions."""
    req = urllib.request.Request(
        f"{API}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": f"pirateface/{api.VERSION}",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            return r.status, (json.loads(data) if data else None)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {"error": e.reason}


def lease(key, name=None, max_bytes=None):
    """Take one job. 200 with the job, 204 when nothing is queued. name is an optional lane label.
    max_bytes caps the model size in bytes (0 = no cap); absent means the server's default cap."""
    body = {}
    if name:
        body["name"] = name
    if max_bytes is not None:
        body["maxBytes"] = max_bytes
    return request("POST", "/api/work/lease", key, body)


def renew(key, lease_id, bytes_done=None, bytes_total=None, name=None):
    """Keep a lease. bytes_done and bytes_total are the progress the rescue board draws."""
    body = {"leaseId": lease_id}
    if name:
        body["name"] = name
    if bytes_done is not None:
        body["bytesDone"] = int(bytes_done)
        body["bytesTotal"] = int(0 if bytes_total is None else bytes_total)
    return request("POST", "/api/work/lease", key, body, timeout=30)


def complete(key, lease_id, torrent, manifest):
    """Post the .torrent (bytes) and the per-file SHA-256 manifest for a job."""
    return request(
        "POST",
        "/api/work/complete",
        key,
        {
            "leaseId": lease_id,
            "torrent": base64.b64encode(torrent).decode(),
            "manifest": manifest,
        },
    )
