#!/usr/bin/env python3
"""Per-run credentials: injection, isolation, redaction and write-back.

    ACTOR_PLATFORM_DATABASE_URL=postgresql://... python3 test_secrets.py

Needs a THROWAWAY database. Docker is needed only for the end-to-end leak test,
which is skipped with a stated reason when Docker is unavailable.

THE PROPERTY UNDER TEST is that a run receives ITS OWN submitter's session and
that the value reaches nothing except the container. The platform already had
this for ONE platform-wide secret; the risk in making it per-run is that the
mechanism which makes it per-run (a per-child environment) is one refactor away
from becoming a process-wide mutation, and the failure mode of that is silent:
user A's session in user B's container, with nothing in any log to show it.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if not (os.environ.get("ACTOR_PLATFORM_DATABASE_URL") or "").strip():
    print("ACTOR_PLATFORM_DATABASE_URL must point at a THROWAWAY database",
          file=sys.stderr)
    raise SystemExit(2)

from db.pool import pool  # noqa: E402
from jobqueue.file_queue import FileJobQueue  # noqa: E402
from registry.pg_registry import PgRunRegistry  # noqa: E402
from shared.crypto import KeyRing, generate_key, session_aad  # noqa: E402
from shared.models import ACTOR_SECRETS_DIR, ENV_SESSION_OUT  # noqa: E402
from worker.executor import _container_args  # noqa: E402
from worker.limits import ActorProfile  # noqa: E402
from worker.run_secrets import (  # noqa: E402
    DbSecretResolver,
    NullSecretResolver,
    SessionUnavailable,
)

TESTS = []
KEYRING = KeyRing.from_env({"ACTOR_PLATFORM_SECRET_KEY": generate_key()})

SECRET = "xs=THE-VALUE-THAT-MUST-NOT-APPEAR-ANYWHERE"
FB_PROFILE = ActorProfile(
    session_cookies=("c_user", "xs"), session_id_cookie="c_user",
    session_env="FB_SESSION", session_out=True,
)


def test(fn):
    TESTS.append(fn)
    return fn


def jar(c_user="61590000", xs="secret-xs"):
    return json.dumps({"cookies": [
        {"name": "c_user", "value": c_user, "domain": ".x.com", "path": "/"},
        {"name": "xs", "value": xs, "domain": ".x.com", "path": "/"},
    ], "origins": []})


def reset():
    with pool().connection() as conn:
        conn.execute("TRUNCATE users, runs CASCADE")


def make_account(session: str, platform: str = "facebook") -> tuple[str, str]:
    """A user with one connected account. Returns (user_id, account_id)."""
    reset()
    with pool().connection() as conn:
        user = conn.execute(
            "INSERT INTO users (email,username,password_hash) "
            "VALUES ('a@x.com','a','h') RETURNING id").fetchone()["id"]
        account = conn.execute(
            "INSERT INTO social_accounts (user_id, platform, session_ciphertext, "
            "session_nonce, key_version, status) VALUES (%s,%s,'\\x00','\\x00',1,'connected') "
            "RETURNING id", (user, platform)).fetchone()["id"]
        ct, nonce, ver = KEYRING.seal(session, session_aad(str(account), platform))
        conn.execute(
            "UPDATE social_accounts SET session_ciphertext=%s, session_nonce=%s, "
            "key_version=%s WHERE id=%s", (ct, nonce, ver, account))
    return str(user), str(account)


def resolver() -> DbSecretResolver:
    return DbSecretResolver(pool, KEYRING, lambda _actor: FB_PROFILE.session_cookies)


def a_run(registry, owner_id, account_id):
    return registry.create("facebook", {"target": "nasa"}, owner_id=owner_id,
                           social_account_id=account_id)


# --- injection --------------------------------------------------------------


@test
def test_a_run_receives_its_own_submitters_session():
    with tempfile.TemporaryDirectory() as tmp:
        reg = PgRunRegistry(Path(tmp) / "runs")
        user, account = make_account(jar(xs="alice-xs"))
        run = a_run(reg, user, account)
        env = resolver().for_run(run, FB_PROFILE)
        assert list(env) == ["FB_SESSION"], env
        assert "alice-xs" in env["FB_SESSION"]


@test
def test_two_runs_get_two_different_sessions_concurrently():
    """The thing a process-wide os.environ mutation would break, and break
    SILENTLY: with threads racing, each container must still see its own."""
    with tempfile.TemporaryDirectory() as tmp:
        reg = PgRunRegistry(Path(tmp) / "runs")
        reset()
        with pool().connection() as conn:
            accounts = {}
            for name in ("alice", "bob"):
                uid = conn.execute(
                    "INSERT INTO users (email,username,password_hash) "
                    "VALUES (%s,%s,'h') RETURNING id",
                    (f"{name}@x.com", name)).fetchone()["id"]
                aid = conn.execute(
                    "INSERT INTO social_accounts (user_id, platform, session_ciphertext,"
                    " session_nonce, key_version, status) "
                    "VALUES (%s,'facebook','\\x00','\\x00',1,'connected') RETURNING id",
                    (uid,)).fetchone()["id"]
                ct, nonce, ver = KEYRING.seal(jar(xs=f"{name}-xs"),
                                              session_aad(str(aid), "facebook"))
                conn.execute("UPDATE social_accounts SET session_ciphertext=%s,"
                             " session_nonce=%s, key_version=%s WHERE id=%s",
                             (ct, nonce, ver, aid))
                accounts[name] = (str(uid), str(aid))

        res, seen, errors = resolver(), {}, []

        def resolve(name):
            uid, aid = accounts[name]
            try:
                for _ in range(15):
                    env = res.for_run(a_run(reg, uid, aid), FB_PROFILE)
                    seen.setdefault(name, set()).add(env["FB_SESSION"])
            except Exception:  # noqa: BLE001
                errors.append(traceback.format_exc())

        threads = [threading.Thread(target=resolve, args=(n,)) for n in ("alice", "bob")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors[0]
        for name in ("alice", "bob"):
            assert len(seen[name]) == 1, f"{name} saw {len(seen[name])} different sessions"
            assert f"{name}-xs" in seen[name].pop(), f"{name} got the wrong session"


@test
def test_a_missing_or_undecryptable_session_is_a_platform_failure():
    """SessionUnavailable, not an auth failure. Telling the user their session
    expired when OUR injection broke would cost them a real login."""
    with tempfile.TemporaryDirectory() as tmp:
        reg = PgRunRegistry(Path(tmp) / "runs")
        user, account = make_account(jar())

        # a run that names no account at all
        run = reg.create("facebook", {}, owner_id=user)
        try:
            resolver().for_run(run, FB_PROFILE)
            raise AssertionError("expected SessionUnavailable")
        except SessionUnavailable as exc:
            assert "names no social account" in str(exc), exc

        # a ciphertext sealed for a DIFFERENT row -- the AAD must refuse it
        other, _, _ = KEYRING.seal(jar(), session_aad("00000000-0000-0000-0000-000000000000",
                                                      "facebook"))
        with pool().connection() as conn:
            conn.execute("UPDATE social_accounts SET session_ciphertext=%s WHERE id=%s",
                         (other, account))
        try:
            resolver().for_run(a_run(reg, user, account), FB_PROFILE)
            raise AssertionError("a ciphertext from another row was accepted")
        except SessionUnavailable as exc:
            assert "could not be decrypted" in str(exc), exc


@test
def test_an_actor_that_needs_no_session_is_given_none():
    with tempfile.TemporaryDirectory() as tmp:
        reg = PgRunRegistry(Path(tmp) / "runs")
        user, account = make_account(jar())
        run = a_run(reg, user, account)
        assert resolver().for_run(run, ActorProfile()) == {}
        assert NullSecretResolver().for_run(run, FB_PROFILE) == {}


@test
def test_an_optional_session_with_no_account_runs_anonymously():
    """session_optional + no account = an anonymous run, NOT SessionUnavailable.

    The API lets such a run through with no account when the user has none
    (api/main.py: `profile.session_optional`), so a worker that refused it produced a
    PLATFORM_ERROR in 0.0 s for every anonymous run of an Actor whose session
    is optional - measured on a live Threads run, which is the one Actor whose
    collector works either way. The two layers must agree.
    """
    optional = ActorProfile(session_cookies=("sessionid",),
                            session_env="THREADS_SESSION", session_optional=True)
    with tempfile.TemporaryDirectory() as tmp:
        reg = PgRunRegistry(Path(tmp) / "runs")
        user, account = make_account(jar())
        anonymous = reg.create("threads", {}, owner_id=user)
        assert resolver().for_run(anonymous, optional) == {}

        # ...but an account that IS named still gets every check it always did:
        # optional does not mean "skip validation", it means "not required".
        with pool().connection() as conn:
            conn.execute("UPDATE social_accounts SET status='revoked' WHERE id=%s",
                         (account,))
        try:
            resolver().for_run(a_run(reg, user, account), optional)
            raise AssertionError("a revoked account was used")
        except SessionUnavailable as exc:
            assert "disconnected" in str(exc), exc

        # and a REQUIRED session is still refused without an account.
        try:
            resolver().for_run(reg.create("facebook", {}, owner_id=user), FB_PROFILE)
            raise AssertionError("a required session was waived")
        except SessionUnavailable as exc:
            assert "names no social account" in str(exc), exc


# --- the value must not escape ---------------------------------------------


@test
def test_the_value_never_reaches_the_container_argv():
    args = _container_args("facebook", "20260101-000001-aaaaaa", Path("/tmp/probe"),
                           secret_names=["FB_SESSION"])
    assert "FB_SESSION" in args, "the NAME must be passed so docker resolves it"
    assert not any(SECRET in str(a) for a in args), "the VALUE reached the argv"
    # "-e NAME", never "-e NAME=value"
    assert not any(str(a).startswith("FB_SESSION=") for a in args), args


@test
def test_the_credential_mount_is_outside_the_run_directory():
    """storage/runs/<id>/ is what GET /runs/{id}/... serves from. A live session
    written there would sit in the platform's public artifact tree."""
    run_dir = Path("/tmp/probe/20260101-000001-aaaaaa")
    scratch = Path("/var/lib/actor-platform/sessions/20260101-000001-aaaaaa")
    args = _container_args("facebook", "20260101-000001-aaaaaa", run_dir,
                           secret_names=["FB_SESSION"], secrets_dir=scratch)
    assert f"{scratch}:{ACTOR_SECRETS_DIR}" in args, args
    assert str(scratch).startswith(str(run_dir)) is False
    assert f"{ENV_SESSION_OUT}={ACTOR_SECRETS_DIR}/session.json" in args, args


