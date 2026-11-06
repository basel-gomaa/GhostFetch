"""Transport errors of the real Threads collector (demo build: never raised)."""


class Challenged(RuntimeError):
    """Threads answered with a challenge instead of data."""


class RateLimited(RuntimeError):
    """Threads reported a rate limit inside an HTTP 200 body."""
