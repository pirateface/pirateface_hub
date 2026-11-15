class WorkError(Exception):
    """A job failed: the lease, the source bytes, or the build did not check out."""
