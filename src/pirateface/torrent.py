import atexit
import functools
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import libtorrent as lt

from pirateface import rescue, ui
from pirateface.config import API, CACHE_DIR, LISTEN, METADATA_TIMEOUT, STALL_TIMEOUT, VERSION, flag

_lock = threading.Lock()
_session = None
_handles = {}  # repo_id -> handle, or None if not on Pirate Face
_wanted = {}


class Unavailable(Exception):
    pass


def _get_session():
    global _session
    if _session is None:
        debug = flag("PF_DEBUG")
        _session = lt.session(
            {
                "listen_interfaces": LISTEN,
                "user_agent": f"pirateface/{VERSION}",
                "alert_mask": lt.alert.category_t.all_categories if debug else lt.alert.category_t.storage_notification,
                # The torrent goes "finished" between files and drops the web seed; reconnect in 2s, not 30s.
                # Keep close_redundant_connections on: idle web seeds otherwise never request the next file.
                "urlseed_wait_retry": 2,
            }
        )
        threading.Thread(target=_pump_alerts, args=(_session, debug), daemon=True).start()
    return _session


# libtorrent reports pieces done before they're written, so files can read as zeros until flushed.
# cache_flushed_alert doesn't say which flush it answers, so only one runs at a time.
_flush = threading.Condition()
_flush_one_at_a_time = threading.Lock()
_flush_done = {}


def _pump_alerts(session, debug):
    """The only reader of libtorrent's alerts."""
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
    for h in _handles.values():
        if h is None:
            continue
        path = Path(h.status().save_path) / h.torrent_file().name()
        _session.remove_torrent(h)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def _flush_to_disk(h, timeout=30):
    key = str(h.info_hashes().v1)
    with _flush_one_at_a_time, _flush:
        before = _flush_done.get(key, 0)
        h.flush_cache()
        if not _flush.wait_for(lambda: _flush_done.get(key, 0) > before, timeout):
            raise TimeoutError("libtorrent didn't confirm the file was written to disk")


NOT_LISTED = object()


@dataclass(frozen=True)
class Listing:
    revision: str
    files: dict[str, str]  # path -> sha256


@functools.cache
def _lookup(repo_id):
    """Pirate Face's entry for the repo, NOT_LISTED, or None if unknown (network error)."""
    url = f"{API}/api/torrents?" + urllib.parse.urlencode({"repo": repo_id})
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return NOT_LISTED if e.code == 404 else None
    except Exception:
        return None


def lookup_magnet(repo_id):
    """The magnet, NOT_LISTED, or None if unknown (network error, or license terms to accept first)."""
    entry = _lookup(repo_id)
    return entry.get("magnet") if isinstance(entry, dict) else entry


@functools.cache
def _listing(repo_id):
    entry = _lookup(repo_id)
    if not isinstance(entry, dict) or not entry.get("magnet") or not entry.get("files"):
        return None
    return Listing(entry["revision"], {f["path"]: f["sha256"] for f in entry["files"]})


def listing(repo_id, revision):
    """Pirate Face's commit and file hashes, if it has the asked-for revision ("main" means whatever it has)."""
    found = _listing(repo_id)
    if found is None or revision not in (None, "main", found.revision):
        return None
    return found


def _handle(repo_id):
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
        atp.save_path = str(CACHE_DIR)
        atp.flags |= lt.torrent_flags.default_dont_download
        # Cache the file list: a magnet alone needs a live peer to get it.
        saved = CACHE_DIR / "torrents" / f"{atp.info_hashes.v1}.torrent"
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
    """Sources = who sent bytes recently, not who is connected: the web seed reconnects per file and flickers."""
    fs = h.torrent_file().files()
    sent = {}
    last_seen = {}
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
        ui.set_status(
            bar,
            f"{files_done}/{len(wanted)} files · {done / 1e6:.1f}/{total / 1e6:.1f} MB · {label} · {h.status().download_rate / 1e6:.1f} MB/s",
        )


def _file_index(ti, filename):
    fs = ti.files()
    for i in range(fs.num_files()):
        path = fs.file_path(i).replace(os.sep, "/")
        rel = path.split("/", 1)[1] if fs.num_files() > 1 and "/" in path else path
        if rel == filename:
            return i
    return None


def fetch_file(repo_id, filename):
    h = _handle(repo_id)
    if h is None:
        raise Unavailable()
    ti = h.torrent_file()
    i = _file_index(ti, filename)
    if i is None:
        raise Unavailable("not in the torrent")
    size = ti.files().file_size(i)
    path = Path(h.status().save_path) / ti.files().file_path(i)
    # Set all priorities in one call (all-skipped for a moment drops the web seed), from our own set:
    # libtorrent applies them lazily, so reading them back across threads loses updates.
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
        if st.errc.value():
            raise RuntimeError(f"torrent error: {st.errc.message()}")
        if done != last_done:
            last_done, last_change = done, time.monotonic()
        elif time.monotonic() - last_change > STALL_TIMEOUT:
            raise TimeoutError(f"stalled for {STALL_TIMEOUT:.0f}s")
        time.sleep(0.25)
    _flush_to_disk(h)
    return path
