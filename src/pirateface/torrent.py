"""One libtorrent session per process. Fetches single files out of Pirate Face torrents."""

import atexit
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import libtorrent as lt

from pirateface import rescue, ui

API = os.environ.get("PF_API", "https://pirateface.co")
DATA_DIR = Path(os.environ.get("PF_CACHE", Path.home() / ".cache" / "pirateface"))
METADATA_TIMEOUT = float(os.environ.get("PF_METADATA_TIMEOUT", 30))
STALL_TIMEOUT = float(os.environ.get("PF_STALL_TIMEOUT", 60))

_lock = threading.Lock()
_session = None
_handles = {}  # repo_id -> torrent_handle, or None if Pirate Face has no torrent
_wanted = {}  # repo_id -> file indexes asked for so far


class Unavailable(Exception):
    """Pirate Face can't supply this file; not an error."""


def _get_session():
    global _session
    if _session is None:
        debug = bool(os.environ.get("PF_DEBUG"))
        _session = lt.session({
            "listen_interfaces": os.environ.get("PF_LISTEN", "0.0.0.0:0,[::]:0"),  # any free port
            "user_agent": "pirateface/0.1",
            "alert_mask": lt.alert.category_t.all_categories if debug else lt.alert.category_t.storage_notification,
            # We fetch files one at a time, so the torrent goes "finished" between requests
            # and libtorrent drops the web seed. Reconnect in 2s instead of the default 30s.
            # Don't turn off close_redundant_connections: idle web seed connections then
            # stay open and never request the next file.
            "urlseed_wait_retry": 2,
        })
        threading.Thread(target=_pump_alerts, args=(_session, debug), daemon=True).start()
    return _session


# libtorrent can call a piece done while its bytes are still in its write buffer, so the file on disk
# can read as zeros. flush_cache() writes the buffer out and answers with a cache_flushed_alert. Those
# answers don't say which request they belong to, so only one flush is in flight at a time.
_flush = threading.Condition()
_flush_one_at_a_time = threading.Lock()
_flush_done = {}  # info hash -> cache_flushed_alerts received


def _pump_alerts(session, debug):
    """The only reader of libtorrent's alerts. PF_DEBUG=1 also prints its log, minus per-block chatter."""
    noisy = ("PIECE", "HAVE", "REQUEST", "picker", "ALLOWED_FAST", "dht", "portmap", "stats", "block")
    while True:
        session.wait_for_alert(1000)
        for a in session.pop_alerts():
            if isinstance(a, lt.cache_flushed_alert):
                with _flush:
                    key = str(a.handle.info_hashes().v1)
                    _flush_done[key] = _flush_done.get(key, 0) + 1
                    _flush.notify_all()
            if debug:
                msg = a.message()
                if not any(k in msg or k in type(a).__name__ for k in noisy):
                    print(f"[lt] {msg[:220]}", file=sys.stderr, flush=True)


@atexit.register
def _cleanup():
    """Hugging Face's cache has its own copy of every file we handed over, so ours can go."""
    for h in _handles.values():
        if h is None:
            continue
        folder = Path(h.status().save_path) / h.torrent_file().name()
        _session.remove_torrent(h)
        shutil.rmtree(folder, ignore_errors=True)


def _flush_to_disk(h, timeout=30):
    key = str(h.info_hashes().v1)
    with _flush_one_at_a_time, _flush:
        before = _flush_done.get(key, 0)
        h.flush_cache()
        if not _flush.wait_for(lambda: _flush_done.get(key, 0) > before, timeout):
            raise TimeoutError("libtorrent didn't confirm the file was written to disk")


NOT_LISTED = object()


def lookup_magnet(repo_id):
    """The magnet, NOT_LISTED if Pirate Face has no torrent for the repo, or None if we can't tell
    (network error, or a license whose terms have to be accepted first)."""
    url = f"{API}/api/torrents?" + urllib.parse.urlencode({"repo": repo_id})
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.load(r).get("magnet")
    except urllib.error.HTTPError as e:
        return NOT_LISTED if e.code == 404 else None
    except Exception:
        return None