@test
def test_no_mount_and_no_env_when_the_actor_does_not_ask():
    """An Actor without session_out gets the argv it always got."""
    plain = _container_args("hello_world", "20260101-000001-aaaaaa", Path("/tmp/probe"))
    assert not any(ACTOR_SECRETS_DIR in str(a) for a in plain), plain
    assert not any(ENV_SESSION_OUT in str(a) for a in plain), plain


@test
def test_a_secret_is_redacted_out_of_the_log():
    """log.txt is served over HTTP. No Actor prints its session deliberately,
    but a traceback through a large vendored collector could."""
    from worker.executor import _execute

    code, lines = _execute(
        ["sh", "-c", f'echo "cookie header: {SECRET}"; echo second line'],
        "probe", redact=[SECRET],
    )
    assert code == 0, code
    body = "\n".join(lines)
    assert SECRET not in body, f"the secret survived into the log: {body}"
    assert "[redacted]" in body, body
    assert "second line" in body, "redaction must not eat other output"


@test
def test_the_child_environment_is_not_the_process_environment():
    """Popen(env=) is per-child. os.environ must be untouched, or concurrent
    runs would leak into each other."""
    from worker.executor import _execute

    before = dict(os.environ)
    code, lines = _execute(
        ["sh", "-c", 'echo "$PROBE_SECRET"'], "probe",
        env={**os.environ, "PROBE_SECRET": "handed-to-the-child"},
    )
    assert code == 0
    assert "handed-to-the-child" in "\n".join(lines), lines
    assert "PROBE_SECRET" not in os.environ, "the worker's own environment was mutated"
    assert dict(os.environ) == before


