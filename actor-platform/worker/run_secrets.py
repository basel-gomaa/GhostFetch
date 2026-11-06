"""Which credential a run gets, and where it comes from.

NOT NAMED `secrets.py`, AND THAT IS LOAD-BEARING. The Worker is started as
`python worker/consumer.py`, which puts `worker/` at sys.path[0] -- so a module
here called `secrets` SHADOWS the standard library's, for the whole process.
shared/crypto.py calls secrets.token_bytes() to mint the AES-GCM nonce, so the
shadow made every session write-back fail with "module 'secrets' has no
attribute 'token_bytes'". The scrape still succeeded and the run still reported
SUCCEEDED; only the refreshed cookie jar was silently dropped -- i.e. it
resurrected the exact session-decay bug this milestone set out to fix, in a
form that logs one line and breaks nothing visible for weeks.

The platform's existing answer to secrets is ACTOR_FORWARD_ENV: name the
variables, and the Worker hands the same values to every Actor container. That
is right for a single-user deployment and WRONG the moment there are thirty
users, because it means every run scrapes as whoever's session the Worker
happens to hold.

This resolves a credential PER RUN, from the run's own social account.

    resolver.for_run(run, profile)  -> {"FB_SESSION": "<the user's session>"}

THE VALUE STILL NEVER ENTERS ARGV. The executor keeps emitting `-e NAME` with no
`=value` and now supplies the value through subprocess.Popen(env=...), which is
a per-child dict applied at fork/exec. That is the whole trick, and it is why
the platform's stated invariant survives becoming per-run: the secret reaches
the container and nothing else.

DO NOT "SIMPLIFY" THIS INTO os.environ[name] = value. The Worker is threaded
three ways -- the consumer dispatches each job on its own thread, and the
executor adds a reader thread and a cancel watcher per run -- so mutating the
process environment would race, and the failure mode is USER A'S SESSION GOING
INTO USER B'S CONTAINER. Popen(env=) touches nothing shared.

THE WORKER DECRYPTS, NOT THE API. The API must never put a plaintext session in
the Job: Job payloads are written to storage/queue/pending/*.json, embedded in
run.json, and written to input.json -- three durable surfaces at once. The
Worker is already the most trusted component (it holds the value in memory
regardless), so it reads the ciphertext from the database and opens it itself.
"""

from __future__ import annotations

import logging
from typing import Mapping, Protocol

from shared.models import Run

logger = logging.getLogger(__name__)


class SessionUnavailable(RuntimeError):
    """This run cannot be given the credential it needs.

    A PLATFORM failure, not an Actor failure: the request was fine and the
    Actor never ran. It must not be reported to the user as "your session
    expired, log in again", because the session may be perfectly good.
    """


class SessionBusy(RuntimeError):
    """Another run already holds this session's lease.

    An ordinary outcome under concurrency, not a failure of anything: the job
    goes back to the queue UNRUN. It must never be acked -- ack means finished,
    and the run would be silently discarded.
    """


class SecretResolver(Protocol):
    """What the executor needs, so it never imports a database.

    Named for its first job, but it is really the platform's account-aware
    component: injection, write-back, expiry AND the session lease all belong to
    whoever knows the relationship between a run and a connected account.
    Keeping them together is what lets the executor stay ignorant of all of it.
    """

    def for_run(self, run: Run, profile) -> Mapping[str, str]:
        """The environment this run's container should receive."""

    def acquire_session(self, run: Run) -> bool:
        """Claim exclusive use of this run's session. False if already held."""

    def release_session(self, run: Run) -> None:
        """Give it back. Always called from a finally."""

    def deliver_report(self, run: Run, artifacts: list) -> bool:
        """E-mail this run's report to whoever submitted it."""

    def report_auth_failure(self, run: Run, reason: str) -> None:
        """The Actor said its session is dead. Record that against the account."""

    def absorb_refreshed(self, run: Run, storage_state: str) -> bool:
        """Store a session the Actor refreshed mid-run. Returns whether it was kept."""


class NullSecretResolver:
    """No per-run credentials. The default, and byte-identical to the old path.

    A deployment with no database, and every pre-existing test, gets exactly the
    behaviour that existed before this module: nothing extra is injected, and
    ACTOR_FORWARD_ENV keeps working as it always did.
    """

    def for_run(self, run: Run, profile) -> Mapping[str, str]:  # noqa: ARG002
        return {}

    def report_auth_failure(self, run: Run, reason: str) -> None:
        pass

    def absorb_refreshed(self, run: Run, storage_state: str) -> bool:  # noqa: ARG002
        return False

    def acquire_session(self, run: Run) -> bool:  # noqa: ARG002
        return True  # nothing to serialise on

    def release_session(self, run: Run) -> None:
        pass

    def deliver_report(self, run: Run, artifacts: list) -> bool:  # noqa: ARG002
        return False  # nobody to deliver to without accounts


