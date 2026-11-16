import os


def flag(name):
    return os.environ.get(name, "") not in ("", "0")


API = os.environ.get("PF_API", "https://pirateface.co")
MAX_UPLOAD_BYTES = 3_000_000  # the largest .torrent the site takes in an upload
CACHE_DIR = os.environ.get("PF_CACHE") or os.path.expanduser("~/.cache/pirateface")  # noqa: PTH111  # pathlib slows every Python startup
SEEDS = os.environ.get("PIRATEFACE_SEEDS") or os.path.expanduser("~/pirateface-seeds")  # noqa: PTH111  # links and .torrent files
LISTEN = os.environ.get("PF_LISTEN", "0.0.0.0:0,[::]:0")
METADATA_TIMEOUT = float(os.environ.get("PF_METADATA_TIMEOUT", "30"))
STALL_TIMEOUT = float(os.environ.get("PF_STALL_TIMEOUT", "60"))
PF_ONLY_STALL_TIMEOUT = float(os.environ.get("PF_ONLY_STALL_TIMEOUT", "600"))