# --- write-back -------------------------------------------------------------


@test
def test_a_refreshed_session_replaces_the_stored_one():
    """Without this every stored session decays to death, and it presents as
    the site blocking you rather than as a bug."""
    with tempfile.TemporaryDirectory() as tmp:
        reg = PgRunRegistry(Path(tmp) / "runs")
        user, account = make_account(jar(xs="old-xs"))
        run = a_run(reg, user, account)

        assert resolver().absorb_refreshed(run, jar(xs="rotated-xs")) is True
        env = resolver().for_run(a_run(reg, user, account), FB_PROFILE)
        assert "rotated-xs" in env["FB_SESSION"], "the rotation was not stored"
        assert "old-xs" not in env["FB_SESSION"]

        with pool().connection() as conn:
            row = conn.execute("SELECT status, last_verified_at, consecutive_auth_failures "
                               "FROM social_accounts WHERE id=%s", (account,)).fetchone()
        assert row["status"] == "connected"
        assert row["last_verified_at"] is not None, "a successful run IS a session check"
        assert row["consecutive_auth_failures"] == 0


@test
def test_a_logged_out_jar_is_refused_rather_than_stored():
    """A run that ended on the login wall holds a jar with the identity cookie
    and no authentication cookie. Storing it would destroy a good session."""
    with tempfile.TemporaryDirectory() as tmp:
        reg = PgRunRegistry(Path(tmp) / "runs")
        user, account = make_account(jar(xs="good-xs"))
        run = a_run(reg, user, account)

        logged_out = json.dumps({"cookies": [{"name": "c_user", "value": "61590000"}]})
        assert resolver().absorb_refreshed(run, logged_out) is False
        env = resolver().for_run(a_run(reg, user, account), FB_PROFILE)
        assert "good-xs" in env["FB_SESSION"], "a logged-out jar overwrote a good session"


