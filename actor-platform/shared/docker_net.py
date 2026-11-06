"""The two Docker bridges untrusted containers run on, created one way.

Actor runs join ``ap-actor-net``; login browsers join ``ap-login-net``. Both are
created with:

* inter-container communication OFF (security reviews M-02 / H-01), so one
  container cannot reach another's ports on the same bridge; and
* a FIXED host interface name (``ap-actor0`` / ``ap-login0``) instead of
  Docker's ``br-<network id>`` (security review N-01). The id changes whenever
  a network is recreated, so a host firewall rule written against it would
  silently stop matching. ``deploy/host-firewall.sh`` matches these names, which
  is what lets it refuse every connection from these containers to the host
  itself (the API, sshd, a web server, anything listening on the host) without
  touching their outbound internet access.

A network that exists with the wrong options is recreated ONLY when nothing is
attached to it -- tearing a running scrape or a half-finished login off its
network to fix an option would be worse than running one more container on a
less strict bridge. Otherwise it is used as it is, and the caller is warned.
"""

from __future__ import annotations

import subprocess
from typing import Callable

ICC_OPTION = "com.docker.network.bridge.enable_icc"
BRIDGE_NAME_OPTION = "com.docker.network.bridge.name"

ACTOR_NETWORK = "ap-actor-net"
ACTOR_BRIDGE = "ap-actor0"
LOGIN_NETWORK = "ap-login-net"
LOGIN_BRIDGE = "ap-login0"

# Linux caps an interface name at 15 bytes (IFNAMSIZ - 1).
_MAX_IFACE = 15


class NetworkError(RuntimeError):
    """Docker could not provide the network at all."""


def ensure_isolated_bridge(network: str, bridge: str, *,
                           run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                           warn: Callable[[str], None] | None = None) -> str:
    """Make sure ``network`` exists with ICC off and host interface ``bridge``.

    Returns what happened: ``"ok"`` (already right), ``"created"``,
    ``"recreated"`` (wrong options, idle, replaced) or ``"in_use"`` (wrong
    options but containers are attached, so left alone and ``warn`` called).
    Raises NetworkError when Docker cannot create it.
    """
    if not bridge or len(bridge) > _MAX_IFACE:
        raise ValueError(f"bridge interface name {bridge!r} must be 1-{_MAX_IFACE} characters")

    probe = run(
        ["docker", "network", "inspect", network, "--format",
         f'{{{{index .Options "{ICC_OPTION}"}}}}|{{{{index .Options "{BRIDGE_NAME_OPTION}"}}}}'
         "|{{len .Containers}}"],
        capture_output=True, text=True,
    )
    outcome = "created"
    if probe.returncode == 0:
        icc, name, attached = (probe.stdout.strip().split("|") + ["", "", ""])[:3]
        if icc == "false" and name == bridge:
            return "ok"
        if attached.strip() not in ("", "0"):
            if warn:
                warn(f"network {network} exists without the expected isolation options "
                     f"(enable_icc={icc or 'unset'}, bridge name={name or 'unset'}) and is in use; "
                     f"using it as it is. It is recreated once nothing is attached to it.")
            return "in_use"
        run(["docker", "network", "rm", network], capture_output=True, text=True)
        outcome = "recreated"

    made = run(
        ["docker", "network", "create", "--driver", "bridge",
         "-o", f"{ICC_OPTION}=false",
         "-o", f"{BRIDGE_NAME_OPTION}={bridge}",
         network],
        capture_output=True, text=True,
    )
    if made.returncode != 0 and "already exists" not in (made.stderr or ""):
        raise NetworkError(f"could not create docker network {network}: "
                           f"{(made.stderr or '').strip()[:300]}")
    return outcome