# The largest single environment variable Linux will accept, MAX_ARG_STRLEN --
# 32 pages, and INDEPENDENT of the much larger ARG_MAX that bounds the whole
# environment. A session bigger than this cannot be handed to a container at
# all: exec fails with E2BIG, which surfaces as
# "OSError: [Errno 7] Argument list too long: 'docker'" and reads like a bug in
# the platform rather than a session that outgrew a kernel limit.
#
# MEASURED, on this machine: 131,000 bytes was forwarded fine and 197,696 was
# refused. A real stored session reached 197 KB -- 89 cookies and 176 KB of
# localStorage the site had accumulated -- and every run for that account then
# failed before its container started.
MAX_ENV_VALUE = 131_072
# Leave room for the name, the "=" and the rest of the environment.
SESSION_ENV_BUDGET = MAX_ENV_VALUE - 4096


def _fit_session(session: str, run_id: str, name: str) -> str:
    """Make a session small enough to be forwarded. -> the document to send.

    DROPS `origins` (localStorage / sessionStorage) AND ONLY WHEN IT MUST,
    so every session under the limit is passed through byte-for-byte and
    nothing that works today changes.

    Dropping that half rather than the cookies is not a coin toss. The platform
    decides a session is live by looking at COOKIES -- `session_cookies` in
    config/actors.json names them, and shared.social_session.is_logged_in reads
    nothing else -- so cookies are the part that cannot be lost. And an Actor
    that genuinely needs local storage is an Actor driving a real browser, which
    is exactly the case `persistent_state` exists for: that mount carries the
    site's local storage and databases across runs already, so for those Actors
    this is redundant rather than lost.
    """
    if len(session) <= SESSION_ENV_BUDGET:
        return session

    import json  # noqa: PLC0415

    try:
        document = json.loads(session)
    except ValueError:
        logger.warning("run %s: %s is %d bytes and not JSON, so it cannot be "
                       "trimmed - the container will refuse to start",
                       run_id, name, len(session))
        return session

    origins = document.pop("origins", None) or []
    trimmed = json.dumps(document)
    logger.warning(
        "run %s: %s was %d bytes, over the %d-byte limit for one environment "
        "variable - dropped %d stored origin(s) to fit, leaving %d bytes. The "
        "cookies, which are what prove the session, are untouched.",
        run_id, name, len(session), SESSION_ENV_BUDGET, len(origins), len(trimmed),
    )
    if len(trimmed) > SESSION_ENV_BUDGET:
        logger.warning("run %s: %s is STILL %d bytes after trimming; the run "
                       "will fail to start", run_id, name, len(trimmed))
    return trimmed


