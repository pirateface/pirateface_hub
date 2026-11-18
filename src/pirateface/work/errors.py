class WorkError(Exception):
    """A job failed: the lease, the source bytes, or the build did not check out."""


class WorkUnreachable(WorkError):
    """The site could not be reached for the lease poll. Not a job failure: the worker backs off and retries."""


class LeaseLost(WorkError):
    """The lease lapsed or was taken away mid-job. The job goes back to the queue and may come back to this lane,
    so its finished files are kept."""


class Oversize(WorkError):
    """The site leased a job bigger than --max-size asked for. Skipped, with a pause: the same row may be
    offered again, so retrying it at once only spins."""