# --- expiry -----------------------------------------------------------------


@test
def test_only_the_confident_reason_expires_an_account_on_one_failure():
    with tempfile.TemporaryDirectory() as tmp:
        reg = PgRunRegistry(Path(tmp) / "runs")

        def status_after(reasons):
            user, account = make_account(jar())
            run = a_run(reg, user, account)
            for reason in reasons:
                resolver().report_auth_failure(run, reason)
            with pool().connection() as conn:
                return conn.execute("SELECT status, expired_reason FROM social_accounts "
                                    "WHERE id=%s", (account,)).fetchone()

        assert status_after(["expired"])["status"] == "expired", "the measured signal must act at once"
        assert status_after(["unknown"])["status"] == "connected", "one soft failure must not expire"
        assert status_after(["unknown", "unknown"])["status"] == "expired", "two in a row must"


# --- one run per connected account ------------------------------------------


@test
def test_the_in_process_gate_lets_different_sessions_through():
    """The eligibility predicate, which is what stops a busy session's job from
    ever being LEASED -- so it stays plainly PENDING where another Worker and
    the reconciler can both still see it."""
    from jobqueue.base import Job
    from worker.consumer import _Slots

    slots = _Slots(4, lambda a: None, lambda a: 1 if a == "facebook" else None)
    alice = Job(run_id="r1", actor="facebook", concurrency_key="social:alice")
    alice_again = Job(run_id="r2", actor="facebook", concurrency_key="social:alice")
    bob = Job(run_id="r3", actor="facebook", concurrency_key="social:bob")

    assert slots.may_run(alice) and slots.take_key(alice)
    assert not slots.may_run(alice_again), "two runs on ONE session"
    assert slots.may_run(bob), "two DIFFERENT sessions must not block each other"
    assert slots.may_run(Job(run_id="r4", actor="hello_world")), "a keyless actor"

    slots.release_key("social:alice")
    assert slots.may_run(alice_again)
    assert slots._key_running == {}, "released keys must not accumulate for ever"


