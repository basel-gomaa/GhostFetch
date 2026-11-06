#!/usr/bin/env python3
"""Build chrome-sandbox.json: Docker's default seccomp profile plus the least
Chrome's own sandbox needs.

    python3 deploy/seccomp/make_chrome_profile.py           # rewrite the profile
    python3 deploy/seccomp/make_chrome_profile.py --check   # exit 1 if it is stale

WHY. Chrome sandboxes its renderers in new user, PID and network namespaces
and then chroots them into an empty directory. Docker's default profile refuses
all of that to a container without CAP_SYS_ADMIN / CAP_SYS_CHROOT -- which ours
never have (--cap-drop ALL) -- so both browser images ran Chrome with
--no-sandbox, and a compromised page could read the signed-in profile.

WHAT CHANGES, and nothing else (test_seccomp_profile.py diffs the result
against the pinned default and fails on any other difference):

  clone   the namespace mask drops CLONE_NEWUSER | CLONE_NEWPID | CLONE_NEWNET.
          NEWNS, NEWUTS, NEWIPC and NEWCGROUP stay refused, as do setns, mount,
          pivot_root and clone3 (still ENOSYS, so callers fall back to clone,
          which is filtered).
  unshare the same three flags and no others. Chrome calls it while checking
          whether namespaces work and from its credentials code (measured:
          see the README section "Chrome's sandbox").
  chroot  allowed. The container user holds no capabilities, so the kernel
          refuses it everywhere except inside a user namespace the process
          has just created -- which is exactly where Chrome uses it.

THE COST, stated rather than hidden: a process in these containers can create
an unprivileged user namespace, which exposes more kernel code than the
default profile does. Only the containers that opt in get this file (the login
browser and Actors declaring chrome_sandbox); every other container keeps
Docker's built-in profile.

The base is Docker's own default, copied from moby at the exact engine version
this host runs (docker-default-29.9.2.json; moby/profiles v0.2.4 as vendored
by the docker-v29.9.2 tag). After a Docker upgrade, replace that file with the
new version's and re-run this script.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE / "docker-default-29.9.2.json"
OUT = HERE / "chrome-sandbox.json"

CLONE_NEWNS = 0x00020000
CLONE_NEWCGROUP = 0x02000000
CLONE_NEWUTS = 0x04000000
CLONE_NEWIPC = 0x08000000
CLONE_NEWUSER = 0x10000000
CLONE_NEWPID = 0x20000000
CLONE_NEWNET = 0x40000000

DEFAULT_MASK = (CLONE_NEWNS | CLONE_NEWCGROUP | CLONE_NEWUTS | CLONE_NEWIPC
                | CLONE_NEWUSER | CLONE_NEWPID | CLONE_NEWNET)          # 0x7E020000
# The namespaces Chrome's sandbox needs are taken OUT of the refused set.
CHROME_MASK = DEFAULT_MASK & ~(CLONE_NEWUSER | CLONE_NEWPID | CLONE_NEWNET)  # 0x0E020000


def build(base: dict) -> dict:
    assert DEFAULT_MASK == 2114060288, hex(DEFAULT_MASK)
    profile = copy.deepcopy(base)
    relaxed = 0
    for rule in profile["syscalls"]:
        if rule["names"] == ["clone"] and rule.get("action") == "SCMP_ACT_ALLOW":
            for arg in rule.get("args") or []:
                if arg.get("op") == "SCMP_CMP_MASKED_EQ" and arg.get("value") == DEFAULT_MASK:
                    arg["value"] = CHROME_MASK
                    relaxed += 1
            rule["comment"] = ("chrome-sandbox: CLONE_NEWUSER|NEWPID|NEWNET allowed for "
                               "Chrome's namespace sandbox; other namespace flags refused")
    # Both arch variants of the clone rule (x86/arm: flags are arg 0; s390: arg 1).
    if relaxed != 2:
        raise SystemExit(f"expected 2 clone rules with the default mask, found {relaxed}: "
                         "the base profile changed shape; review before regenerating")

    profile["syscalls"].append({
        "names": ["unshare"],
        "action": "SCMP_ACT_ALLOW",
        "args": [{"index": 0, "value": CHROME_MASK, "valueTwo": 0,
                  "op": "SCMP_CMP_MASKED_EQ"}],
        "comment": "chrome-sandbox: unshare limited to NEWUSER|NEWPID|NEWNET",
    })
    profile["syscalls"].append({
        "names": ["chroot"],
        "action": "SCMP_ACT_ALLOW",
        "comment": ("chrome-sandbox: the kernel still requires CAP_SYS_CHROOT, which "
                    "only exists inside a user namespace the process created"),
    })
    return profile


def render(profile: dict) -> str:
    return json.dumps(profile, indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="fail if chrome-sandbox.json is stale")
    args = ap.parse_args(argv)
    text = render(build(json.loads(BASE.read_text())))
    if args.check:
        if not OUT.is_file() or OUT.read_text() != text:
            print(f"{OUT} is stale: run {Path(__file__).name}", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
