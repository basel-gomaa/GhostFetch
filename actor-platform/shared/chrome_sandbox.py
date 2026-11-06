"""Chrome's own sandbox, for the containers that run Chrome and ask for it.

Chrome isolates every page it renders in new user, PID and network namespaces,
then chroots it into an empty directory. Docker's default seccomp profile
refuses all three operations to a container without CAP_SYS_ADMIN /
CAP_SYS_CHROOT -- ours never have them -- so Chrome used to run with
--no-sandbox, and a page that compromised the renderer had the run's whole
filesystem: the signed-in profile, the cookie store, the session file.

`docker_args()` hands such a container deploy/seccomp/chrome-sandbox.json --
Docker's default profile plus exactly clone/unshare for those three namespaces
and chroot (see deploy/seccomp/make_chrome_profile.py) -- and tells the browser
inside, through CHROME_SANDBOX=1, that it may leave the sandbox on. The two
always travel together: a browser told to sandbox without the profile dies at
launch ("Failed to move to new namespace"), and the profile without the flag
widens seccomp for nothing.

Everything else about the container is unchanged: non-root, --cap-drop ALL,
no-new-privileges, its own network. Nothing here adds a capability, a host
namespace or --privileged.

ACTOR_PLATFORM_CHROME_SANDBOX=0 turns it off and restores the exact old argv,
for a host whose kernel refuses unprivileged user namespaces (Chrome would not
start there). That is a security downgrade and it is logged as one.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

PROFILE_PATH = Path(__file__).resolve().parent.parent / "deploy" / "seccomp" / "chrome-sandbox.json"
ENV = "ACTOR_PLATFORM_CHROME_SANDBOX"
CONTAINER_ENV = "CHROME_SANDBOX"

log = logging.getLogger("chrome_sandbox")
_warned = False


class SandboxProfileMissing(RuntimeError):
    """The profile file is not where it must be. Never silently unsandboxed."""


def enabled(env: dict[str, str] | None = None) -> bool:
    global _warned
    raw = ((env if env is not None else os.environ).get(ENV) or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        if not _warned:
            _warned = True
            log.warning("%s=%s: Chrome runs with --no-sandbox in its containers", ENV, raw)
        return False
    return True


def docker_args(env: dict[str, str] | None = None) -> list[str]:
    """`docker run` arguments that give a container Chrome's sandbox, or []
    when the operator turned it off. Raises rather than run Chrome unsandboxed
    because a file went missing."""
    if not enabled(env):
        return []
    if not PROFILE_PATH.is_file():
        raise SandboxProfileMissing(
            f"{PROFILE_PATH} is missing; run deploy/seccomp/make_chrome_profile.py, "
            f"or set {ENV}=0 to run Chrome without its sandbox")
    # The docker CLI reads this file itself and sends its contents, so the path
    # is resolved here, on the host, not inside any container.
    return ["--security-opt", f"seccomp={PROFILE_PATH}", "-e", f"{CONTAINER_ENV}=1"]
