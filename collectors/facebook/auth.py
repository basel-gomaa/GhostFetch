"""Session handling for the Facebook demo collector. The demo needs no session."""


class AuthError(RuntimeError):
    """Raised by the real collector when its saved session is missing or dead."""


def preflight():
    """The real collector checks the saved session here. -> (ok, reason)."""
    return True, "demo"