@test
def test_the_database_lease_settles_the_cross_process_race():
    """The reason the in-process counter is not enough.

    That counter lives in ONE process, so two Workers would both admit a run on
    the same session -- the exact thing the old per-Actor cap CLAIMED to prevent
    and did not. Simulated here by two resolvers, which is what two Workers are.
    """
    with tempfile.TemporaryDirectory() as tmp:
        reg = PgRunRegistry(Path(tmp) / "runs")
        user, account = make_account(jar())
        first, second = a_run(reg, user, account), a_run(reg, user, account)

        worker_a, worker_b = resolver(), resolver()
        assert worker_a.acquire_session(first) is True
        assert worker_b.acquire_session(second) is False, (
            "two processes both claimed one session"
        )

        worker_a.release_session(first)
        assert worker_b.acquire_session(second) is True, "the lease was not released"
        worker_b.release_session(second)


@test
def test_a_lease_is_idempotent_for_its_own_run():
    """The queue delivers at-least-once, so the same run can arrive twice."""
    with tempfile.TemporaryDirectory() as tmp:
        reg = PgRunRegistry(Path(tmp) / "runs")
        user, account = make_account(jar())
        run = a_run(reg, user, account)
        assert resolver().acquire_session(run) is True
        assert resolver().acquire_session(run) is True, "a redelivery must not deadlock itself"
        resolver().release_session(run)


@test
def test_a_lease_left_by_a_dead_worker_is_reclaimed():
    """Otherwise one killed Worker strands an account for ever."""
    with tempfile.TemporaryDirectory() as tmp:
        reg = PgRunRegistry(Path(tmp) / "runs")
        user, account = make_account(jar())
        stranded, mine = a_run(reg, user, account), a_run(reg, user, account)

        # A lease owned by a pid that cannot be alive, in this host's own format.
        import socket
        with pool().connection() as conn:
            conn.execute(
                "INSERT INTO session_leases (social_account_id, run_id, owner) "
                "VALUES (%s,%s,%s)",
                (account, stranded.run_id, f"{socket.gethostname()}:999999"))

        assert resolver().acquire_session(mine) is True, "a dead owner's lease was not reclaimed"
        with pool().connection() as conn:
            row = conn.execute("SELECT run_id FROM session_leases WHERE social_account_id=%s",
                               (account,)).fetchone()
        assert row["run_id"] == mine.run_id, row


@test
def test_releasing_never_steals_a_lease_that_moved_on():
    """A release is scoped to its OWN run. After ours was reaped as dead and
    another run took the lease, our late release must not free theirs."""
    with tempfile.TemporaryDirectory() as tmp:
        reg = PgRunRegistry(Path(tmp) / "runs")
        user, account = make_account(jar())
        old, new = a_run(reg, user, account), a_run(reg, user, account)

        assert resolver().acquire_session(new) is True
        resolver().release_session(old)  # the late, stale release
        with pool().connection() as conn:
            row = conn.execute("SELECT run_id FROM session_leases WHERE social_account_id=%s",
                               (account,)).fetchone()
        assert row is not None and row["run_id"] == new.run_id, "a live lease was stolen"
        resolver().release_session(new)


@test
def test_a_busy_session_returns_the_job_unrun_rather_than_losing_it():
    """SessionBusy must NACK. Acking would silently discard the run, and letting
    recover() reclaim it would burn an attempt for work that never started."""
    from jobqueue.base import Job
    from worker.consumer import process_job
    from worker.limits import ExecutionLimits

    with tempfile.TemporaryDirectory() as tmp:
        reg = PgRunRegistry(Path(tmp) / "runs")
        queue = FileJobQueue(str(Path(tmp) / "queue"))
        user, account = make_account(jar())
        holder, mine = a_run(reg, user, account), a_run(reg, user, account)

        held = resolver()
        assert held.acquire_session(holder) is True

        queue.enqueue(Job(run_id=mine.run_id, actor="facebook",
                          concurrency_key=f"social:{account}"))
        job = queue.dequeue()
        assert job is not None and job.attempts == 0

        # The image build runs before the session lease. This test is about
        # the lease, so the build is stubbed: CI has no vendored Facebook
        # collector to build from, and building it proves nothing here.
        from worker import executor
        original_preflight, executor._preflight = executor._preflight, (lambda *a, **k: None)
        try:
            result = process_job(job, queue=queue, registry=reg, limits=ExecutionLimits(),
                                 resolver=resolver(), profile=FB_PROFILE)
        finally:
            executor._preflight = original_preflight
        assert result is None
        assert queue.depth() == 1, "the job was not returned to the queue"
        assert queue.inflight() == 0, "the lease was not given back"
        again = queue.dequeue()
        assert again.run_id == mine.run_id
        assert again.attempts == 0, "a busy session must not cost an attempt"
        assert reg.get(mine.run_id).status.value == "PENDING", "the run was moved"

        held.release_session(holder)


