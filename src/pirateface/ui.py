import os
import sys
import threading

from tqdm.auto import tqdm

from pirateface.config import flag

QUIET = flag("PF_QUIET")

_lock = threading.Lock()
_announced = set()
_download_counts = {"swarm": 0, "hf": 0}


def ansi(stream=sys.stdout):
    """Escape codes only on a terminal that shows them: not piped, not NO_COLOR, not TERM=dumb,
    and on Windows only in Windows Terminal (the classic console prints them as text)."""
    return (
        stream.isatty()
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM") != "dumb"
        and (os.name != "nt" or "WT_SESSION" in os.environ)
    )


def _dim(text, stream):
    return f"\033[2m{text}\033[0m" if ansi(stream) else text


def _bold(text, stream):
    return f"\033[1m{text}\033[0m" if ansi(stream) else text


def _print(line):
    if not QUIET:
        tqdm.write(line, file=sys.stderr)


def _emit(line, stream):
    if stream is sys.stderr:
        _print(line)
    else:
        print(line, file=stream)


def note(label, text, stream=sys.stderr):
    """One fact. The label is dim; the text is plain."""
    _emit(_dim(f"{label:<8}", stream) + text, stream)


def aligned(rows, stream=sys.stdout, width=None):
    """rows: (label, value). One line per fact, labels aligned. Short layer names are bold."""
    rows = [(str(label), value) for label, value in rows if value is not None]
    if not rows:
        return
    width = width or max(len(label) for label, _ in rows)
    for label, value in rows:
        shown = _bold(value, stream) if value in ("hybrid v1+v2", "v1 only") else value
        _emit(f"    {_dim(f'{label:<{width}}', stream)}  {shown}", stream)


def layer_block(rows, nxt=None):
    """The seeding layers, then the one command that fills a layer still missing."""
    rows = list(rows)
    if nxt:
        rows.append(("next", nxt))
    if rows:
        print()
        aligned(rows)


def mb(n):
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB"
    return f"{n / 1e6:.1f} MB" if n >= 1e5 else f"{n / 1e3:.1f} kB"


def once(key):
    """True the first time this process sees key: a line keyed by it is printed once."""
    with _lock:
        if key in _announced:
            return False
        _announced.add(key)
        return True


def banner(repo_id, files, size, where="Pirate Face"):
    """`where` is where the file list came from: the site, or the cached index while it is down."""
    if not once(repo_id):
        return
    note("swarm", f"{repo_id}  {files} files, {mb(size)}" + ("" if where == "Pirate Face" else f"  · {where}"))


def not_on_pirateface(repo_id, rescue_requested=False):
    if not once(repo_id):
        return
    line = f"{repo_id}  isn't on Pirate Face yet, using Hugging Face"
    if rescue_requested:
        line += "  · requested a rescue (turn off: PF_NO_RESCUE=1)"
    note("hf", line)


def repo_progress(repo_id):
    return tqdm(
        total=1,
        leave=False,
        file=sys.stderr,
        disable=QUIET or not sys.stderr.isatty(),
        desc=repo_id,
        bar_format="{desc}  {bar:20}",
        ascii=not ansi(sys.stderr),
    )


def work_progress(size):
    """Byte progress for a streamed work job, shown in an interactive terminal."""
    return tqdm(
        total=size,
        unit="B",
        unit_scale=True,
        unit_divisor=1000,
        file=sys.stderr,
        dynamic_ncols=True,
        disable=QUIET or not sys.stderr.isatty(),
    )


def work_reuse(progress, size):
    """Count saved bytes as progress without treating them as downloaded bytes."""
    if progress.disable or not size:
        return
    progress.n += size
    progress.initial += size
    progress.last_print_n += size
    progress.refresh()


def set_status(bar, text):
    # Not tqdm's postfix: it always prepends ", ".
    bar.bar_format = "{desc}  {bar:20}  " + text.replace("{", "{{").replace("}", "}}")
    bar.refresh()


def sources_label(webseed, peers):
    if webseed and peers:
        return f"web seed + {peers} peer{'s' * (peers > 1)}"
    if webseed:
        return "web seed"
    if peers:
        return f"{peers} peer{'s' * (peers > 1)}"
    return "finding sources…"


def from_swarm(filename, size, checked_by="Hugging Face"):
    with _lock:
        _download_counts["swarm"] += 1
    note("file", f"{filename}  {mb(size)}  fingerprint matches {checked_by}")


def from_index(filename, size, build, checked_by="Hugging Face"):
    """A file from the swarm the cached index names, and what its bytes were checked against."""
    with _lock:
        _download_counts["swarm"] += 1
    if checked_by == "Hugging Face":
        note("file", f"{filename}  {mb(size)}  from the index (build {build}), fingerprint matches Hugging Face")
    else:
        note("file", f"{filename}  {mb(size)}  from the index (build {build}), checked against {checked_by}")


def from_hf(filename, reason=None):
    if reason:
        with _lock:
            _download_counts["hf"] += 1
        note("hf", f"{filename}  from Hugging Face ({reason})")


def download_summary():
    """How many files came from the swarm and how many fell back to Hugging Face."""
    with _lock:
        swarm, hf = _download_counts["swarm"], _download_counts["hf"]
    parts = []
    if swarm:
        parts.append(f"{swarm} from the swarm")
    if hf:
        parts.append(f"{hf} from Hugging Face")
    if parts:
        note("files", ", ".join(parts))
