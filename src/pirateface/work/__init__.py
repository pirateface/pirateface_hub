"""pf work: builds torrents for queued model requests, streaming the files from Hugging Face.

Modules: runner (the loop and consent), site (HTTP calls to the work endpoints), build (streams
and checks one job), source (reads one file from Hugging Face), errors.
"""

from pirateface.work.runner import run

__all__ = ["run"]