@test
def test_a_lost_lease_leaves_the_run_untouched():
    """Losing the lease must change NOTHING -- the run stays PENDING, not
    RUNNING-and-walked-back."""
    from worker.executor import _run_prepared
    from worker.limits import ExecutionLimits

    with tempfile.TemporaryDirectory() as tmp:
        reg = PgRunRegistry(Path(tmp) / "runs")
        user, account = make_account(jar())
        holder, mine = a_run(reg, user, account), a_run(reg, user, account)
        held = resolver()
        assert held.acquire_session(holder) is True

        from worker.run_secrets import SessionBusy
        try:
            _run_prepared(mine, registry=reg, limits=ExecutionLimits(),
                          resolver=resolver(), profile=FB_PROFILE)
            raise AssertionError("expected SessionBusy")
        except SessionBusy:
            pass
        assert reg.get(mine.run_id).status.value == "PENDING", "the run was claimed anyway"
        assert reg.get(mine.run_id).attempts == 0, "a lost lease cost an attempt"
        held.release_session(holder)


# --- report delivery --------------------------------------------------------


@test
def test_a_report_is_e_mailed_once_and_only_once():
    """The queue is at-least-once, so a finished job CAN arrive twice. Nobody
    wants the same report twice, so report_sent_at is the guard."""
    import delivery.mailer as mailer

    with tempfile.TemporaryDirectory() as tmp:
        reg = PgRunRegistry(Path(tmp) / "runs")
        reset()
        with pool().connection() as conn:
            uid = conn.execute(
                "INSERT INTO users (email,username,password_hash) "
                "VALUES ('alice@gmail.com','alice','h') RETURNING id").fetchone()["id"]
        run = reg.create("facebook", {"target": "nasa"}, owner_id=str(uid))
        reg.mark_running(run.run_id)
        finished = reg.complete(run.run_id, status=__import__(
            "shared.models", fromlist=["RunStatus"]).RunStatus.SUCCEEDED, exit_code=0)

        sent = []
        real = mailer.send_report
        mailer.send_report = lambda to, subject, body, attach=None, environ=None: (
            sent.append((to, subject, list(attach or []))) or True)
        try:
            assert resolver().deliver_report(finished, []) is True
            assert len(sent) == 1, sent
            to, subject, _ = sent[0]
            # The recipient is the address they SIGNED UP with, resolved here --
            # it never travelled in the input document.
            assert to == "alice@gmail.com", to
            assert run.run_id in subject, subject

            # A redelivery of the same finished job must not send again.
            assert resolver().deliver_report(finished, []) is False
            assert len(sent) == 1, f"the report was sent {len(sent)} times"
        finally:
            mailer.send_report = real

        with pool().connection() as conn:
            row = conn.execute("SELECT report_sent_at FROM runs WHERE run_id=%s",
                               (run.run_id,)).fetchone()
        assert row["report_sent_at"] is not None


@test
def test_delivery_failure_never_touches_the_run():
    """A forty-minute scrape must not be reported as failed because a mail
    server was down. The artifacts are already on disk and served by the API."""
    import delivery.mailer as mailer

    with tempfile.TemporaryDirectory() as tmp:
        reg = PgRunRegistry(Path(tmp) / "runs")
        reset()
        with pool().connection() as conn:
            uid = conn.execute(
                "INSERT INTO users (email,username,password_hash) "
                "VALUES ('alice@gmail.com','alice','h') RETURNING id").fetchone()["id"]
        run = reg.create("facebook", {}, owner_id=str(uid))
        reg.mark_running(run.run_id)
        from shared.models import RunStatus
        finished = reg.complete(run.run_id, status=RunStatus.SUCCEEDED, exit_code=0)

        real = mailer.send_report

        def explode(*args, **kwargs):
            raise RuntimeError("smtp is on fire")

        mailer.send_report = explode
        try:
            try:
                resolver().deliver_report(finished, [])
            except RuntimeError:
                pass  # the resolver may propagate; the EXECUTOR is what must not
        finally:
            mailer.send_report = real

        assert reg.get(run.run_id).status is RunStatus.SUCCEEDED, "the run was changed"
        with pool().connection() as conn:
            row = conn.execute("SELECT report_sent_at FROM runs WHERE run_id=%s",
                               (run.run_id,)).fetchone()
        assert row["report_sent_at"] is None, "a failed send was recorded as sent"


