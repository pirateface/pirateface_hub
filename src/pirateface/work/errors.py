class WorkError(Exception):
    """A job failed: the lease, the source bytes, or the build did not check out."""


class WorkUnreachable(WorkError):
    """The site could not be reached for the lease poll. Not a job failure: the worker backs off and retries."""
