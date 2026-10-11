import os
from importlib.metadata import version
from pathlib import Path


def flag(name):
    return os.environ.get(name, "") not in ("", "0")


VERSION = version("pirateface")
API = os.environ.get("PF_API", "https://pirateface.co")
CACHE_DIR = Path(os.environ.get("PF_CACHE", Path.home() / ".cache" / "pirateface"))
LISTEN = os.environ.get("PF_LISTEN", "0.0.0.0:0,[::]:0")
METADATA_TIMEOUT = float(os.environ.get("PF_METADATA_TIMEOUT", "30"))
STALL_TIMEOUT = float(os.environ.get("PF_STALL_TIMEOUT", "60"))