def _handle(repo_id):
    """Add the repo's torrent (once) and wait for its file list."""
    with _lock:
        if repo_id in _handles:
            return _handles[repo_id]
        magnet = lookup_magnet(repo_id)
        if magnet is NOT_LISTED:
            ui.not_on_pirateface(repo_id, rescue.request(repo_id))
            _handles[repo_id] = None
            return None
        if not magnet:
            ui.not_on_pirateface(repo_id)
            _handles[repo_id] = None
            return None
        atp = lt.parse_magnet_uri(magnet)
        atp.save_path = str(DATA_DIR)
        # Every file starts as "skip": nothing downloads until a file is asked for.
        atp.flags |= lt.torrent_flags.default_dont_download
        # A magnet is only an ID; the file list has to come from a peer. Keep it once we have it.
        saved = DATA_DIR / "torrents" / f"{atp.info_hashes.v1}.torrent"
        if saved.exists():
            atp.ti = lt.torrent_info(str(saved))
        h = _get_session().add_torrent(atp)
        deadline = time.monotonic() + METADATA_TIMEOUT
        while not h.status().has_metadata:
            if time.monotonic() > deadline:
                _get_session().remove_torrent(h)
                _handles[repo_id] = None
                raise TimeoutError("no peer sent the file list in time")
            time.sleep(0.2)
        if not saved.exists():
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(lt.bencode({"info": lt.bdecode(h.torrent_file().info_section())}))
        _handles[repo_id] = h
        threading.Thread(target=_show_status, args=(repo_id, h), daemon=True).start()
        fs = h.torrent_file().files()
        real = [i for i in range(fs.num_files()) if not fs.file_flags(i) & fs.flag_pad_file]
        ui.banner(repo_id, len(real), sum(fs.file_size(i) for i in real))
        return h


def _show_status(repo_id, h, every=0.5, recent=3.0):
    """Draw the model's status line while any requested file is still downloading.

    Sources are whoever actually sent bytes in the last few seconds, not who is connected: the web seed
    reconnects for every file and redirect, so "connected" flickers several times a second."""
    fs = h.torrent_file().files()
    sent = {}  # (ip, port) -> bytes received from that connection so far
    last_seen = {}  # (ip, is web seed) -> time it last sent us bytes; a peer reconnecting still counts once
    bar = None
    while True:
        time.sleep(every)
        with _lock:
            wanted = list(_wanted.get(repo_id, ()))
        progress = h.file_progress(lt.torrent_handle.piece_granularity)
        total = sum(fs.file_size(i) for i in wanted)
        done = sum(min(progress[i], fs.file_size(i)) for i in wanted)
        files_done = sum(progress[i] >= fs.file_size(i) for i in wanted)
        if not wanted or files_done == len(wanted):
            if bar is not None:
                bar.close()
                bar = None
            continue
        now = time.monotonic()
        for p in h.get_peer_info():
            conn = tuple(p.ip)
            if p.total_download > sent.get(conn, 0):
                last_seen[(conn[0], p.connection_type != lt.peer_info.standard_bittorrent)] = now
            sent[conn] = p.total_download
        active = [web for (_, web), t in last_seen.items() if now - t < recent]
        label = ui.sources_label(any(active), active.count(False))
        if bar is None:
            bar = ui.repo_progress(repo_id)
        bar.total, bar.n = total, done
        ui.set_status(bar, f"{files_done}/{len(wanted)} files · {done / 1e6:.1f}/{total / 1e6:.1f} MB · {label} · {h.status().download_rate / 1e6:.1f} MB/s")


def _file_index(ti, filename):
    fs = ti.files()
    for i in range(fs.num_files()):
        path = fs.file_path(i).replace(os.sep, "/")
        rel = path.split("/", 1)[1] if fs.num_files() > 1 and "/" in path else path
        if rel == filename:
            return i
    return None


def fetch_file(repo_id, filename):
    """Download one file of the repo's torrent and return its path. Raises Unavailable if Pirate Face doesn't have it."""
    h = _handle(repo_id)
    if h is None:
        raise Unavailable()
    ti = h.torrent_file()
    i = _file_index(ti, filename)
    if i is None:
        raise Unavailable("not in the torrent")
    size = ti.files().file_size(i)
    path = Path(h.status().save_path) / ti.files().file_path(i)
    # Want only the files asked for so far. Set them all in one call: a moment where
    # every file is skipped marks the torrent "finished" and drops the web seed.
    # Track this ourselves: libtorrent applies priority changes later, so reading them back
    # while other threads (hf download fetches 8 files at once) set theirs loses updates.
    with _lock:
        wanted = _wanted.setdefault(repo_id, set())
        wanted.add(i)
        h.prioritize_files([4 if f in wanted else 0 for f in range(ti.num_files())])

    last_done, last_change = -1, time.monotonic()
    while True:
        done = h.file_progress(lt.torrent_handle.piece_granularity)[i]
        if done >= size:
            break
        st = h.status()
        if st.errc.value():  # e.g. a file it can't open: no point waiting for the stall timeout
            raise RuntimeError(f"torrent error: {st.errc.message()}")
        if done != last_done:
            last_done, last_change = done, time.monotonic()
        elif time.monotonic() - last_change > STALL_TIMEOUT:
            raise TimeoutError(f"stalled for {STALL_TIMEOUT:.0f}s")
        time.sleep(0.25)
    _flush_to_disk(h)
    return path
