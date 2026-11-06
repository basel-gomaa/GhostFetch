#!/usr/bin/env python3
"""Check, LIVE, what an Actor or login-browser container can reach on this host.

    .venv/bin/python scripts/check_container_isolation.py [--port 8000] [--image IMAGE]

Security review N-01. Unit tests can say what the code ASKS for; only this can
say what the network actually lets through. For each of the platform's two
bridges it starts a throwaway container with the same hardening an Actor gets
(--cap-drop ALL, no-new-privileges), and from inside it tries a TCP connect to
every port this host listens on beyond loopback, plus the API port, at the
bridge's gateway address and at every other host IPv4 address.

Each target is reported as:

    REACHABLE  the container connected            (exposed -- this fails the check)
    blocked    the host listens there, but the container could not connect
    closed     nothing listens there on the host  (nothing to protect)

It also checks the API itself: it must answer on 127.0.0.1 and must not be
listening on any other address. And it checks that the same container can still
resolve a public name and open an HTTPS connection to it -- the firewall must
not cost an Actor its internet. Exit status: 0 all good, 1 something on this
host is reachable, 2 outbound internet or DNS is broken.

UDP is not probed (avahi's 5353, for instance): a connectionless "no reply"
cannot be told apart from a dropped packet. deploy/host-firewall.sh rejects UDP
from these bridges as well.

It creates the two networks if they are missing (exactly as the Worker and the
login manager would on their next use) and otherwise changes nothing. Read-only
for the host.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared import docker_net  # noqa: E402

# A neutral public HTTPS host. Platform code names no site (the per-Actor tests
# grep for that), so the default is not one the Actors scrape.
OUTBOUND_HOST = "example.com"
DEFAULT_IMAGES = ("actor-platform/hello_world:latest", "python:3.13-slim")
WILDCARDS = {"0.0.0.0", "*", "[::]", "::"}
LOOPBACK = ("127.", "[::1]", "::1")

# Runs INSIDE the container. Kept to the stdlib: no image needs anything extra.
PROBE = r"""
import json, socket, sys
OUTBOUND = sys.argv[2]
out = []
for ip, port in json.loads(sys.argv[1]):
    s = socket.socket(); s.settimeout(1.5)
    try:
        s.connect((ip, port)); r = "open"
    except ConnectionRefusedError:
        r = "refused"
    except socket.timeout:
        r = "timeout"
    except OSError as e:
        r = "error:" + (e.strerror or str(e.errno))
    finally:
        s.close()
    out.append([ip, port, r])
try:
    s = socket.create_connection((OUTBOUND, 443), timeout=8); s.close(); egress = "ok"
except OSError as e:
    egress = "FAILED (" + (e.strerror or str(e)) + ")"
print(json.dumps({"targets": out, "egress": egress}))
"""


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def host_listeners() -> list[tuple[str, int]]:
    """(address, port) for every listening TCP socket, from `ss`."""
    found = []
    for line in _run(["ss", "-tlnH"]).splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        addr, _, port = cols[3].rpartition(":")
        addr = addr.split("%")[0]
        if port.isdigit():
            found.append((addr, int(port)))
    return found


def host_ipv4() -> list[str]:
    ips = []
    for line in _run(["ip", "-4", "-o", "addr", "show"]).splitlines():
        cols = line.split()
        if "inet" in cols:
            ip = cols[cols.index("inet") + 1].split("/")[0]
            if not ip.startswith("127."):
                ips.append(ip)
    return ips


def gateway(network: str) -> str:
    return _run(["docker", "network", "inspect", network, "--format",
                 "{{range .IPAM.Config}}{{.Gateway}}{{end}}"]).strip()


def pick_image(requested: str | None) -> str:
    for image in ([requested] if requested else DEFAULT_IMAGES):
        if subprocess.run(["docker", "image", "inspect", image],
                          capture_output=True).returncode == 0:
            return image
    sys.exit(f"none of {requested or DEFAULT_IMAGES} is present locally; pass --image")


def listening_on(listeners: list[tuple[str, int]], ip: str, port: int) -> bool:
    return any(p == port and (a in WILDCARDS or a == ip) for a, p in listeners)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8000, help="the API port (default 8000)")
    ap.add_argument("--image", help="image to probe from (needs python3)")
    ap.add_argument("--outbound-host", default=OUTBOUND_HOST,
                    help=f"public HTTPS host for the outbound check (default {OUTBOUND_HOST})")
    args = ap.parse_args()

    exposed = False
    egress_broken = False
    listeners = host_listeners()

    # 1. The API's own bind.
    api_addrs = sorted({a for a, p in listeners if p == args.port})
    print(f"API port {args.port}")
    if not api_addrs:
        print("  not listening (start it with ./run-local.sh to check the bind)")
    for addr in api_addrs:
        ok = addr.startswith(LOOPBACK)
        exposed |= not ok
        print(f"  listening on {addr:<12} {'loopback only' if ok else 'EXPOSED beyond loopback'}")
    if api_addrs:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/health", timeout=3) as r:
                print(f"  http://127.0.0.1:{args.port}/health -> {r.status}")
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            print(f"  http://127.0.0.1:{args.port}/health -> FAILED ({exc})")

    # 2. What a container can reach.
    image = pick_image(args.image)
    ports = sorted({p for a, p in listeners if not a.startswith(LOOPBACK)} | {args.port})
    host_ips = host_ipv4()
    for network, bridge in ((docker_net.ACTOR_NETWORK, docker_net.ACTOR_BRIDGE),
                            (docker_net.LOGIN_NETWORK, docker_net.LOGIN_BRIDGE)):
        state = docker_net.ensure_isolated_bridge(network, bridge, warn=print)
        gw = gateway(network)
        targets = [[ip, port] for ip in dict.fromkeys([gw, *host_ips]) for port in ports]
        raw = _run(["docker", "run", "--rm", "--network", network,
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--entrypoint", "python3", image, "-c", PROBE, json.dumps(targets),
                    args.outbound_host])
        print(f"\nfrom a container on {network} (bridge {bridge}, gateway {gw}, network {state})")
        report = json.loads(raw.strip().splitlines()[-1])
        print(f"  outbound https://{args.outbound_host} (DNS + TCP 443): {report['egress']}")
        egress_broken |= report["egress"] != "ok"
        for ip, port, result in report["targets"]:
            if result == "open":
                verdict = "REACHABLE"
                exposed = True
            elif listening_on(listeners, ip, port):
                verdict = "blocked"
            else:
                verdict = "closed"
            print(f"  {ip:>15}:{port:<6} {verdict:<10} ({result})")

    print("\nRESULT:", "EXPOSED -- see REACHABLE above" if exposed
          else "nothing on this host is reachable from these containers")
    if egress_broken:
        print("        OUTBOUND BROKEN -- containers cannot reach the internet; Actors and logins will fail")
        return 2
    return 1 if exposed else 0


if __name__ == "__main__":
    sys.exit(main())
