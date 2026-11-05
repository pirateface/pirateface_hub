"""The user's torrent client: Transmission (RPC) or qBittorrent (Web UI)."""

import base64
import contextlib
import http.cookiejar
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

HELP = (
    "Transmission: its remote access (RPC) must be on (TR_HOST, TR_AUTH=user:pass). "
    "qBittorrent: the Web UI must be on (QBT_URL, QBT_USER, QBT_PASS)."
)


class Transmission:
    name = "Transmission"

    def __init__(self):
        host = os.environ.get("TR_HOST") or "localhost:9091"
        url = host if host.startswith("http") else f"http://{host}"
        self.url = url.rstrip("/") + ("" if url.endswith("/rpc") else "/transmission/rpc")
        self.headers = {"Content-Type": "application/json"}
        if os.environ.get("TR_AUTH"):
            self.headers["Authorization"] = "Basic " + base64.b64encode(os.environ["TR_AUTH"].encode()).decode()
        self.version = self.rpc("session-get", {"fields": ["rpc-version"]}).get("rpc-version", 0)

    def rpc(self, method, args):
        body = json.dumps({"method": method, "arguments": args}).encode()
        for _ in range(2):
            try:
                req = urllib.request.Request(self.url, body, self.headers)
                with urllib.request.urlopen(req, timeout=30) as r:
                    out = json.load(r)
                if out.get("result") != "success":
                    raise RuntimeError(out.get("result"))
                return out["arguments"]
            except urllib.error.HTTPError as e:
                if e.code != 409:
                    raise
                self.headers["X-Transmission-Session-Id"] = e.headers["X-Transmission-Session-Id"]
        raise RuntimeError("no Transmission session id")

    def has(self, v1):
        return bool(self.rpc("torrent-get", {"ids": [v1], "fields": ["id"]})["torrents"])

    def add(self, data, save_dir):
        self.rpc("torrent-add", {"metainfo": base64.b64encode(data).decode(), "download-dir": str(save_dir)})

    def update_trackers(self, mine):
        added = current = skipped = 0
        changed = []
        for t in self.rpc("torrent-get", {"fields": ["id", "trackers"]})["torrents"]:
            trackers = t["trackers"]
            urls = [x["announce"] for x in trackers]
            if not is_pirateface(urls):
                skipped += 1
                continue
            if mine in urls:
                current += 1
                continue
            keep = [x for x in trackers if not KEYED.match(x["announce"])]  # a key replaced at /account
            if self.version >= 17:  # Transmission 4: trackerList, one tier per blank-line block
                tiers = {}
                for x in keep:
                    tiers.setdefault(x["tier"], []).append(x["announce"])
                lines = "\n\n".join("\n".join(v) for _, v in sorted(tiers.items()))
                self.rpc("torrent-set", {"ids": [t["id"]], "trackerList": lines + "\n\n" + mine})
            else:
                old = [x["id"] for x in trackers if KEYED.match(x["announce"])]
                if old:
                    self.rpc("torrent-set", {"ids": [t["id"]], "trackerRemove": old})
                self.rpc("torrent-set", {"ids": [t["id"]], "trackerAdd": [mine]})
            added += 1
            changed.append(t["id"])
        if changed:
            self.rpc("torrent-reannounce", {"ids": changed})
        return added, current, skipped, ""


class QBittorrent:
    name = "qBittorrent"

    def __init__(self):
        self.base = (os.environ.get("QBT_URL") or "http://localhost:8080").rstrip("/")
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.op.addheaders = [("Referer", self.base)]
        # No login needed when "Bypass authentication for clients on localhost" is on.
        if os.environ.get("QBT_USER"):
            self.call("auth/login", {"username": os.environ["QBT_USER"], "password": os.environ.get("QBT_PASS", "")})
        self.call("app/version")

    def call(self, path, form=None, body=None, content_type=None):
        if form is not None:
            body = urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(self.base + "/api/v2/" + path, body)
        if content_type:
            req.add_header("Content-Type", content_type)
        with self.op.open(req, timeout=30) as r:
            return r.read().decode()

    def has(self, v1):
        # qBittorrent names a hybrid torrent by its v2 hash; the v1 one is in infohash_v1.
        return any(v1 in (t.get("hash"), t.get("infohash_v1")) for t in json.loads(self.call("torrents/info")))

    def add(self, data, save_dir):
        b = uuid.uuid4().hex
        body = (
            (
                f'--{b}\r\nContent-Disposition: form-data; name="savepath"\r\n\r\n{save_dir}\r\n'
                f'--{b}\r\nContent-Disposition: form-data; name="torrents"; filename="model.torrent"\r\n'
                "Content-Type: application/x-bittorrent\r\n\r\n"
            ).encode()
            + data
            + f"\r\n--{b}--\r\n".encode()
        )
        if self.call("torrents/add", body=body, content_type=f"multipart/form-data; boundary={b}").strip() == "Fails.":
            raise RuntimeError("qBittorrent did not take the torrent")

    def update_trackers(self, mine):
        # Announce to every tracker in every tier. qBittorrent's default stops at the first
        # tracker that answers, so the seeding tracker would never hear from it.
        prefs = json.loads(self.call("app/preferences"))
        extra = ""
        if not (prefs.get("announce_to_all_trackers") and prefs.get("announce_to_all_tiers")):
            prefs = {"announce_to_all_trackers": True, "announce_to_all_tiers": True}
            self.call("app/setPreferences", {"json": json.dumps(prefs)})
            extra = " Turned on Options > Advanced > Always announce to all trackers in a tier / to all tiers."
        added = current = skipped = 0
        changed = []
        for t in json.loads(self.call("torrents/info")):
            h = t["hash"]
            urls = [x["url"] for x in json.loads(self.call("torrents/trackers?hash=" + h)) if "://" in x["url"]]
            if not is_pirateface(urls):
                skipped += 1
                continue
            if mine in urls:
                current += 1
                continue
            old = [u for u in urls if KEYED.match(u)]
            if old:
                self.call("torrents/removeTrackers", {"hash": h, "urls": "|".join(old)})
            self.call("torrents/addTrackers", {"hash": h, "urls": mine})
            added += 1
            changed.append(h)
        if changed:
            self.call("torrents/reannounce", {"hashes": "|".join(changed)})
        return added, current, skipped, extra


PIRATEFACE = re.compile(r"^(udp|https?)://tracker\.pirateface\.co[:/]")
KEYED = re.compile(r"^https?://tracker\.pirateface\.co:6970/[0-9a-f]+/announce$")


def is_pirateface(urls):
    return any(PIRATEFACE.match(u) for u in urls)


def running():
    """The clients that answer."""
    found = []
    for client in (Transmission, QBittorrent):
        with contextlib.suppress(Exception):
            found.append(client())
    return found


def add(data, save_dir, v1):
    """Add the torrent to the first client that answers. Returns what happened, or None if none could."""
    for client in running():
        with contextlib.suppress(Exception):
            if client.has(v1):
                return f"Already in {client.name}."
            client.add(data, save_dir)
            return f"Added to {client.name} with the files in {save_dir}. It checks them, then seeds."
    return None


def update_trackers(mine):
    clients = running()
    if not clients:
        raise RuntimeError("no torrent client answered. " + HELP)
    for client in clients:
        added, current, skipped, extra = client.update_trackers(mine)
        print(f"    {client.name}: added to {added}, already had it {current}, not Pirate Face {skipped}.{extra}")
