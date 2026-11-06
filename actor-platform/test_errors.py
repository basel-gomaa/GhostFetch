#!/usr/bin/env python3
"""Error-handling tests: what kind of failure, and what a caller does about it.

The platform could already say WHETHER a run failed. These cover the half it
could not say: which category the failure belongs to, whether retrying could
possibly help, whether data survived, and -- the one with teeth -- that no
credential reaches a stored error or an API response.

No Docker. Every test builds its own registry, so real storage is untouched.

    python3 test_errors.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
VENV = REPO_ROOT / ".venv"
VENV_PYTHON = VENV / "bin" / "python"

if (
    VENV_PYTHON.is_file()
    and Path(sys.prefix).resolve() != VENV.resolve()
    and not os.environ.get("_ACTOR_PLATFORM_REEXEC")
):
    os.environ["_ACTOR_PLATFORM_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

sys.path.insert(0, str(REPO_ROOT))

from registry.run_registry import RunRegistry  # noqa: E402
from shared.errors import (  # noqa: E402
    CONTAINER_EXITED,
    CONTAINER_KILLED,
    CONTAINER_OOM,
    CONTAINER_START_FAILED,
    IMAGE_NOT_FOUND,
    ErrorCategory,
    RunError,
    category_for_actor_status,
    classify_container_exit,
    parse_retry_after,
    redact,
    retryable_by_default,
)
from shared.models import OUTPUT_FILE, RunStatus  # noqa: E402
from worker.executor import _classify  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


@contextmanager
def registry():
    with tempfile.TemporaryDirectory() as tmp:
        yield RunRegistry(Path(tmp) / "runs")


def started(reg, actor="hello_world", payload=None):
    """A run that has reached RUNNING, as the worker leaves it before a verdict."""
    run = reg.create(actor, payload if payload is not None else {})
    reg.mark_running(run.run_id)
    return reg.get(run.run_id)


def write_output(reg, run_id, document):
    path = Path(reg.run_dir(run_id)) / OUTPUT_FILE
    path.write_text(json.dumps(document), encoding="utf-8")


# --- the category set -------------------------------------------------------


@test
def test_only_the_upstream_can_make_a_failure_retryable():
    """Retrying is for what the upstream itself said would pass, and nothing else.

    The default matters more than it looks: a category that is retryable by
    accident turns one bad URL into three identical failures and three times
    the load on a site that already refused us.
    """
    retryable = {c for c in ErrorCategory if retryable_by_default(c)}
    assert retryable == {ErrorCategory.RATE_LIMITED, ErrorCategory.TRANSIENT_ERROR}, retryable

    for category in (ErrorCategory.INPUT_ERROR, ErrorCategory.AUTH_ERROR,
                     ErrorCategory.ACCESS_ERROR, ErrorCategory.ACTOR_ERROR,
                     ErrorCategory.UNKNOWN_ERROR, ErrorCategory.TIMEOUT):
        assert not retryable_by_default(category), f"{category} must not retry by default"


@test
def test_an_unclassifiable_failure_is_never_a_success():
    """UNKNOWN_ERROR exists so a failure nobody understood is still a failure."""
    error = RunError(ErrorCategory.UNKNOWN_ERROR, "something went wrong")
    assert error.retryable is False
    assert error.to_dict()["code"] == "UNKNOWN_ERROR"


@test
def test_every_category_survives_a_round_trip():
    for category in ErrorCategory:
        original = RunError(category, "m", stage="s", actor="a", attempt=2, detail="D")
        restored = RunError.from_dict(original.to_dict())
        assert restored == original, f"{category} did not round-trip: {restored}"


@test
def test_an_unreadable_error_field_does_not_become_a_fake_category():
    assert RunError.from_dict(None) is None
    assert RunError.from_dict({}) is None
    assert RunError.from_dict("nonsense") is None
    # A code from a NEWER version must degrade to UNKNOWN, not explode.
    assert RunError.from_dict({"code": "FROM_THE_FUTURE"}).code is ErrorCategory.UNKNOWN_ERROR


# --- container failures -----------------------------------------------------


@test
def test_docker_failures_are_not_reported_as_actor_failures():
    """The Actor never ran, so calling these a scraping failure is a lie.

    This is the case `exit_code != 0` got wrong: a missing image and a bad
    selector both exit non-zero, and only one of them is the Actor's fault.
    """
    category, detail, _ = classify_container_exit(125, "docker: Error response from daemon")
    assert (category, detail) == (ErrorCategory.PLATFORM_ERROR, CONTAINER_START_FAILED)

    category, detail, _ = classify_container_exit(1, "Unable to find image 'actor-x:latest' locally")
    assert (category, detail) == (ErrorCategory.PLATFORM_ERROR, IMAGE_NOT_FOUND)

    for code in (126, 127):
        category, _, _ = classify_container_exit(code)
        assert category is ErrorCategory.PLATFORM_ERROR, f"exit {code} is not the actor's fault"


@test
def test_an_unreachable_daemon_is_the_one_retryable_container_failure():
    category, detail, _ = classify_container_exit(1, "Cannot connect to the Docker daemon at unix://")
    assert category is ErrorCategory.TRANSIENT_ERROR
    assert detail == CONTAINER_START_FAILED
    assert RunError(category, "x").retryable is True


@test
def test_an_oom_kill_is_only_claimed_when_docker_said_so():
    """137 is SIGKILL, and the platform kills containers itself.

    Reporting every 137 as an OOM would tell an operator to raise a memory
    limit after every cancellation. `oom_killed=None` means "not checked", and
    the honest answer then is that it was killed, not why.
    """
    category, detail, _ = classify_container_exit(137, oom_killed=True)
    assert (category, detail) == (ErrorCategory.PLATFORM_ERROR, CONTAINER_OOM)

    category, detail, _ = classify_container_exit(137)
    assert detail == CONTAINER_KILLED, "an unverified 137 must not be called an OOM"

    category, detail, _ = classify_container_exit(137, oom_killed=False)
    assert detail == CONTAINER_KILLED


@test
def test_a_plain_non_zero_exit_is_the_actors_own():
    category, detail, message = classify_container_exit(1)
    assert (category, detail) == (ErrorCategory.ACTOR_ERROR, CONTAINER_EXITED)
    assert "exited with code 1" in message


@test
def test_a_container_with_no_exit_status_is_a_platform_failure():
    category, detail, _ = classify_container_exit(None)
    assert (category, detail) == (ErrorCategory.PLATFORM_ERROR, CONTAINER_START_FAILED)


# --- the actor's own verdict ------------------------------------------------


@test
def test_an_actors_status_word_becomes_a_category_without_naming_the_actor():
    """The map is on generic words, so a new Actor is classified on day one."""
    assert category_for_actor_status("login_wall") == (ErrorCategory.ACCESS_ERROR, "LOGIN_WALL")
    assert category_for_actor_status("authwall")[0] is ErrorCategory.ACCESS_ERROR
    assert category_for_actor_status("rate_limited")[0] is ErrorCategory.RATE_LIMITED
    assert category_for_actor_status("private_profile")[0] is ErrorCategory.ACCESS_ERROR
    assert category_for_actor_status("transport_error")[0] is ErrorCategory.TRANSIENT_ERROR
    # A robots.txt refusal is a policy, so it is access-denied and never retried.
    robots = category_for_actor_status("robots_disallowed")
    assert robots == (ErrorCategory.ACCESS_ERROR, "ROBOTS_DISALLOWED"), robots
    assert RunError(robots[0], "refused").retryable is False
    # Case and padding are the Actor's business, not the mapping's.
    assert category_for_actor_status("  LOGIN_WALL  ")[0] is ErrorCategory.ACCESS_ERROR


@test
def test_an_empty_result_is_not_an_error():
    """The regression that an error taxonomy invites.

    A scrape that ran correctly and found nothing is a SUCCESSFUL run with an
    empty result. Mapping these would relabel every legitimately empty answer
    as a failure -- and a caller could no longer tell an empty hashtag from a
    private profile, which is the distinction that actually matters.
    """
    for word in ("no_posts_found", "no_comments", "success", "ok", ""):
        assert category_for_actor_status(word) is None, f"{word!r} must not be an error"
    assert category_for_actor_status(None) is None


@test
def test_a_login_wall_is_access_denied_not_a_generic_actor_failure():
    with registry() as reg:
        run = started(reg, "facebook")
        write_output(reg, run.run_id, {"status": "login_wall", "error": "hit the login wall",
                                       "posts": [], "post_count": 0})

        status, message, info, partial = _classify(
            reg, run.run_id, exit_code=1, actor="facebook", attempt=1
        )
        assert status is RunStatus.FAILED
        assert info.code is ErrorCategory.ACCESS_ERROR
        assert info.detail == "LOGIN_WALL"
        assert info.retryable is False, "retrying a login wall just hits it again"
        assert info.stage == "actor" and info.actor == "facebook" and info.attempt == 1
        assert message == "hit the login wall"


@test
def test_a_dead_session_is_an_auth_error_even_when_the_status_was_vague():
    with registry() as reg:
        run = started(reg, "instagram")
        write_output(reg, run.run_id, {"status": "error", "session_failure": "expired"})

        _, _, info, _ = _classify(reg, run.run_id, exit_code=1, actor="instagram")
        assert info.code is ErrorCategory.AUTH_ERROR, info
        assert info.retryable is False


@test
def test_a_container_failure_outranks_whatever_the_actor_left_behind():
    """The Actor never ran, so a stale output document must not be believed."""
    with registry() as reg:
        run = started(reg, "x")
        write_output(reg, run.run_id, {"status": "login_wall"})

        status, _, info, _ = _classify(
            reg, run.run_id, exit_code=1, log_tail="Unable to find image 'actor-x:latest' locally"
        )
        assert status is RunStatus.PLATFORM_ERROR
        assert info.detail == IMAGE_NOT_FOUND
        assert info.stage == "container"


# --- Retry-After ------------------------------------------------------------


@test
def test_both_rfc9110_retry_after_forms_are_read():
    """The date form is the one a delta-seconds-only parser silently ignores."""
    from email.utils import parsedate_to_datetime

    assert parse_retry_after("120") == 120.0
    assert parse_retry_after(" 45 ") == 45.0

    when = "Wed, 21 Oct 2026 07:29:00 GMT"
    base = parsedate_to_datetime(when).timestamp()
    assert parse_retry_after(when, now=base - 60) == 60.0
    assert parse_retry_after(when, now=base - 3600) == 3600.0
    # RFC 9110 still requires recipients to accept the obsolete formats.
    assert parse_retry_after("Wednesday, 21-Oct-26 07:29:00 GMT", now=base - 120) == 120.0
    # No zone is read as UTC; reading it as local time would be wrong by the
    # host's offset, which is a bug that only shows up outside UTC.
    assert parse_retry_after("Wed, 21 Oct 2026 07:29:00", now=base - 30) == 30.0


@test
def test_a_retry_after_in_the_past_is_zero_not_negative():
    """A date already gone means "you may retry now", which is not a negative wait."""
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
    assert parse_retry_after(-5) == 0.0


@test
def test_an_absurd_retry_after_is_clamped_rather_than_obeyed():
    """A misconfigured proxy must not be able to park a run for a week."""
    assert parse_retry_after(999_999_999) == 86400.0
    assert parse_retry_after("999999999") == 86400.0


@test
def test_a_retry_after_that_says_nothing_is_none():
    for value in (None, "", "   ", "soon", "later please", [], {}, True, False,
                  float("nan"), float("inf")):
        assert parse_retry_after(value) is None, f"{value!r} should say nothing"


@test
def test_the_actors_retry_after_reaches_the_run_error():
    """End to end: output document -> classification -> stored error."""
    with registry() as reg:
        run = started(reg, "x")
        write_output(reg, run.run_id, {"status": "rate_limited", "error": "slow down",
                                       "retry_after_seconds": 90})

        _, _, info, _ = _classify(reg, run.run_id, exit_code=1, actor="x")
        assert info.code is ErrorCategory.RATE_LIMITED
        assert info.retryable is True
        assert info.retry_after_seconds == 90.0
        assert info.to_dict()["retry_after_seconds"] == 90.0


@test
def test_a_raw_retry_after_header_works_as_well_as_a_number():
    """An Actor may pass the header through instead of doing the arithmetic."""
    with registry() as reg:
        run = started(reg, "threads")
        write_output(reg, run.run_id, {"status": "rate_limited", "retry_after": "300"})

        _, _, info, _ = _classify(reg, run.run_id, exit_code=1)
        assert info.retry_after_seconds == 300.0


@test
def test_retry_after_is_read_from_the_reliability_block_too():
    """Where the most instrumented Actor already publishes code and retryability."""
    with registry() as reg:
        run = started(reg, "tiktok")
        write_output(reg, run.run_id, {
            "status": "rate_limited",
            "reliability": {"error_code": "RATE_LIMITED", "retry_after_seconds": 30},
        })

        _, _, info, _ = _classify(reg, run.run_id, exit_code=1)
        assert info.retry_after_seconds == 30.0


@test
def test_the_platform_never_invents_a_delay():
    """A rate limit with no stated delay carries none, rather than a guess.

    A guessed number in the same field as a real Retry-After is
    indistinguishable from one, and a caller acting on it would be obeying us
    rather than the site.
    """
    with registry() as reg:
        run = started(reg)
        write_output(reg, run.run_id, {"status": "rate_limited"})

        _, _, info, _ = _classify(reg, run.run_id, exit_code=1)
        assert info.code is ErrorCategory.RATE_LIMITED
        assert info.retry_after_seconds is None
        assert "retry_after_seconds" not in info.to_dict()


# --- success, and the shape of failure --------------------------------------


@test
def test_a_successful_run_carries_no_error_at_all():
    with registry() as reg:
        run = started(reg)
        write_output(reg, run.run_id, {"status": "success", "posts": []})

        status, message, info, partial = _classify(reg, run.run_id, exit_code=0)
        assert status is RunStatus.SUCCEEDED
        assert (message, info, partial) == (None, None, False)


@test
def test_exit_zero_with_no_output_is_still_a_failed_run():
    """Unchanged behaviour, now with a reason attached."""
    with registry() as reg:
        run = started(reg)
        status, _, info, _ = _classify(reg, run.run_id, exit_code=0)
        assert status is RunStatus.FAILED
        assert info.code is ErrorCategory.ACTOR_ERROR
        assert info.detail == "MALFORMED_OUTPUT"


@test
def test_malformed_output_json_is_an_actor_error_not_a_crash():
    with registry() as reg:
        run = started(reg)
        (Path(reg.run_dir(run.run_id)) / OUTPUT_FILE).write_text("{not json", encoding="utf-8")

        status, _, info, _ = _classify(reg, run.run_id, exit_code=0)
        assert status is RunStatus.FAILED
        assert info.code is ErrorCategory.ACTOR_ERROR


# --- partial results --------------------------------------------------------


@test
def test_a_failed_run_that_produced_items_is_marked_partial():
    """640 of 1000 posts is a failure worth reading, not a failure to discard."""
    with registry() as reg:
        run = started(reg)
        dataset = Path(reg.run_dir(run.run_id)) / "dataset.jsonl"
        dataset.write_text(
            "".join(json.dumps({"collection": "posts", "data": {"i": i}}) + "\n"
                    for i in range(640)),
            encoding="utf-8",
        )
        write_output(reg, run.run_id, {"status": "transport_error"})

        status, _, info, partial = _classify(reg, run.run_id, exit_code=1)
        assert status is RunStatus.FAILED
        assert partial is True, "the collected items must not be written off"
        assert info.code is ErrorCategory.TRANSIENT_ERROR


@test
def test_a_successful_run_that_streamed_items_is_not_partial():
    """`partial` means failed-but-left-data. On 30 October two SUCCEEDED runs
    (a public group and a TikTok profile) were badged PARTIAL in the console,
    with "This run did not finish", only because they stream their items as
    they go."""
    with registry() as reg:
        run = started(reg)
        dataset = Path(reg.run_dir(run.run_id)) / "dataset.jsonl"
        dataset.write_text(
            "".join(json.dumps({"collection": "posts", "data": {"i": i}}) + "\n"
                    for i in range(155)),
            encoding="utf-8",
        )
        write_output(reg, run.run_id, {"status": "partial_success", "posts": []})

        status, message, info, partial = _classify(reg, run.run_id, exit_code=0)
        assert status is RunStatus.SUCCEEDED
        assert (message, info, partial) == (None, None, False)


@test
def test_a_run_that_produced_nothing_is_not_called_partial():
    with registry() as reg:
        run = started(reg)
        write_output(reg, run.run_id, {"status": "login_wall"})
        _, _, _, partial = _classify(reg, run.run_id, exit_code=1)
        assert partial is False


@test
def test_the_record_keeps_the_structured_error_and_the_sentence():
    with registry() as reg:
        run = started(reg)
        error = RunError(ErrorCategory.RATE_LIMITED, "slow down", retry_after_seconds=60,
                         stage="collect_posts", actor="x", attempt=2)
        reg.complete(run.run_id, status=RunStatus.FAILED, exit_code=1,
                     error=error.message, error_info=error.to_dict(), partial=True)

        stored = reg.get(run.run_id)
        assert stored.error == "slow down", "the human sentence is unchanged"
        assert stored.error_info["code"] == "RATE_LIMITED"
        assert stored.error_info["retryable"] is True
        assert stored.error_info["retry_after_seconds"] == 60
        assert stored.partial is True


@test
def test_completing_a_run_the_old_way_still_works():
    """Backward compatibility: every caller that predates error_info.

    The CLI, the reconciler and any older worker all call complete() with just
    a status and a sentence. That must keep working and must not write nulls
    over a record.
    """
    with registry() as reg:
        run = started(reg)
        reg.complete(run.run_id, status=RunStatus.FAILED, exit_code=1, error="plain text")
        stored = reg.get(run.run_id)
        assert stored.error == "plain text"
        assert stored.error_info is None
        assert stored.partial is False


@test
def test_a_record_written_before_this_existed_still_loads():
    from shared.models import Run

    legacy = {"run_id": "20260101-000000000000-abcdef", "actor": "hello_world",
              "status": "FAILED", "created_at": "t", "error": "boom"}
    run = Run.from_dict(legacy)
    assert run.error == "boom" and run.error_info is None and run.partial is False


# --- secrets ----------------------------------------------------------------


@test
def test_a_session_cookie_never_reaches_a_stored_error():
    """The test with teeth. Over-redaction is fine; a leaked jar is an incident."""
    leaky = (
        "POST https://www.facebook.com/api/graphql?access_token=EAAB1234567890abcdef failed; "
        "Cookie: c_user=100012345678; xs=29%3Aabcdefghijklmnop%3A2%3A1700000000; "
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payloadpayload"
    )
    cleaned = redact(leaky)
    for secret in ("EAAB1234567890abcdef", "100012345678", "29%3Aabcdefghijklmnop",
                   "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"):
        assert secret not in cleaned, f"{secret!r} survived redaction: {cleaned}"
    assert "facebook.com/api/graphql" in cleaned, "the useful half must survive"


@test
def test_redaction_happens_on_construction_so_no_path_can_skip_it():
    error = RunError(ErrorCategory.AUTH_ERROR, "failed with api_key=sk-livesecretvalue123")
    assert "sk-livesecretvalue123" not in error.message
    assert "[REDACTED]" in error.message
    assert "sk-livesecretvalue123" not in json.dumps(error.to_dict())


@test
def test_the_redactor_never_raises_on_the_failure_path():
    class Hostile:
        def __str__(self):
            raise RuntimeError("nope")

    assert redact(None) == ""
    assert redact(Hostile()) == "<unprintable>"
    assert isinstance(redact(12345), str)


@test
def test_a_huge_message_is_truncated_rather_than_stored_whole():
    """Docker logs belong in the run log, not in a field a listing renders.

    The invariant is the BOUND, not the ellipsis: a 50,000-character run of one
    character is redacted to "[REDACTED]" as a credential-shaped blob before it
    is ever long enough to truncate, which is correct and is why the ellipsis is
    asserted separately on prose that survives redaction intact.
    """
    blob = RunError(ErrorCategory.PLATFORM_ERROR, "x" * 50_000)
    assert len(blob.message) <= 2000, len(blob.message)

    prose = RunError(ErrorCategory.PLATFORM_ERROR, "the container said no. " * 5_000)
    assert len(prose.message) <= 2000, len(prose.message)
    assert prose.message.endswith("..."), "a truncated message must say it was truncated"


@test
def test_the_api_redacts_an_error_detail_it_was_handed():
    from api.main import PlatformError

    exc = PlatformError(500, "registry error", "open failed: /tmp/x?sessionid=livecookievalue")
    assert "livecookievalue" not in exc.body["detail"], exc.body
    assert exc.body["error"] == "registry error", "the fixed label is untouched"


# --- what the API exposes ---------------------------------------------------


@test
def test_the_api_adds_the_new_fields_without_moving_the_old_ones():
    """Section 21: add fields, never repurpose them."""
    from api.schemas import RunDetail, RunSummary

    with registry() as reg:
        run = started(reg)
        error = RunError(ErrorCategory.ACCESS_ERROR, "private", detail="PRIVATE_RESOURCE")
        reg.complete(run.run_id, status=RunStatus.FAILED, exit_code=1, error=error.message,
                     error_info=error.to_dict(), partial=True)
        stored = reg.get(run.run_id)

        detail = RunDetail.from_run(stored).model_dump()
        assert detail["error"] == "private", "`error` is still the sentence it always was"
        assert detail["error_info"]["code"] == "ACCESS_ERROR"
        assert detail["error_info"]["detail"] == "PRIVATE_RESOURCE"
        assert detail["partial"] is True
        # The run directory must still be absent from what the API can name.
        assert "run_dir" not in detail

        summary = RunSummary.from_run(stored).model_dump()
        assert summary["error_code"] == "ACCESS_ERROR"
        assert summary["partial"] is True
        assert "error_info" not in summary, "a listing carries the category, not the whole error"


@test
def test_a_successful_run_reports_no_category_anywhere():
    from api.schemas import RunDetail, RunSummary

    with registry() as reg:
        run = started(reg)
        write_output(reg, run.run_id, {"status": "success"})
        reg.complete(run.run_id, status=RunStatus.SUCCEEDED, exit_code=0)
        stored = reg.get(run.run_id)

        assert RunDetail.from_run(stored).error_info is None
        assert RunSummary.from_run(stored).error_code is None
        assert RunDetail.from_run(stored).partial is False


def main() -> int:
    failures = 0
    print(f"running {len(TESTS)} error-handling tests\n")

    for fn in TESTS:
        name = fn.__name__.removeprefix("test_")
        try:
            fn()
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {name}")
            print("        " + traceback.format_exc().strip().replace("\n", "\n        "))
        else:
            print(f"  ok    {name}")

    print()
    print(f"{failures} of {len(TESTS)} tests FAILED" if failures else f"all {len(TESTS)} tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