@test
def test_an_unconfigured_mailer_is_silent_not_broken():
    """Artifacts are on disk and reachable through the API, so an unsent report
    is a missing convenience rather than a lost result."""
    from delivery.mailer import is_configured, send_report

    assert is_configured({}) is False
    assert send_report("a@b.c", "subject", "body", None, {}) is False
    # ...and a configured-but-unreachable server is the same answer, not a raise.
    assert send_report("a@b.c", "subject", "body", None, {
        "ACTOR_PLATFORM_SMTP_USER": "u", "ACTOR_PLATFORM_SMTP_PASSWORD": "p",
        "ACTOR_PLATFORM_SMTP_HOST": "127.0.0.1", "ACTOR_PLATFORM_SMTP_PORT": "59999",
    }) is False


def _run_with_fake_actor(output: dict, handout: str | None):
    """Drive the REAL executor with the container replaced by a fake that writes
    `output` and (optionally) a refreshed-session handout. -> (run, account)."""
    import worker.executor as ex
    from shared.models import SESSION_OUT_FILE
    from worker.limits import ExecutionLimits

    def fake_execute(command, name, **kwargs):  # noqa: ARG001
        mounts = [command[i + 1] for i, a in enumerate(command) if a == "-v"]
        scratch = next(m.split(":")[0] for m in mounts if m.endswith(ACTOR_SECRETS_DIR))
        run_dir = next(m.split(":")[0] for m in mounts if "/runs/" in m)
        Path(run_dir, "output.json").write_text(json.dumps(output))
        if handout is not None:
            Path(scratch, SESSION_OUT_FILE).write_text(handout)
        return 1, ["[fake] done"]

    tmp = tempfile.mkdtemp()
    reg = PgRunRegistry(Path(tmp) / "runs")
    user, account = make_account(jar(xs="good-xs"))
    saved = ex._execute
    ex._execute = fake_execute
    try:
        run = a_run(reg, user, account)
        ex._run_prepared(run, registry=reg, limits=ExecutionLimits(),
                         resolver=resolver(), profile=FB_PROFILE)
    finally:
        ex._execute = saved
    return run, account


def _account(account):
    with pool().connection() as conn:
        return conn.execute("SELECT status, consecutive_auth_failures, expired_reason "
                            "FROM social_accounts WHERE id=%s", (account,)).fetchone()


@test
def test_an_actor_reported_session_failure_is_recorded_and_not_absorbed():
    """The Actor says its session is dead: the account records it, and the jar
    the run ended with is NOT stored -- absorbing it would keep a dead session
    AND reset the counter that was just incremented."""
    run, account = _run_with_fake_actor(
        {"status": "login_wall", "session_failure": "login_wall"},
        handout=jar(xs="dead-xs"))
    row = _account(account)
    assert row["consecutive_auth_failures"] == 1, dict(row)
    assert row["status"] == "connected", "one soft failure must not expire"
    env = resolver().for_run(run, FB_PROFILE)
    assert "good-xs" in env["FB_SESSION"], "the walled run's jar was stored"


@test
def test_no_session_failure_field_means_no_verdict():
    from worker.executor import session_failure
    for output in ({"status": "success"}, {"session_failure": ""},
                   {"session_failure": 3}, ["not", "a", "dict"]):
        with tempfile.TemporaryDirectory() as tmp:
            reg = PgRunRegistry(Path(tmp) / "runs")
            user, account = make_account(jar())
            run = a_run(reg, user, account)
            Path(run.run_dir, "output.json").write_text(json.dumps(output))
            assert session_failure(reg, run.run_id) is None, output


