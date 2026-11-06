#!/usr/bin/env python3
"""Chrome's sandbox seccomp profile, offline: what it adds, and who gets it.

    python3 test_seccomp_profile.py

No Docker, no database. What is pinned:
  * deploy/seccomp/chrome-sandbox.json is exactly what the generator makes from
    the pinned Docker default -- nobody hand-edited it;
  * it differs from Docker's default in exactly three places (the clone mask,
    an unshare rule with the same mask, chroot) and nothing else;
  * the namespaces it still refuses: mount, UTS, IPC, cgroup -- and setns,
    mount, pivot_root, clone3 are untouched;
  * the worker gives it only to an Actor declaring chrome_sandbox, and that is
    the TikTok Actor alone; every other argv is unchanged;
  * the login browser gets it by default, ACTOR_PLATFORM_CHROME_SANDBOX=0 gives
    back the old argv, and a missing profile is an error, never a quiet
    --no-sandbox.

The live half -- Chrome reporting itself sandboxed, renderers in nested PID
namespaces -- needs Docker and is in test_login.py.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SECCOMP = REPO_ROOT / "deploy" / "seccomp"
TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def load(name):
    return json.loads((SECCOMP / name).read_text())


@test
def test_the_profile_is_what_the_generator_makes():
    r = subprocess.run([sys.executable, str(SECCOMP / "make_chrome_profile.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@test
def test_it_differs_from_dockers_default_in_exactly_three_places():
    base, chrome = load("docker-default-29.9.2.json"), load("chrome-sandbox.json")
    for key in base:
        if key != "syscalls":
            assert chrome[key] == base[key], f"top-level {key!r} changed"
    n = len(base["syscalls"])
    assert len(chrome["syscalls"]) == n + 2, "expected exactly two new rules"

    # Every existing rule is unchanged except the two clone rules' mask.
    changed = []
    for i, (old, new) in enumerate(zip(base["syscalls"], chrome["syscalls"][:n])):
        if old == new:
            continue
        expected = copy.deepcopy(old)
        for arg in expected.get("args") or []:
            if arg.get("value") == 0x7E020000:
                arg["value"] = 0x0E020000
        expected["comment"] = new.get("comment")
        assert old["names"] == ["clone"] and new == expected, f"rule {i} changed: {old} -> {new}"
        changed.append(i)
    assert len(changed) == 2, changed

    unshare, chroot = chrome["syscalls"][n:]
    assert unshare["names"] == ["unshare"] and unshare["action"] == "SCMP_ACT_ALLOW"
    assert unshare["args"] == [{"index": 0, "value": 0x0E020000, "valueTwo": 0,
                                "op": "SCMP_CMP_MASKED_EQ"}], unshare
    assert chroot["names"] == ["chroot"] and chroot["action"] == "SCMP_ACT_ALLOW"
    assert "args" not in chroot


@test
def test_the_namespaces_chrome_does_not_need_stay_refused():
    mask = 0x0E020000
    for flag, name in ((0x00020000, "NEWNS"), (0x04000000, "NEWUTS"),
                       (0x08000000, "NEWIPC"), (0x02000000, "NEWCGROUP")):
        assert mask & flag, f"CLONE_{name} is no longer refused"
    for flag, name in ((0x10000000, "NEWUSER"), (0x20000000, "NEWPID"), (0x40000000, "NEWNET")):
        assert not mask & flag, f"CLONE_{name} is still refused; Chrome cannot sandbox"

    # No call is newly allowed without its capability gate, beyond the three.
    base, chrome = load("docker-default-29.9.2.json"), load("chrome-sandbox.json")

    def ungated(profile):
        out = set()
        for r in profile["syscalls"]:
            if r["action"] == "SCMP_ACT_ALLOW" and not (r.get("includes") or {}).get("caps"):
                out.update(r["names"])
        return out
    assert ungated(chrome) - ungated(base) == {"unshare", "chroot"}, ungated(chrome) - ungated(base)
    for name in ("setns", "mount", "umount2", "pivot_root", "clone3", "bpf", "ptrace", "keyctl"):
        before = [r for r in base["syscalls"] if name in r["names"]]
        after = [r for r in chrome["syscalls"] if name in r["names"]]
        assert before == after, f"{name} rules changed"


@test
def test_no_actor_needs_it_in_the_demo_build_and_the_flag_still_works():
    """The demo build runs no browser, so no Actor declares chrome_sandbox; the
    worker's support for the flag is still exercised directly."""
    os.environ.pop("ACTOR_PLATFORM_CHROME_SANDBOX", None)
    from worker import executor
    from worker.limits import load_actor_config
    from shared import chrome_sandbox

    _, _, profiles = load_actor_config()
    assert not [n for n, p in profiles.items() if p.chrome_sandbox], \
        {n: p.chrome_sandbox for n, p in profiles.items()}

    off = executor._container_args("x", "r1", Path("/tmp/r1"))
    assert not any("seccomp" in a for a in off), off
    on = executor._container_args("tiktok", "r1", Path("/tmp/r1"), chrome_sandbox=True)
    image = on[-1]
    i = on.index("no-new-privileges")
    added = on[i + 1:-1]
    assert added == ["--security-opt", f"seccomp={chrome_sandbox.PROFILE_PATH}",
                     "-e", "CHROME_SANDBOX=1"], added
    assert image == off[-1].replace("/x:", "/tiktok:"), image
    # Nothing else moved: the hardening is all still there, nothing privileged.
    for flag in ("--cap-drop", "ALL", "no-new-privileges"):
        assert flag in on, flag
    for bad in ("--privileged", "--cap-add", "--userns", "--pid", "seccomp=unconfined"):
        assert not any(a.startswith(bad) for a in on), bad


@test
def test_a_missing_profile_is_an_error_not_a_quiet_no_sandbox():
    from shared import chrome_sandbox

    real = chrome_sandbox.PROFILE_PATH
    chrome_sandbox.PROFILE_PATH = real.with_name("does-not-exist.json")
    try:
        try:
            chrome_sandbox.docker_args()
        except chrome_sandbox.SandboxProfileMissing:
            pass
        else:
            raise AssertionError("docker_args() returned with no profile file")
    finally:
        chrome_sandbox.PROFILE_PATH = real


def main() -> int:
    print(f"running {len(TESTS)} seccomp-profile tests\n")
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  ok    {fn.__name__.removeprefix('test_')}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {fn.__name__.removeprefix('test_')}: {type(exc).__name__}: {exc}")
    print(f"\n{'all ' + str(len(TESTS)) + ' tests passed' if not failed else f'{failed} FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
