"""Session errors for the LinkedIn demo collector. The demo needs no session."""


class AuthError(RuntimeError):
    """Raised by the real collector when its saved session is missing or dead."""