# --- end to end, through a real container -----------------------------------


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    import subprocess
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def image_available(image: str) -> bool:
    import subprocess
    return subprocess.run(["docker", "image", "inspect", image],
                          capture_output=True).returncode == 0


@test
def test_the_secret_reaches_the_container_and_nothing_else():
    """The whole invariant, end to end, against a real container.

    Six surfaces are checked because the platform's own note says the input
    document alone touches four of them. `-e NAME` with the value supplied
    through the docker CLI's own environment reaches none.
    """
    if not docker_available():
        print("        (skipped: docker is not available)", end="")
        return
    if not image_available("actor-platform/hello_world:latest"):
        print("        (skipped: the hello_world image is not built)", end="")
        return

    from worker.executor import _execute

    marker = "SECRET-" + "K9x2Qp7Lm4Vt8Nw3"

    # 1. It ACTUALLY arrives -- otherwise the rest proves only that we sent
    #    nothing. Deliberately not redacted here, so delivery is observable.
    code, lines = _execute(
        ["docker", "run", "--rm", "-e", "PROBE_SESSION",
         "--entrypoint", "sh", "actor-platform/hello_world:latest",
         "-c", "echo $PROBE_SESSION"],
        "probe", env={**os.environ, "PROBE_SESSION": marker},
    )
    assert code == 0, f"probe container failed: {lines}"
    assert any(marker in line for line in lines), (
        f"the secret did NOT reach the container: {lines}")
    assert "PROBE_SESSION" not in os.environ, "the worker's environment was mutated"

    # 2. ...and it reaches nothing else. A full run through the real path.
    with tempfile.TemporaryDirectory() as tmp:
        reg = PgRunRegistry(Path(tmp) / "runs")
        queue_dir = Path(tmp) / "queue"
        queue = FileJobQueue(str(queue_dir))
        user, account = make_account(jar(xs=marker))
        run = reg.create("hello_world", {"name": "Basel"},
                         owner_id=user, social_account_id=account)

        from jobqueue.base import Job
        queue.enqueue(Job(run_id=run.run_id, actor="hello_world",
                          input={"name": "Basel"}))

        from worker.executor import _run_prepared
        from worker.limits import ExecutionLimits

        profile = ActorProfile(session_cookies=("c_user", "xs"),
                               session_env="PROBE_SESSION", session_out=False)
        finished = _run_prepared(run, registry=reg, limits=ExecutionLimits(),
                                 resolver=resolver(), profile=profile)
        assert finished.status.value == "SUCCEEDED", (finished.status, finished.error)

        surfaces = {
            "run.json": (Path(run.run_dir) / "run.json"),
            "input.json": (Path(run.run_dir) / "input.json"),
            "output.json": (Path(run.run_dir) / "output.json"),
            "log.txt": (Path(run.run_dir) / "log.txt"),
        }
        for label, path in surfaces.items():
            if path.is_file():
                assert marker not in path.read_text(encoding="utf-8", errors="replace"), (
                    f"the session leaked into {label}")

        for path in queue_dir.rglob("*"):
            if path.is_file():
                assert marker not in path.read_text(encoding="utf-8", errors="replace"), (
                    f"the session leaked into the queue file {path.name}")

        # ...and not into the database row for the RUN either.
        with pool().connection() as conn:
            row = conn.execute("SELECT input::text, error FROM runs WHERE run_id=%s",
                               (run.run_id,)).fetchone()
        assert marker not in (row["input"] or ""), "the session leaked into runs.input"
        assert marker not in (row["error"] or ""), "the session leaked into runs.error"


def main() -> int:
    print(f"running {len(TESTS)} per-run credential tests\n")
    failed = 0
    for fn in TESTS:
        name = fn.__name__.removeprefix("test_")
        try:
            fn()
            print(f"  ok    {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
            print("        " + traceback.format_exc().strip().replace("\n", "\n        ")[:1200])
    print(f"\n{'all ' + str(len(TESTS)) + ' passed' if not failed else f'{failed} FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
