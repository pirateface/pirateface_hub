import os
import sys
import threading

from tqdm.auto import tqdm

from pirateface.config import flag

QUIET = flag("PF_QUIET")
_color = sys.stderr.isatty() and "NO_COLOR" not in os.environ
_lock = threading.Lock()
_announced = set()

SPLASH = r'''
          _.-""""-._
     __.-'   (xx)   '-.__
    '-.________________.-'
      /                \
     |   ^      (###)===|
      \    \______/    /
       '-.__________.-'
'''[1:].splitlines()
TAGLINE = ("pirateface", "  Open-weight model downloads, from the swarm")


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _color else text


def _print(line):
    if not QUIET:
        tqdm.write(line, file=sys.stderr)


def _bar():
    return _c("35", "┃")


def mb(n):
    return f"{n / 1e6:.1f} MB" if n >= 1e5 else f"{n / 1e3:.1f} kB"


def splash():
    if _announced or not sys.stderr.isatty():
        return
    pad = " " * ((len("".join(TAGLINE)) - max(map(len, SPLASH))) // 2)
    art = [pad + (_c("1", line) if i < 3 else _c("33", line)) for i, line in enumerate(SPLASH)]
    _print("\n".join(art) + "\n" + _c("1;35", TAGLINE[0]) + _c("2", TAGLINE[1]) + "\n")


def banner(repo_id, files, size):
    with _lock:
        if repo_id in _announced:
            return
        splash()
        _announced.add(repo_id)
    _print(
        _bar()
        + " "
        + _c("1;35", "🏴‍☠️ pirateface")
        + f"  {repo_id} is on Pirate Face ({files} files, {mb(size)}), downloading from the swarm"
    )


def not_on_pirateface(repo_id, rescue_requested=False):
    with _lock:
        if repo_id in _announced:
            return
        _announced.add(repo_id)
    line = _bar() + " " + _c("1;35", "🏴‍☠️ pirateface") + f"  {repo_id} isn't on Pirate Face yet, using Hugging Face"
    if rescue_requested:
        line += _c("2", " · requested a rescue (turn off: PF_NO_RESCUE=1)")
    _print(line)


def repo_progress(repo_id):
    return tqdm(
        total=1,
        leave=False,
        file=sys.stderr,
        disable=QUIET or not sys.stderr.isatty(),
        desc=f"{_bar()}  🏴‍☠️ {repo_id}",
        bar_format="{desc}  {bar:20}",
        colour="magenta",
    )


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


def from_swarm(filename, size):
    _print(
        f"{_bar()}  {_c('32', '✔')} {filename}  {_c('2', mb(size) + ' from the swarm, fingerprint matches Hugging Face')}"
    )


def from_hf(filename, reason=None):
    if reason:
        _print(f"{_bar()}  {_c('33', '↩')} {filename}  {_c('2', 'from Hugging Face (' + reason + ')')}")
