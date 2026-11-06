"""Session errors for the TikTok demo collector. The demo needs no session."""


class AuthError(RuntimeError):
    """Raised by the real collector when its saved session is missing or dead."""


class BrowserUnavailable(RuntimeError):
    """Raised by the real collector when no browser could be started."""