class DbSecretResolver:
    """Per-run credentials, decrypted from the run's own social account."""

    def __init__(self, pool, keyring, required_cookies_for=None) -> None:
        self._pool = pool
        self._keyring = keyring
        # "Which cookies prove a session for this Actor" is CONFIGURATION, and
        # it arrives as a callable so this module names no Actor and reads no
        # config file of its own. Defaults to a rule nothing can satisfy, so a
        # missing wiring refuses to store sessions rather than storing bad ones.
        self._required_cookies_for = required_cookies_for or (lambda _actor: ())

    # --- injection ----------------------------------------------------------

    def for_run(self, run: Run, profile) -> Mapping[str, str]:
        name = getattr(profile, "session_env", None)
        if not name:
            return {}  # this Actor needs no session

        if not run.social_account_id:
            # An OPTIONAL session with no account attached is an anonymous run,
            # not a missing credential. The API attaches the user's account when
            # one is connected and lets the run through without one otherwise
            # (api/main.py: `if profile.session_optional`), so refusing it here
            # turned every such run into a PLATFORM_ERROR in 0.0 s - the API
            # accepted it and the worker rejected it before the container was
            # built. The two layers must answer this question identically.
            if getattr(profile, "session_optional", False):
                logger.info("run %s: no account attached and %s is optional - "
                            "running without a session", run.run_id, name)
                return {}
            raise SessionUnavailable(
                f"actor '{run.actor}' needs a {name} session but run {run.run_id} "
                f"names no social account"
            )

        from shared.crypto import DecryptionFailed, session_aad  # noqa: PLC0415

        with self._pool().connection() as conn:
            row = conn.execute(
                "SELECT platform, session_ciphertext, session_nonce, key_version, status "
                "FROM social_accounts WHERE id = %s",
                (run.social_account_id,),
            ).fetchone()

        if row is None:
            raise SessionUnavailable(
                f"run {run.run_id} names social account {run.social_account_id}, "
                f"which no longer exists"
            )
        if row["status"] == "revoked":
            raise SessionUnavailable(
                f"the connected account for run {run.run_id} has been disconnected"
            )

        try:
            session = self._keyring.open_(
                bytes(row["session_ciphertext"]),
                bytes(row["session_nonce"]),
                session_aad(str(run.social_account_id), row["platform"]),
                row["key_version"],
            )
        except DecryptionFailed as exc:
            # OUR failure, not the user's. Telling them to log in again would
            # be wrong and would cost them a real login attempt.
            raise SessionUnavailable(
                f"the stored session for run {run.run_id} could not be decrypted: {exc}"
            ) from exc

        # Deliberately counts cookies and says nothing else -- see
        # shared/social_session.describe for why a session must never be logged.
        session = _fit_session(session, run.run_id, name)
        logger.info("run %s: injecting %s (%d bytes)", run.run_id, name, len(session))
        return {name: session}

    # --- write-back ---------------------------------------------------------

    def absorb_refreshed(self, run: Run, storage_state: str) -> bool:
        """Re-encrypt a session the Actor refreshed, replacing the stored one.

        REFUSES A LOGGED-OUT JAR, which is the entire point of the guard. A run
        that ended on a login wall holds a jar with the identity cookie and no
        authentication cookie; writing that back would turn one bounced run into
        "log in again" for a session that was fine.
        """
        from shared.crypto import session_aad  # noqa: PLC0415
        from shared.social_session import (  # noqa: PLC0415
            account_id,
            cookies_of,
            is_logged_in,
        )

        if not run.social_account_id:
            return False

        with self._pool().connection() as conn:
            row = conn.execute(
                "SELECT platform FROM social_accounts WHERE id = %s",
                (run.social_account_id,),
            ).fetchone()
        if row is None:
            return False

        required = self._required_cookies_for(run.actor)
        cookies = cookies_of(storage_state)
        if not is_logged_in(cookies, required):
            logger.info(
                "run %s: not storing the refreshed session (%d cookie(s), not a login)",
                run.run_id, len(cookies),
            )
            return False

        ciphertext, nonce, version = self._keyring.seal(
            storage_state, session_aad(str(run.social_account_id), row["platform"])
        )
        with self._pool().connection() as conn:
            conn.execute(
                "UPDATE social_accounts SET session_ciphertext = %s, session_nonce = %s, "
                "key_version = %s, status = 'connected', last_verified_at = now(), "
                "expired_at = NULL, expired_reason = NULL, consecutive_auth_failures = 0 "
                "WHERE id = %s",
                (ciphertext, nonce, version, run.social_account_id),
            )
        # A successful run IS a positive session check, so it clears the failure
        # counter and refreshes last_verified_at above.
        logger.info("run %s: stored the refreshed session", run.run_id)
        return True

    # --- the session lease --------------------------------------------------

    def acquire_session(self, run: Run) -> bool:
        """One run per connected account, ACROSS PROCESSES.

        The consumer's in-process counter is the fast path and is what stops a
        busy session's job from being leased at all. This is the correctness
        backstop, and it has to exist because that counter lives in ONE process:
        two Workers would otherwise both run a scrape on the same session, race
        its server-side rotation, and make the account look automated. The
        Actor-level cap this replaces claimed to prevent that and did not.

        The PRIMARY KEY is the whole mechanism -- a second INSERT conflicts, so
        the database decides, not a check-then-act in application code.

        Reaps a lease whose owner is provably dead first, using the same
        <host>:<pid> convention and the same owner_is_dead() the queue's leases
        use, so a killed Worker does not strand an account for ever.
        """
        if not run.social_account_id:
            return True

        from jobqueue.base import owner_id, owner_is_dead  # noqa: PLC0415

        me = owner_id()
        with self._pool().connection() as conn:
            held = conn.execute(
                "SELECT run_id, owner FROM session_leases WHERE social_account_id = %s",
                (run.social_account_id,),
            ).fetchone()

            if held is not None:
                if held["run_id"] == run.run_id:
                    return True  # our own lease, redelivered
                if not owner_is_dead(held["owner"]):
                    return False
                logger.info(
                    "reclaiming the session lease held by dead worker %s (run %s)",
                    held["owner"], held["run_id"],
                )
                conn.execute(
                    "DELETE FROM session_leases WHERE social_account_id = %s AND owner = %s",
                    (run.social_account_id, held["owner"]),
                )

            row = conn.execute(
                "INSERT INTO session_leases (social_account_id, run_id, owner) "
                "VALUES (%s,%s,%s) ON CONFLICT (social_account_id) DO NOTHING "
                "RETURNING social_account_id",
                (run.social_account_id, run.run_id, me),
            ).fetchone()
        return row is not None

    def release_session(self, run: Run) -> None:
        if not run.social_account_id:
            return
        try:
            with self._pool().connection() as conn:
                # Scoped to THIS run, so a lease another run has since taken --
                # after ours was reaped as dead, say -- is never released by us.
                conn.execute(
                    "DELETE FROM session_leases WHERE social_account_id = %s AND run_id = %s",
                    (run.social_account_id, run.run_id),
                )
        except Exception as exc:  # noqa: BLE001
            # Never fatal: a stranded lease is reclaimed by the dead-owner check
            # above, whereas raising here would mask the run's real outcome.
            logger.warning("run %s: could not release the session lease: %s", run.run_id, exc)

    # --- delivery -----------------------------------------------------------

    def deliver_report(self, run, artifacts: list) -> bool:
        """E-mail this run's report to whoever submitted it. Never raises.

        Sent from the WORKER because the recipient must not travel in
        input.json (which is served over HTTP) and SMTP credentials must not
        reach a container running vendored third-party code.

        Recorded with report_sent_at so a second delivery cannot happen if the
        queue redelivers a finished job -- at-least-once applies to jobs, and
        nobody wants the same report twice.
        """
        from delivery.mailer import send_report  # noqa: PLC0415

        with self._pool().connection() as conn:
            row = conn.execute(
                "SELECT r.report_email, r.report_sent_at, r.actor, r.status, "
                "       u.email AS owner_email "
                "FROM runs r LEFT JOIN users u ON u.id = r.owner_id WHERE r.run_id = %s",
                (run.run_id,),
            ).fetchone()
        if row is None or row["report_sent_at"] is not None:
            return False

        recipient = row["report_email"] or row["owner_email"]
        if not recipient:
            return False

        subject = f"{row['actor']} report - {run.run_id}"
        body = (
            f"Your {row['actor']} run has finished.\n\n"
            f"  run     {run.run_id}\n"
            f"  status  {run.status.value}\n"
            f"  took    {run.duration_seconds}s\n\n"
            + ("The report is attached.\n" if artifacts else
               "This run produced no report.\n")
        )
        if not send_report(recipient, subject, body, artifacts):
            return False

        with self._pool().connection() as conn:
            conn.execute("UPDATE runs SET report_sent_at = now() WHERE run_id = %s",
                         (run.run_id,))
        return True

    # --- expiry -------------------------------------------------------------

    def report_auth_failure(self, run: Run, reason: str) -> None:
        """Record that this run found the session dead.

        Debounced, because a false positive here tells a user to re-do an
        interactive login they did not need to. Only the CONFIDENT reason flips
        an account on one occurrence; anything else needs two in a row.
        """
        if not run.social_account_id:
            return
        confident = reason == "expired"
        with self._pool().connection() as conn:
            conn.execute(
                "UPDATE social_accounts SET "
                "  consecutive_auth_failures = consecutive_auth_failures + 1, "
                "  status = CASE WHEN %s OR consecutive_auth_failures + 1 >= 2 "
                "                THEN 'expired' ELSE status END, "
                "  expired_at = CASE WHEN %s OR consecutive_auth_failures + 1 >= 2 "
                "                    THEN now() ELSE expired_at END, "
                "  expired_reason = %s "
                "WHERE id = %s",
                (confident, confident, reason, run.social_account_id),
            )
        logger.info("run %s: auth failure recorded (%s)", run.run_id, reason)


def resolver_from_env(config) -> SecretResolver:
    """The resolver this deployment is configured for.

    Falls back to NullSecretResolver whenever there is no database or no
    encryption key, so the single-user path needs no configuration and cannot
    accidentally acquire per-run credentials it was not set up for.
    """
    import os  # noqa: PLC0415

    if not (os.environ.get("ACTOR_PLATFORM_DATABASE_URL") or "").strip():
        return NullSecretResolver()
    try:
        from db.pool import pool  # noqa: PLC0415
        from shared.crypto import KeyRing  # noqa: PLC0415

        return DbSecretResolver(
            pool,
            KeyRing.from_env(),
            lambda actor: config.limits_for(actor).session_cookies,
        )
    except Exception as exc:  # noqa: BLE001
        # A missing key is a configuration mistake, not a reason to run with
        # nobody's credentials -- but it must be VISIBLE, not silent.
        logger.warning("per-run credentials are OFF: %s", exc)
        return NullSecretResolver()
