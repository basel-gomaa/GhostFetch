"""The per-run token that unlocks a run's viewable display.

Security review H-01: the display's websocket (websockify in the Actor's
container) refuses any connection without this token, so reaching the
loopback port is no longer enough to watch -- or drive -- someone's browser.

DERIVED, NOT STORED. The Worker and the API are separate processes that never
talk directly, and the registry is readable by more than either. Both already
hold ACTOR_PLATFORM_SECRET_KEY (it decrypts stored sessions), so both can
compute HMAC(key, "display:" + run_id) independently: the Worker hands it to
the container, the API presents it when proxying, and it is written nowhere.

None without a key, and the container's plugin then refuses every connection:
fail closed, never an open viewer.
"""

from __future__ import annotations

import hashlib
import hmac
import os

ENV_DISPLAY_TOKEN = "ACTOR_DISPLAY_TOKEN"
KEY_ENV = "ACTOR_PLATFORM_SECRET_KEY"


def display_token(run_id: str, key: str | None = None) -> str | None:
    secret = key if key is not None else os.environ.get(KEY_ENV, "")
    if not secret or not run_id:
        return None
    return hmac.new(secret.encode("utf-8"), f"display:{run_id}".encode("utf-8"),
                    hashlib.sha256).hexdigest()
