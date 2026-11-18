import atexit
import contextlib
import enum
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
from pirateface.listing import info_problems, unsafe_path

CACHE_DIR = Path(config.CACHE_DIR)
_lock = threading.Lock()
_session = None
_handles = {}  # repo_id -> its RepoState once asked: a torrent handle, NO_SWARM, or the Unsafe that refused it
_wanted = {}
_active = {}  # repo_id -> fetch_file calls currently downloading
_setup = {}  # repo_id -> lock serialising handle setup
_resume = {}  # infohash -> bencoded resume data captured from the alert pump
_resume_cv = threading.Condition()
_idle_cv = threading.Condition(_lock)  # signalled when a repo's in-flight fetch count drops


class Unavailable(Exception):
    pass


class RepoState(enum.Enum):
    """A repo's _handles state other than its torrent handle or the Unsafe that refused its listing."""

    UNASKED = "the site was never asked about the repo"
    NO_SWARM = "not listed, no usable answer, or no file list in time: Hugging Face serves it"


class Unsafe(Unavailable):
    """The torrent lists a path this client will not write: one that leaves the torrent's own folder, or a
    name this OS cannot store."""


class NotInTorrent(Unavailable):
    """The torrent's file list does not include this file: the swarm carries only part of the repo."""


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
                # The tracker hands out a 60s interval to peers that are moving data; libtorrent's default floor is 300s.
                "min_announce_interval": 60,
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
    # Never block interpreter exit behind a fetch thread holding _lock: give up on the snapshot
    # and let the OS reclaim files.
    if not _lock.acquire(timeout=1):
        return
    try:
        handles = [h for h in _handles.values() if isinstance(h, lt.torrent_handle)]
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


def _remembered(repo_id):
    with _lock:
        return _handles.get(repo_id, RepoState.UNASKED)


def _remember(repo_id, state):
    """One map says what happened to a repo, so nothing has to be kept in step with it."""
    with _lock:
        _handles[repo_id] = state


def _lookup_magnet(repo_id):
    """The repo's magnet, or None to fall back on Hugging Face. NO_SWARM is remembered so the site is
    asked once per process; an unreachable site raises instead, so it is retried next time."""
    try:
        entry = api.lookup(repo_id)
    except api.Unreachable as e:
        raise Unavailable("Pirate Face unreachable") from e
    if entry is api.LICENSE_REQUIRED:
        raise Unavailable(f"accept the license terms at {config.API}/{repo_id}")
    magnet = entry.get("magnet") if isinstance(entry, dict) else None
    if not magnet:
        ui.not_on_pirateface(repo_id, entry is api.NOT_LISTED and api.request_rescue(repo_id))
        _remember(repo_id, RepoState.NO_SWARM)
    return magnet


def _torrent_params(repo_id, magnet, resume, cache_list=True):
    """(params, saved): add_torrent params with the cached file list and any resume data folded in, and
    the .torrent path the file list is cached to (a magnet alone needs a live peer to get it).

    cache_list is False when the caller supplied the magnet: the site it would ask is exactly what may be
    unreachable on that path, and the magnet is authoritative.
    """
    atp = lt.parse_magnet_uri(magnet)
    atp.save_path = str(CACHE_DIR)
    atp.flags |= lt.torrent_flags.default_dont_download
    saved = CACHE_DIR / "torrents" / f"{atp.info_hashes.v1}.torrent"
    if not saved.exists() and cache_list:
        _save_torrent_file(repo_id, saved, str(atp.info_hashes.v1))
    if saved.exists():
        atp.ti = lt.torrent_info(str(saved))
    if resume:
        ra = lt.read_resume_data(resume)
        atp.have_pieces = ra.have_pieces
        atp.verified_pieces = ra.verified_pieces
        atp.unfinished_pieces = ra.unfinished_pieces
    return atp, saved


