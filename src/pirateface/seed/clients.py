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


class LoginRefused(Exception):
    """The client answered but wouldn't let us in: worth telling the user, unlike a client that isn't running."""


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
                if e.code == 401:
                    raise LoginRefused("Transmission refused the login: check TR_AUTH (user:pass)") from e
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

    def torrents_named(self, name):
        """(save folder, tracker URLs, file paths inside the torrent) of each torrent called name."""
        fields = ["name", "downloadDir", "trackers", "files"]
        return [
            (
                t["downloadDir"],
                [x["announce"] for x in t["trackers"]],
                [f["name"].removeprefix(name + "/") for f in t["files"]],
            )
            for t in self.rpc("torrent-get", {"fields": fields})["torrents"]
            if t["name"] == name
        ]


class QBittorrent:
    name = "qBittorrent"

    def __init__(self):
        self.base = (os.environ.get("QBT_URL") or "http://localhost:8080").rstrip("/")
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.op.addheaders = [("Referer", self.base)]
        # No login needed when "Bypass authentication for clients on localhost" is on.
        if os.environ.get("QBT_USER"):
            answer = self.call(
                "auth/login", {"username": os.environ["QBT_USER"], "password": os.environ.get("QBT_PASS", "")}
            )
            if answer.strip() == "Fails.":  # a wrong password is a 200 "Fails.", not an HTTP error
                raise LoginRefused("qBittorrent refused the login: check QBT_USER and QBT_PASS")
        self.call("app/version")

    def call(self, path, form=None, body=None, content_type=None, text=True):
        if form is not None:
            body = urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(self.base + "/api/v2/" + path, body)
        if content_type:
            req.add_header("Content-Type", content_type)
        with self.op.open(req, timeout=30) as r:
            data = r.read()
        return data.decode() if text else data

    def torrents(self):
        return json.loads(self.call("torrents/info"))

    def trackers(self, h):
        return [x["url"] for x in json.loads(self.call("torrents/trackers?hash=" + h)) if "://" in x["url"]]

    def torrents_named(self, name):
        """(save folder, tracker URLs, file paths inside the torrent) of each torrent called name."""
        found = []
        for t in self.torrents():
            if t["name"] == name:
                files = json.loads(self.call("torrents/files?hash=" + t["hash"]))
                paths = [f["name"].removeprefix(name + "/") for f in files]
                found.append((t["save_path"], self.trackers(t["hash"]), paths))
        return found

    def export(self, h):
        """The .torrent file of a torrent in the client (qBittorrent 4.5 or later)."""
        return self.call("torrents/export?hash=" + h, text=False)

    def find(self, infohash):
        """The client's entry for the torrent, or None."""
        # qBittorrent ids a hybrid or v2 torrent by its v2 hash cut to 40 digits; full hashes are in infohash_v1/_v2.
        ids = ("hash", "infohash_v1", "infohash_v2")
        return next((t for t in self.torrents() if infohash in (t.get(k) for k in ids)), None)

    def has(self, infohash):
        return self.find(infohash) is not None

    def add(self, data, save_dir, skip_checking=False):
        b = uuid.uuid4().hex
        skip = f'--{b}\r\nContent-Disposition: form-data; name="skip_checking"\r\n\r\ntrue\r\n' if skip_checking else ""
        body = (
            (
                f'--{b}\r\nContent-Disposition: form-data; name="savepath"\r\n\r\n{save_dir}\r\n{skip}'
                f'--{b}\r\nContent-Disposition: form-data; name="torrents"; filename="model.torrent"\r\n'
                "Content-Type: application/x-bittorrent\r\n\r\n"
            ).encode()
            + data
            + f"\r\n--{b}--\r\n".encode()
        )
        if self.call("torrents/add", body=body, content_type=f"multipart/form-data; boundary={b}").strip() == "Fails.":
            raise RuntimeError("qBittorrent did not take the torrent")

    def force_start(self, v2):
        """Seed a v2 torrent even when the queue is full (qBittorrent queues past 3 active uploads by default).
        Its id here is the v2 infohash cut to 40 digits; the full one is ignored."""
        self.call("torrents/setForceStart", {"hashes": v2[:40], "value": "true"})

    def ensure_announce_all(self):
        """Announce to every tracker in every tier. qBittorrent's default stops at the first tracker that
        answers, so a keyed tracker added after the site ones would never hear from it. True if it changed."""
        prefs = json.loads(self.call("app/preferences"))
        if not (prefs.get("announce_to_all_trackers") and prefs.get("announce_to_all_tiers")):
            self.call(
                "app/setPreferences",
                {"json": json.dumps({"announce_to_all_trackers": True, "announce_to_all_tiers": True})},
            )
            return True
        return False

    def update_trackers(self, mine):
        extra = (
            " Turned on Options > Advanced > Always announce to all trackers in a tier / to all tiers."
            if self.ensure_announce_all()
            else ""
        )
        added = current = skipped = 0
        changed = []
        for t in self.torrents():
            h = t["hash"]
            urls = self.trackers(h)
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
    return [c for c in map(_connect, (Transmission, QBittorrent)) if c]


def _connect(client):
    """The client, or None if it isn't running. A client that refuses our login says so."""
    try:
        return client()
    except LoginRefused as e:
        print(f"    {e}")
        return None
    except Exception:  # not running, or remote access off
        return None


def add(data, save_dir, v1, prefer=None):
    """Add the torrent to the first client that answers, trying a running client of class prefer first.
    Returns what happened, or None if none could."""
    for client in sorted(running(), key=lambda c: not (prefer and isinstance(c, prefer))):
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
