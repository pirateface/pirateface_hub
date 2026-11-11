import atexit
import contextlib
import os
import shutil
import sys
import threading
import time
import urllib.parse
from importlib.metadata import version
from pathlib import Path

import libtorrent as lt

from pirateface import config, ui
from pirateface.client import api

CACHE_DIR = Path(config.CACHE_DIR)
_lock = threading.Lock()
_session = None
_handles = {}  # repo_id -> handle, or None if not on Pirate Face
_wanted = {}
_active = {}  # repo_id -> fetch_file calls currently downloading
_setup = {}  # repo_id -> lock serialising handle setup
_resume = {}  # infohash -> bencoded resume data captured from the alert pump
_resume_cv = threading.Condition()
_idle_cv = threading.Condition(_lock)  # signalled when a repo's in-flight fetch count drops


class Unavailable(Exception):
    pass


def _get_session():
    global _session
    if _session is None:
        debug = config.flag("PF_DEBUG")
        _session = lt.session(
            {
                "listen_interfaces": config.LISTEN,
                "user_agent": f"pirateface/{version('pirateface')}",
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
            if isinstance(a, lt.save_resume_data_alert):
                with _resume_cv:
                    _resume[str(a.handle.info_hashes().v1)] = lt.bencode(a.resume_data)
                    _resume_cv.notify_all()
            if debug:
                msg = a.message()
                if not any(k in msg or k in type(a).__name__ for k in noisy):
                    print(f"[lt] {msg[:220]}", file=sys.stderr, flush=True)


@atexit.register
def _cleanup():
    if _session is None:
        return
    # Never block interpreter exit behind a fetch thread holding _lock (up to
    # METADATA_TIMEOUT): give up on the snapshot and let the OS reclaim files.
    if not _lock.acquire(timeout=1):
        return
    try:
        handles = [h for h in _handles.values() if h is not None]
    finally:
        _lock.release()
    for h in handles:
        with contextlib.suppress(Exception):
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


def _resume_data(h, timeout=10):
    """Bencoded resume data, so a re-added handle trusts what is already on disk instead of re-hashing it."""
    key = str(h.info_hashes().v1)
    with _resume_cv:
        _resume.pop(key, None)
        h.save_resume_data()
        if not _resume_cv.wait_for(lambda: key in _resume, timeout):
            return None
        return _resume.pop(key)


def _setup_lock(repo_id):
    """Serialises handle creation/re-add per repo so parallel fetches share one handle."""
    with _lock:
        return _setup.setdefault(repo_id, threading.Lock())


def _handle(repo_id, resume=None):
    with _lock:
        if repo_id in _handles:
            return _handles[repo_id]
        magnet = api.lookup_magnet(repo_id)
        if magnet is api.NOT_LISTED:
            ui.not_on_pirateface(repo_id, api.request_rescue(repo_id))
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
        if not saved.exists():
            _save_torrent_file(repo_id, saved, str(atp.info_hashes.v1))
        if saved.exists():
            atp.ti = lt.torrent_info(str(saved))
        if resume:
            ra = lt.read_resume_data(resume)
            atp.have_pieces = ra.have_pieces
            atp.verified_pieces = ra.verified_pieces
            atp.unfinished_pieces = ra.unfinished_pieces
        h = _get_session().add_torrent(atp)
        deadline = time.monotonic() + config.METADATA_TIMEOUT
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


def _save_torrent_file(repo_id, saved, infohash):
    data = api.torrent_file(repo_id)
    if data and str(lt.torrent_info(data).info_hashes().v1) == infohash:
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes(data)


def _show_status(repo_id, h, every=0.5, recent=3.0):
    """Sources = who sent bytes recently, not who is connected: the web seed reconnects per file and flickers."""
    sent = {}
    last_seen = {}
    bar = None
    while True:
        time.sleep(every)
        try:
            with _lock:
                wanted = list(_wanted.get(repo_id, ()))
                current = _handles.get(repo_id)
            # Stop if this handle was replaced (e.g. re-added after going finished).
            if current is not h:
                if bar is not None:
                    bar.close()
                return
            fs = h.torrent_file().files()
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
        except Exception:
            # The handle can disappear while this thread is reading it; the download
            # thread reports real failures, so stop quietly here.
            if bar is not None:
                bar.close()
            return


def _file_index(ti, filename):
    fs = ti.files()
    for i in range(fs.num_files()):
        path = fs.file_path(i).replace(os.sep, "/")
        rel = path.split("/", 1)[1] if fs.num_files() > 1 and "/" in path else path
        if rel == filename:
            return i
    return None


def fetch_file(repo_id, filename, stall_timeout=config.STALL_TIMEOUT):
    with _setup_lock(repo_id):
        h = _handle(repo_id)
        if h is None:
            raise Unavailable()
        # A model download fetches files one at a time. After the last wanted file
        # completes the torrent goes "finished" and libtorrent drops the web seed,
        # so the next file never gets bytes and stalls until PF_STALL_TIMEOUT.
        # Re-add the handle (with resume data, so nothing already on disk is
        # re-hashed) -- but only while no other fetch is using it, so parallel
        # calls never yank a handle out from under each other.
        if h.status().state == lt.torrent_status.finished:
            # Another fetch may still be flushing its last file on this handle;
            # wait for it to finish rather than prioritising into a finished torrent.
            # The count only ever drains (every fetch decrements in a finally), so
            # waiting is bounded by the other fetch's own flush/stall deadline.
            with _idle_cv:
                while _active.get(repo_id):
                    _idle_cv.wait(5)
            idle = True
        else:
            idle = False
        if idle and h.status().state == lt.torrent_status.finished:
            resume = _resume_data(h)
            with _lock:
                with contextlib.suppress(Exception):
                    _get_session().remove_torrent(h)
                _handles.pop(repo_id, None)
            h = _handle(repo_id, resume)
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
        # Keep accumulating wanted files: from_pretrained fetches several files in parallel.
        with _lock:
            wanted = _wanted.setdefault(repo_id, set())
            wanted.add(i)
            h.prioritize_files([4 if f in wanted else 0 for f in range(ti.num_files())])
            # Counted only after the priorities are in place: an exception above
            # must not leave a phantom in-flight fetch behind.
            _active[repo_id] = _active.get(repo_id, 0) + 1

    try:
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
            elif time.monotonic() - last_change > stall_timeout:
                raise TimeoutError(f"stalled for {stall_timeout:.0f}s")
            time.sleep(0.25)
        _flush_to_disk(h)
        return path
    except BaseException:
        # A failed fetch must not leave its file wanted forever: at priority 4 the
        # torrent never reaches "finished" again, which would disable the re-add path.
        with _idle_cv:
            wanted = _wanted.get(repo_id)
            if wanted is not None and i in wanted and _active.get(repo_id, 0) <= 1:
                wanted.discard(i)
                with contextlib.suppress(Exception):
                    h.prioritize_files([4 if f in wanted else 0 for f in range(ti.num_files())])
        raise
    finally:
        with _idle_cv:
            _active[repo_id] -= 1
            _idle_cv.notify_all()


def files_in(data):
    """(path, size, web seed URL or None) for each file in a .torrent, BEP 47 pad files left out."""
    torrent = lt.bdecode(data)
    info = torrent[b"info"]
    name = info[b"name"].decode()
    if b"files" not in info:
        return [(name, info[b"length"], web_seed_url(torrent, name))]
    files = []
    for f in info[b"files"]:
        if b"p" in f.get(b"attr", b""):
            continue
        path = "/".join(part.decode() for part in f[b"path"])
        files.append((path, f[b"length"], web_seed_url(torrent, f"{name}/{path}")))
    return files


def web_seed_url(torrent, path_in_torrent):
    """BEP 19: a base ending in "/" gets the file's path appended; otherwise it is the file itself."""
    seeds = torrent.get(b"url-list") or []
    seed = (seeds if isinstance(seeds, bytes) else seeds[0] if seeds else b"").decode()
    if not seed:
        return None
    return seed + urllib.parse.quote(path_in_torrent) if seed.endswith("/") else seed