def _wait_for_metadata(repo_id, h):
    """Block until the torrent has its file list, or forget the repo and time out."""
    deadline = time.monotonic() + config.METADATA_TIMEOUT
    while not h.status().has_metadata:
        if time.monotonic() > deadline:
            _get_session().remove_torrent(h)
            _remember(repo_id, RepoState.NO_SWARM)
            raise TimeoutError("no peer sent the file list in time")
        time.sleep(0.2)


def _check_listing(repo_id, h, saved):
    """Refuse a listing this client won't write, before the handle is remembered or its .torrent cached.

    libtorrent writes the torrent's own paths under save_path, so this is the one place the download can be
    stopped: every file is still skipped (default_dont_download), so nothing has been prioritised or created."""
    info = lt.bdecode(h.torrent_file().info_section())
    try:
        files_in(info)  # the decoded info dict: no re-encode, and the result is only used to refuse
    except Unsafe as e:
        _get_session().remove_torrent(h)
        saved.unlink(missing_ok=True)  # the .torrent we cached from the site, kept for nothing
        _remember(repo_id, e)  # the reason, not None: every later file of this repo says the same
        raise


def _cache_file_list(saved, h):
    """Save the file list a peer sent, so a magnet alone needs a live peer only the first time."""
    if not saved.exists():
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes(lt.bencode({"info": lt.bdecode(h.torrent_file().info_section())}))


def _handle(repo_id, resume=None, magnet=None, where=None):
    """The repo's handle, adding the torrent the first time. Callers hold the repo's _setup_lock, so only the
    shared dicts need _lock: the site lookup and the metadata wait run without it, so one repo waiting for its
    file list doesn't hold up every other repo's downloads and status lines.

    `magnet` joins that swarm instead of asking the site which one belongs to the repo: that is the
    cached-index path, where the site is what cannot be reached. One repo id is not one swarm -- revisions
    and quants are separate torrents -- so a handle already open is reused only if it is that swarm.
    `where` is the provenance appended to the swarm line -- "the cached index" when the file list came
    from the cache instead of the site.
    """
    from_index = magnet is not None
    match _remembered(repo_id):
        case Unsafe() as refused:
            raise Unavailable(str(refused))  # a listing this client refused: same answer for every file
        case RepoState.NO_SWARM if magnet is None:
            return None  # an answer about the API, not the index: a magnet still joins its swarm
        case lt.torrent_handle() as h:
            if magnet is None or str(h.info_hashes().v1) == str(lt.parse_magnet_uri(magnet).info_hashes.v1):
                return h
            raise Unavailable("a different torrent for this repo is already open")
    if magnet is None:
        magnet = _lookup_magnet(repo_id)
        if magnet is None:
            return None
    atp, saved = _torrent_params(repo_id, magnet, resume, cache_list=not from_index)
    h = _get_session().add_torrent(atp)
    _wait_for_metadata(repo_id, h)
    _check_listing(repo_id, h, saved)
    _cache_file_list(saved, h)
    _choose_by_piece(h)
    _remember(repo_id, h)
    threading.Thread(target=_show_status, args=(repo_id, h), daemon=True).start()
    fs = h.torrent_file().files()
    real = real_files(fs)
    if where is None:
        where = "the cached index" if from_index else "Pirate Face"
    ui.banner(repo_id, len(real), sum(fs.file_size(i) for i in real), where)
    return h


def _choose_by_piece(h):
    """Turn every file on, then choose what to download per piece. A file at priority 0 has its share of a
    piece parked in libtorrent's part file, and switching that file on while the piece is half-downloaded
    fails with "file too short" and stops the torrent. Nothing is downloaded yet, so this switch is safe."""
    h.prioritize_files([4] * h.torrent_file().num_files())
    # Applied all at once by a disk job, which then resets piece priorities; pad files stay at 0.
    deadline = time.monotonic() + config.METADATA_TIMEOUT
    while 4 not in h.get_file_priorities():
        if time.monotonic() > deadline:
            raise TimeoutError("libtorrent didn't apply the file priorities")
        time.sleep(0.05)
    h.prioritize_pieces([0] * h.torrent_file().num_pieces())


