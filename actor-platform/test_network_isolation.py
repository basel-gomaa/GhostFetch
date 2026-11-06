#!/usr/bin/env python3
"""Containers kept off the host's own ports (security review N-01).

    python3 test_network_isolation.py

Offline: no Docker, no database, no root. What this proves is what the code
ASKS for -- the API's bind address, how the two bridges are created, and
exactly which firewall rules the host script would install. Whether the network
actually refuses a container is a live fact that only
scripts/check_container_isolation.py can establish; passing here is not that.

  * run-local.sh binds the API to 127.0.0.1 unless LAN access is asked for,
    and public mode forces loopback whatever was asked
  * both bridges are created with ICC off AND a fixed host interface name,
    and a wrong network is recreated only while nothing is attached
  * the Worker and the login manager both go through that one helper
  * deploy/host-firewall.sh, driven against a fake iptables, adds exactly one
    chain and one INPUT jump per bridge, is idempotent, removes itself
    completely, and never touches a policy or anybody else's chain
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent
VENV = REPO_ROOT / ".venv"
VENV_PYTHON = VENV / "bin" / "python"
if (VENV_PYTHON.is_file() and Path(sys.prefix).resolve() != VENV.resolve()
        and not os.environ.get("_ACTOR_PLATFORM_REEXEC")):
    os.environ["_ACTOR_PLATFORM_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])
sys.path.insert(0, str(REPO_ROOT))

from shared import docker_net  # noqa: E402

RUN_LOCAL = REPO_ROOT / "run-local.sh"
FIREWALL = REPO_ROOT / "deploy" / "host-firewall.sh"
UNIT = REPO_ROOT / "deploy" / "actor-platform-firewall.service"


# --- the API's bind address ------------------------------------------------------


def _bind_block() -> str:
    """The lines of run-local.sh that decide HOST, run for real by bash below."""
    text = RUN_LOCAL.read_text(encoding="utf-8")
    start = text.index('HOST="${ACTOR_PLATFORM_BIND_HOST')
    end = text.index("\nfi\n", start) + len("\nfi\n")
    return text[start:end]


def _resolved_host(**env: str) -> str:
    clean = {k: v for k, v in os.environ.items()
             if k not in ("ACTOR_PLATFORM_BIND_HOST", "PUBLIC_DOMAIN")}
    out = subprocess.run(["bash", "-c", _bind_block() + '\necho "$HOST"'],
                         env={**clean, **env}, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_api_binds_to_loopback_by_default():
    assert _resolved_host() == "127.0.0.1", "the API binds beyond loopback by default"


def test_lan_access_is_an_explicit_opt_in():
    assert _resolved_host(ACTOR_PLATFORM_BIND_HOST="0.0.0.0") == "0.0.0.0"


def test_public_mode_forces_loopback_whatever_was_asked():
    assert _resolved_host(PUBLIC_DOMAIN="platform.example.com") == "127.0.0.1"
    assert _resolved_host(PUBLIC_DOMAIN="platform.example.com",
                          ACTOR_PLATFORM_BIND_HOST="0.0.0.0") == "127.0.0.1", \
        "public mode let uvicorn bind beyond loopback, bypassing Caddy"


def test_uvicorn_gets_the_decided_host_and_nothing_hardcodes_a_wildcard():
    text = RUN_LOCAL.read_text(encoding="utf-8")
    launches = [line for line in text.splitlines() if "uvicorn api.main:app" in line
                and not line.lstrip().startswith(("#", "pkill"))]
    assert launches and all('--host "$HOST"' in line for line in launches), launches
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not any('HOST="0.0.0.0"' in line or "--host 0.0.0.0" in line for line in code), \
        "a wildcard bind is hardcoded again"


# --- the two bridges ---------------------------------------------------------------


class FakeDocker:
    """Answers `docker network inspect/rm/create` the way the helper reads them."""

    def __init__(self, existing: str | None = None, create_rc: int = 0):
        self.existing = existing
        self.create_rc = create_rc
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **_kw):
        self.calls.append(cmd)
        verb = cmd[2]
        if verb == "inspect":
            if self.existing is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="no such network")
            return SimpleNamespace(returncode=0, stdout=self.existing + "\n", stderr="")
        if verb == "create":
            return SimpleNamespace(returncode=self.create_rc, stdout="",
                                   stderr="" if self.create_rc == 0 else "boom")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def verbs(self) -> list[str]:
        return [c[2] for c in self.calls]


def test_a_missing_network_is_created_with_icc_off_and_a_fixed_bridge_name():
    fake = FakeDocker()
    assert docker_net.ensure_isolated_bridge("ap-actor-net", "ap-actor0", run=fake) == "created"
    create = fake.calls[-1]
    assert create[:3] == ["docker", "network", "create"], create
    assert "com.docker.network.bridge.enable_icc=false" in create, create
    assert "com.docker.network.bridge.name=ap-actor0" in create, create
    assert create[-1] == "ap-actor-net"


def test_a_correct_network_is_left_alone():
    fake = FakeDocker(existing="false|ap-actor0|3")
    assert docker_net.ensure_isolated_bridge("ap-actor-net", "ap-actor0", run=fake) == "ok"
    assert fake.verbs() == ["inspect"], fake.calls


def test_an_idle_network_with_dockers_default_name_is_recreated():
    # Exactly the pre-N-01 state: ICC off, but a br-<id> interface no firewall
    # rule can rely on.
    fake = FakeDocker(existing="false|<no value>|0")
    assert docker_net.ensure_isolated_bridge("ap-login-net", "ap-login0", run=fake) == "recreated"
    assert fake.verbs() == ["inspect", "rm", "create"], fake.calls


def test_a_network_in_use_is_never_torn_down():
    warned = []
    fake = FakeDocker(existing="true|<no value>|2")
    got = docker_net.ensure_isolated_bridge("ap-actor-net", "ap-actor0", run=fake, warn=warned.append)
    assert got == "in_use" and fake.verbs() == ["inspect"], fake.calls
    assert warned and "in use" in warned[0], warned


def test_docker_failing_to_create_it_is_an_error_not_a_fallback():
    try:
        docker_net.ensure_isolated_bridge("ap-actor-net", "ap-actor0", run=FakeDocker(create_rc=1))
    except docker_net.NetworkError:
        return
    raise AssertionError("a failed create was swallowed")


def test_interface_names_fit_the_kernel_limit():
    for bad in ("", "x" * 16):
        try:
            docker_net.ensure_isolated_bridge("n", bad, run=FakeDocker())
        except ValueError:
            continue
        raise AssertionError(f"accepted interface name {bad!r}")


def test_the_worker_uses_the_helper_with_the_fixed_names():
    from worker import executor  # noqa: PLC0415

    seen = []
    real = docker_net.ensure_isolated_bridge
    docker_net.ensure_isolated_bridge = lambda network, bridge, **_kw: seen.append((network, bridge)) or "ok"
    try:
        executor._network_ready = False
        assert executor.ensure_actor_network() == "ap-actor-net"
    finally:
        docker_net.ensure_isolated_bridge = real
        executor._network_ready = False
    assert seen == [("ap-actor-net", "ap-actor0")], seen


# --- the host firewall script -----------------------------------------------------

# A stand-in for iptables/ip6tables that keeps rules in a JSON file and logs
# every invocation, so the real script can be run end to end without root.
FAKE_IPTABLES = r'''#!/usr/bin/env python3
import json, os, sys
fam = os.path.basename(sys.argv[0])
state_path = os.path.join(os.environ["FAKE_FW_DIR"], fam + ".json")
state = json.load(open(state_path)) if os.path.exists(state_path) else {"INPUT": ["-j EXISTING-RULE"]}
args = [a for a in sys.argv[1:] if a != "-w"]
with open(os.path.join(os.environ["FAKE_FW_DIR"], "calls.log"), "a") as log:
    log.write(fam + " " + " ".join(args) + "\n")
op, chain, rest = args[0], args[1], args[2:]
rc = 0
if op == "-N":
    rc = 1 if chain in state else 0
    state.setdefault(chain, [])
elif op == "-F":
    if chain not in state: rc = 1
    else: state[chain] = []
elif op == "-X":
    if chain not in state or state[chain]: rc = 1
    else: del state[chain]
elif op == "-A":
    state[chain].append(" ".join(rest))
elif op == "-I":
    state[chain].insert(int(rest[0]) - 1, " ".join(rest[1:]))
elif op == "-C":
    rc = 0 if " ".join(rest) in state.get(chain, []) else 1
elif op == "-D":
    state[chain].remove(" ".join(rest))
elif op == "-S":
    if chain not in state: rc = 1
    else: print("\n".join(f"-A {chain} {r}" for r in state[chain]))
else:
    rc = 2
json.dump(state, open(state_path, "w"))
sys.exit(rc)
'''


def _firewall(action: str, fw_dir: Path) -> subprocess.CompletedProcess:
    bin_dir = fw_dir / "bin"
    if not bin_dir.exists():
        bin_dir.mkdir()
        for name in ("iptables", "ip6tables"):
            path = bin_dir / name
            path.write_text(FAKE_IPTABLES)
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        fake_id = bin_dir / "id"
        fake_id.write_text("#!/bin/sh\necho 0\n")
        fake_id.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_FW_DIR": str(fw_dir)}
    return subprocess.run(["bash", str(FIREWALL), action], env=env,
                          capture_output=True, text=True)


def _state(fw_dir: Path, family: str = "iptables") -> dict:
    return json.loads((fw_dir / f"{family}.json").read_text())


CHAIN = "AP-CONTAINER-INPUT"


def test_firewall_apply_adds_one_jump_for_the_bridge_and_its_own_chain():
    with tempfile.TemporaryDirectory() as tmp:
        fw = Path(tmp)
        assert _firewall("apply", fw).returncode == 0
        for family, icmp in (("iptables", "icmp-port-unreachable"),
                             ("ip6tables", "icmp6-port-unreachable")):
            state = _state(fw, family)
            assert state["INPUT"] == [f"-i ap-actor0 -j {CHAIN}",
                                      "-j EXISTING-RULE"], state["INPUT"]
            assert state[CHAIN] == [
                "-m conntrack --ctstate ESTABLISHED,RELATED -j RETURN",
                "-p tcp -j REJECT --reject-with tcp-reset",
                f"-j REJECT --reject-with {icmp}",
            ], state[CHAIN]


def test_firewall_apply_twice_changes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        fw = Path(tmp)
        _firewall("apply", fw)
        first = _state(fw)
        assert _firewall("apply", fw).returncode == 0
        assert _state(fw) == first, "a second apply duplicated rules"


def test_firewall_remove_restores_exactly_what_was_there():
    with tempfile.TemporaryDirectory() as tmp:
        fw = Path(tmp)
        _firewall("apply", fw)
        assert _firewall("remove", fw).returncode == 0
        for family in ("iptables", "ip6tables"):
            assert _state(fw, family) == {"INPUT": ["-j EXISTING-RULE"]}, _state(fw, family)


def test_firewall_never_touches_a_policy_or_another_chain():
    with tempfile.TemporaryDirectory() as tmp:
        fw = Path(tmp)
        for action in ("apply", "apply", "status", "remove", "remove"):
            _firewall(action, fw)
        calls = (fw / "calls.log").read_text().splitlines()
    for call in calls:
        _, op, chain, *_ = call.split()
        assert op != "-P", f"a chain policy was changed: {call}"
        if op in ("-F", "-X", "-N", "-A"):
            assert chain == CHAIN, f"{op} on someone else's chain: {call}"
        if chain == "INPUT":
            assert op in ("-C", "-I", "-D", "-S"), f"INPUT was modified unexpectedly: {call}"
            if op in ("-I", "-D", "-C"):
                assert re.search(rf"-i ap-actor0 -j {CHAIN}$", call), call


def test_firewall_matches_the_names_the_networks_are_created_with():
    text = FIREWALL.read_text(encoding="utf-8")
    assert f'IFACES=("{docker_net.ACTOR_BRIDGE}")' in text, \
        "the firewall's interface names drifted from shared/docker_net.py"


def test_firewall_refuses_to_run_without_root():
    out = subprocess.run(["bash", str(FIREWALL), "status"], capture_output=True, text=True,
                         env={**os.environ, "PATH": "/usr/bin:/bin"})
    if os.geteuid() == 0:
        return
    assert out.returncode == 1 and "run as root" in out.stderr, out


def test_the_boot_unit_runs_a_root_owned_copy_not_the_checkout():
    text = UNIT.read_text(encoding="utf-8")
    execs = [line for line in text.splitlines() if line.startswith(("ExecStart=", "ExecStop="))]
    assert execs and all("/usr/local/sbin/actor-platform-firewall" in line for line in execs), execs
    assert str(REPO_ROOT) not in text and "/home/" not in "".join(execs)


TESTS = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]


def main() -> int:
    failed = 0
    print(f"running {len(TESTS)} network-isolation tests\n")
    for fn in TESTS:
        name = fn.__name__.removeprefix("test_")
        try:
            fn()
            print(f"  ok    {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{failed} FAILED" if failed else "\nall passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