def _pieces_for(ti, files):
    """Piece priorities that download exactly the pieces these files touch."""
    fs, length = ti.files(), ti.piece_length()
    prio = [0] * ti.num_pieces()
    for f in files:
        start, size = fs.file_offset(f), fs.file_size(f)
        for p in range(start // length, (start + size - 1) // length + 1 if size else 0):
            prio[p] = 4
    return prio


def _save_torrent_file(repo_id, saved, infohash):
    try:
        data = api.torrent_file(repo_id)
    except api.Unreachable:
        return  # the magnet still works; file list can come from peers
    if not data:
        return
    try:
        ti = lt.torrent_info(data)
    except RuntimeError:
        # A .torrent the site sent that libtorrent can't parse shouldn't take the repo off the
        # swarm: the magnet still works and the file list can come from peers.
        return
    if str(ti.info_hashes().v1) == infohash:
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


def real_files(fs):
    """The indices of fs's files, BEP 47 pad files left out."""
    return [i for i in range(fs.num_files()) if not fs.file_flags(i) & fs.flag_pad_file]


def _relative(ti, i):
    """The entry's path inside the torrent's folder (libtorrent prefixes the torrent name, and on Windows
    returns backslashes). The single place a torrent path becomes a relative one."""
    fs = ti.files()
    path = fs.file_path(i).replace(os.sep, "/")
    return path.split("/", 1)[1] if fs.num_files() > 1 and "/" in path else path


def _file_index(ti, filename):
    fs = ti.files()
    for i in range(fs.num_files()):
        if _relative(ti, i) == filename:
            return i
    return None


def swarm_files(repo_id, magnet=None, where=None):
    """(path, size) for each real file in the repo's torrent, BEP 47 pad files left out.

    Opens (or reuses) the handle, so this is how a caller finds out what a swarm actually carries before
    promising a download from it: a repo can be in the index while its torrent holds only some files.
    """
    with _setup_lock(repo_id):
        h = _handle(repo_id, magnet=magnet, where=where)
        if h is None:
            raise Unavailable()
        ti = h.torrent_file()
        fs = ti.files()
        return [(_relative(ti, i), fs.file_size(i)) for i in real_files(fs)]


def fetch_file(repo_id, filename, stall_timeout=config.STALL_TIMEOUT, magnet=None):
    counted = False
    try:
        with _setup_lock(repo_id):
            h = _handle(repo_id, magnet=magnet)
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
                # Every counted fetch decrements in a finally, so this always drains --
                # though it can take as long as the other fetch's flush and metadata
                # waits plus any wait behind the global flush lock.
                with _idle_cv:
                    while _active.get(repo_id):
                        _idle_cv.wait(5)
                if h.status().state == lt.torrent_status.finished:  # still: no other fetch revived it meanwhile
                    resume = _resume_data(h)
                    with _lock:
                        with contextlib.suppress(Exception):
                            _get_session().remove_torrent(h)
                        _handles.pop(repo_id, None)
                    h = _handle(repo_id, resume, magnet=magnet)
                    if h is None:
                        raise Unavailable()

            ti = h.torrent_file()
            i = _file_index(ti, filename)
            if i is None:
                raise NotInTorrent("not in the torrent")
            size = ti.files().file_size(i)
            path = Path(h.status().save_path) / ti.files().file_path(i)
            # Set all priorities in one call (all-skipped for a moment drops the web seed), from our own set:
            # libtorrent applies them lazily, so reading them back across threads loses updates.
            # Keep accumulating wanted files: from_pretrained fetches several files in parallel.
            with _lock:
                wanted = _wanted.setdefault(repo_id, set())
                wanted.add(i)
                h.prioritize_pieces(_pieces_for(ti, wanted))
                # Counted only after the priorities are in place: an exception above
                # must not leave a phantom in-flight fetch behind.
                _active[repo_id] = _active.get(repo_id, 0) + 1
                counted = True

        # A stall is no bytes arriving, not no piece finishing: one large piece from a slow but live swarm can
        # take longer than the timeout.
        last_received, last_change = -1, time.monotonic()
        while True:
            done = h.file_progress(lt.torrent_handle.piece_granularity)[i]
            if done >= size:
                break
            st = h.status()
            if st.errc.value():
                raise RuntimeError(f"torrent error: {st.errc.message()}")
            if st.total_payload_download != last_received:
                last_received, last_change = st.total_payload_download, time.monotonic()
            elif time.monotonic() - last_change > stall_timeout:
                raise TimeoutError(f"stalled for {stall_timeout:.0f}s")
            time.sleep(0.25)
        _flush_to_disk(h)
        return path
    except BaseException:
        # A failed fetch must not leave its file wanted forever: at priority 4 the
        # torrent never reaches "finished" again, which would disable the re-add path.
        if counted:  # set only once h, ti and i exist
            with _idle_cv:
                wanted = _wanted.get(repo_id)
                if wanted is not None and i in wanted and _active.get(repo_id, 0) <= 1:
                    wanted.discard(i)
                    with contextlib.suppress(Exception):
                        h.prioritize_pieces(_pieces_for(ti, wanted))
        raise
    finally:
        if counted:
            with _idle_cv:
                _active[repo_id] -= 1
                _idle_cv.notify_all()


PAD_DIR = ".pad"  # pad bytes a creator wrote out by hand, without attr=p to say so


def files_in(data):
    """(path, size, web seed URL or None) for each file in a .torrent, BEP 47 pad files left out.

    Refuses with Unsafe -- naming the offending path -- a listing this client cannot write as given. Every
    torrent path becomes a path on disk here, so a caller never has to check one again.

    `data` is a .torrent's bytes or its decoded info dict: a caller that already decoded the info (the
    download gate does, from the handle) needs no round trip. An info dict carries no BEP 19 web seed, so
    the third element is None.
    """
    if isinstance(data, dict):
        torrent, info = {}, data
    else:
        torrent = lt.bdecode(data)
        info = torrent[b"info"]
    try:
        name = info[b"name"].decode()
    except UnicodeDecodeError as e:
        raise Unsafe(f"the torrent's name is not UTF-8: {info[b'name'][:80]!r}") from e
    single = b"files" not in info
    if single:
        if b"length" not in info:
            raise Unsafe(f"{name}: the torrent has no file list this client can read")  # BEP 52 v2 only
        entries = [(name, info[b"length"])]
    else:
        entries = []
        for f in info[b"files"]:
            if b"p" in f.get(b"attr", b""):
                continue
            try:
                parts = [part.decode() for part in f[b"path"]]
            except UnicodeDecodeError as e:
                raise Unsafe(f"{name}: refusing {f[b'path']!r}: a path that is not UTF-8") from e
            if not parts:
                raise Unsafe(f"{name}: a file in the torrent has no path")
            if parts[0] == PAD_DIR:
                continue  # a pad folder written out by hand, without attr=p to say so
            entries.append(("/".join(parts), f[b"length"]))
    why = unsafe_path(name) or next(iter(info_problems(info, [p for p, _ in entries])), None)
    if why:
        raise Unsafe(f"{name}: refusing {why}")
    return [(p, size, web_seed_url(torrent, name if single else f"{name}/{p}")) for p, size in entries]


def web_seed_url(torrent, path_in_torrent):
    """BEP 19: a base ending in "/" gets the file's path appended; otherwise it is the file itself."""
    seeds = torrent.get(b"url-list") or []
    seed = (seeds if isinstance(seeds, bytes) else seeds[0] if seeds else b"").decode()
    if not seed:
        return None
    return seed + urllib.parse.quote(path_in_torrent) if seed.endswith("/") else seed
