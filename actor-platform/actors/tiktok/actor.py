#!/usr/bin/env python3
"""tiktok Actor -- an ADAPTER around the TikTok collector.

DEMO BUILD: the vendored collector returns synthetic data (see
collectors/demo.py in the repository), so this Actor makes no network request
and needs no browser. The adapter itself is unchanged in shape:

    platform input JSON   ->  collect_profile_posts / collect_profile_comments args
    collector's logging   ->  stderr
    collector's documents ->  one result at ACTOR_OUTPUT_PATH
    collector's status    ->  an exit code the platform understands

It still installs the connected account's session into a scratch directory,
points the collector at it, mounts the per-account state directory and writes
a refreshed session back - the platform features this Actor exercises.

    python3 actor.py                    # reads ACTOR_INPUT_PATH
"""

from __future__ import annotations

import json
import logging
import contextlib
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any

# Contract paths, duplicated rather than imported: an Actor image must not
# depend on the platform's source tree.
DEFAULT_INPUT_PATH = "/actor/storage/input.json"
DEFAULT_OUTPUT_PATH = "/actor/storage/output.json"

# The session, forwarded BY NAME through ACTOR_FORWARD_ENV. The platform reads
# the value from the Worker's environment and hands it to the container; it
# never passes through the input document, the queue, the registry or a log.
SESSION_ENV = "TIKTOK_SESSION"

# Where to hand a REFRESHED session back to the platform, when it asked for one.
# Set by the worker on a private mount OUTSIDE the run directory, so a live
# session never lands in the tree the API serves artifacts from.
SESSION_OUT_ENV = "ACTOR_SESSION_OUT"

# The DURABLE mount, when the platform gave this run one: a directory keyed to
# the connected account that survives this container. The collector keeps its
# browser profile in there.
#
# WHY IT MATTERS HERE MORE THAN ANYWHERE ELSE. The scrape runs a real browser,
# and this site checks the DEVICE as well as the login: the profile's local
# storage and databases are where a solved verification is recorded, and the
# cookie jar the platform stores cannot carry them. A profile rebuilt in a
# temporary directory each run therefore arrives as a brand new device every
# time and gets verified every time -- which is the failure this mount removes.
# Without the mount everything still works exactly as before, in scratch space.
STATE_DIR_ENV = "ACTOR_STATE_DIR"

# The collector reads this for its persistent Playwright profile.
PROFILE_DIR_ENV = "TIKTOK_PROFILE_DIR"
PROFILE_SUBDIR = "browser-profile"

# Chrome's single-instance guard, which OUTLIVES A KILLED CONTAINER and then
# bricks the account.
#
# MEASURED: a run whose container was killed mid-scrape left
# `SingletonLock -> 43b999fcdc15-22` -- a hostname and pid from a container that
# no longer exists. Every later run on that account then died at launch with
# "The profile appears to be in use by another Google Chrome process (22) on
# another computer", which reads as a code fault and is not one. The persistent
# mount is what makes it permanent: nothing clears it between runs, because
# not clearing things is the entire point of that mount.
#
# Removing them at startup is safe BECAUSE OF concurrency_scope: the platform
# runs at most one container per connected account, so a lock present when this
# run begins cannot belong to a live browser. That guarantee is the whole
# justification -- without it this would be how two browsers corrupt one profile.
SINGLETON_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")

# The cookie that proves a live session. The rest of the jar -- the CSRF token,
# the device ids -- is handed to logged-OUT visitors too, so requiring any of it
# would let a bounced run pass as a live session and let the write-back replace
# a good stored session with a dead one.
#
# THE WRITE-BACK MATTERS MORE HERE THAN ANYWHERE ELSE. A solved CAPTCHA's
# verification token lives in this jar, so exporting it is what stops every run
# facing a fresh slider. The collector's own persistent browser profile cannot
# do that job in a container: it lives under the data directory, which is
# scratch space destroyed when the container exits.
SESSION_COOKIES = ("sessionid",)

# The name the shared report layer switches its vocabulary on, and the label the
# report carries. This site has a real shares metric and real view counts, so it
# takes the default wording -- but the label still has to be right, and it is
# the collector's own PLATFORM string verbatim so the two cannot disagree.
PLATFORM = "TikTok"

# Report artifacts are named after the Actor, not "report.pdf". They are served
# by name (`GET /runs/{id}/artifacts/<name>`) and downloaded into one folder,
# where six Actors all writing report.pdf / report.txt / dashboard.html give a
# person three files whose names say nothing about which scrape produced them.
ARTIFACT_PREFIX = "tiktok"

EXIT_OK = 0
EXIT_ACTOR_FAILED = 1

# Collector feed statuses that mean the platform's request could not be served.
# `no_posts_found` is deliberately NOT here: an empty result from a valid window
# is a real answer, not a failure.
#
# The last three come from the collector's reliability layer, and they are
# failures for a reason worth stating: each one means the site REFUSED to
# answer. Before they existed all three arrived as `no_posts_found` -- a run
# that was challenged, rate-limited or blocked exited 0 and reported an empty
# result, which is indistinguishable from an account that posts nothing. A
# caller acting on that would schedule the same refused run again tomorrow.
FAILURE_STATUSES = {
    "login_wall", "profile_unavailable", "hashtag_unavailable", "error",
    # The collector REFUSED rather than failed: a verification page, a rate
    # limit, a block, or -- the last one -- its own robots.txt check declining
    # to ask at all. All four are non-zero exits because none of them returned
    # the data the caller asked for, and reporting them as success would make a
    # refusal indistinguishable from an account with nothing to show.
    "challenge", "rate_limited", "blocked", "robots_disallowed",
    # robots.txt unreadable and nothing recent cached: refused, retryable.
    "robots_unavailable",
}


def _now_iso(offset: float = 0.0) -> str:
    """UTC, ISO-8601."""
    return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()


def _quietly(call, default=None):
    """Run `call`, swallowing anything it raises."""
    try:
        return call()
    except Exception:
        return default


def log(message: str) -> None:
    """Actor progress goes to STDERR, so stdout carries only the result JSON."""
    print(f"[tiktok] {message}", file=sys.stderr, flush=True)


class InputError(ValueError):
    """The input could not be used. The Actor's own defensive validation."""


# --- input ------------------------------------------------------------------


def read_input(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        raise InputError(f"no input file at {path}")
    with open(path, encoding="utf-8") as handle:
        raw = handle.read().strip()
    if not raw:
        raise InputError(f"input file {path} is empty")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputError(f"input file {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise InputError(f"input must be a JSON object, got {type(payload).__name__}")
    return payload


def parse_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate defensively.

    The API already checks input_schema.json before a run is created, but an
    Actor is runnable by hand and through the direct executor path, so it
    validates its own input rather than trusting that it was checked.
    """
    target = payload.get("target")
    if not isinstance(target, str) or not target.strip():
        raise InputError('input field "target" is required and must be a non-empty string')

    def optional_int(name: str, low: int, high: int) -> int | None:
        value = payload.get(name)
        if value is None:
            return None
        # bool is an int subclass; True would otherwise pass as 1.
        if not isinstance(value, int) or isinstance(value, bool):
            raise InputError(f'input field "{name}" must be an integer')
        if not low <= value <= high:
            raise InputError(f'input field "{name}" must be between {low} and {high}, got {value}')
        return value

    def optional_str(name: str) -> str | None:
        value = payload.get(name)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise InputError(f'input field "{name}" must be a non-empty string')
        return value.strip()

    # ON unless explicitly turned off, matching the Facebook Actor. Off by
    # default meant a run started without ticking the box collected posts
    # only, even with max_comments_per_post set, and nothing said so.
    collect_comments = payload.get("collect_comments", True)
    if not isinstance(collect_comments, bool):
        raise InputError('input field "collect_comments" must be true or false')

    mode = payload.get("mode", "profile")
    if mode not in ("profile", "hashtag"):
        raise InputError('input field "mode" must be "profile" or "hashtag"')

    analyse = payload.get("analyse", True)
    if not isinstance(analyse, bool):
        raise InputError('input field "analyse" must be true or false')

    ai_prompt = payload.get("ai_prompt")
    if ai_prompt is not None and not isinstance(ai_prompt, str):
        raise InputError('input field "ai_prompt" must be a string')

    return {
        "target": target.strip(),
        # max_posts stays None when absent, and that is load-bearing: the
        # collector documents None + a window as "the date boundary is the
        # stopping condition", and substituting a number makes every tier
        # measure itself against a threshold the caller never asked for.
        "max_posts": optional_int("max_posts", 1, 5000),
        "start_date": optional_str("start_date"),
        "end_date": optional_str("end_date"),
        "mode": mode,
        "collect_comments": collect_comments,
        "max_comments_per_post": optional_int("max_comments_per_post", 1, 5000),
        # 0 is MEANINGFUL here - "collect the comments but none of their
        # replies" - so the floor is 0 rather than 1, unlike every other cap.
        "max_replies_per_comment": optional_int("max_replies_per_comment", 0, 200),
        "analyse": analyse,
        "ai_prompt": (ai_prompt or "").strip(),
    }


# --- analysis and reports ----------------------------------------------------


def analyse_and_report(posts_doc: dict, comments_doc: dict | None,
                       ai_prompt: str, artifacts_dir: str) -> dict[str, Any]:
    """Turn the scrape into an executive report. -> a summary of what was made.

    BEST EFFORT, ALWAYS. A scrape is minutes to hours of work and the analysis
    is two API calls; losing the first because the second failed would be
    absurd. Every failure here is logged and returned as `analysis.error`, and
    the run still succeeds with its data intact. That mirrors what the pipeline
    already does with e-mail and segmentation.

    THE ADAPTER STILL CONTAINS NO LOGIC. Reshaping, analysis and rendering are
    all upstream code, vendored verbatim; this only decides the order and where
    the files land.
    """
    summary: dict[str, Any] = {"attempted": True}
    try:
        from processors import tiktok_comments_processor as comments_processor
        from processors import tiktok_page_processor as page_processor
        from ai.claude import analyze_comments, analyze_page
        from reports.dashboard import generate_dashboard
        from reports.tiktok_report import (
            format_combined_text_report,
            generate_combined_pdf,
        )
    except ImportError as exc:
        log(f"analysis unavailable: {exc}")
        return {"attempted": False, "error": f"analysis layer not installed: {exc}"}

    os.makedirs(artifacts_dir, exist_ok=True)

    try:
        # process() is a PURE reshape of the collector's own document, so the
        # documents already in memory are passed straight in -- no writing them
        # out and reading them back just to satisfy a file-based entry point.
        page_data = page_processor.process(posts_doc)
        comments_data = (
            comments_processor.process(comments_doc) if comments_doc else {"posts": []}
        )

        log("analysing the page" + (" with your instructions" if ai_prompt else ""))
        page_analysis = analyze_page(page_data, ai_prompt)

        comment_analysis = None
        if any(p.get("comments") for p in comments_data.get("posts", [])):
            log("analysing comments")
            comment_analysis = analyze_comments(comments_data, ai_prompt)
        else:
            log("no comments to analyse")

        summary["page"] = {
            "executive_summary": page_analysis.executive_summary,
            "performance_rating": page_analysis.performance_rating,
            "key_insights": page_analysis.key_insights,
            "content_recommendations": page_analysis.content_recommendations,
        }
        if comment_analysis is not None:
            summary["comments"] = {
                "executive_summary": comment_analysis.executive_summary,
                "overall_sentiment": comment_analysis.overall_sentiment,
                "sentiment_score": comment_analysis.sentiment_score,
                "positive_topics": comment_analysis.positive_topics,
                "negative_topics": comment_analysis.negative_topics,
                "recommendations": comment_analysis.recommendations,
            }
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc(file=sys.stderr)
        log(f"analysis failed: {type(exc).__name__}: {exc}")
        return {"attempted": True, "error": f"{type(exc).__name__}: {exc}"}

    # Rendering is separate from analysis, and fails separately: a report that
    # cannot be drawn must not discard an analysis that succeeded.
    written: list[str] = []
    try:
        if comment_analysis is not None:
            # platform=PLATFORM on EVERY renderer, or the report labels itself
            # as another platform.
            text = format_combined_text_report(page_data, page_analysis,
                                               comment_analysis, comments_data,
                                               platform=PLATFORM)
            pdf = generate_combined_pdf(page_data, page_analysis, comments_data,
                                        comment_analysis,
                                        output_path=os.path.join(artifacts_dir, f"{ARTIFACT_PREFIX}_report.pdf"),
                                        platform=PLATFORM)
            dashboard = generate_dashboard(
                page_data, page_analysis, comments_data, comment_analysis,
                output_path=os.path.join(artifacts_dir, f"{ARTIFACT_PREFIX}_dashboard.html"),
                platform=PLATFORM)
        else:
            from reports.tiktok_report import format_page_text_report, generate_page_pdf

            text = format_page_text_report(page_data, page_analysis, platform=PLATFORM)
            pdf = generate_page_pdf(page_data, page_analysis,
                                    output_path=os.path.join(artifacts_dir, f"{ARTIFACT_PREFIX}_report.pdf"),
                                    platform=PLATFORM)
            dashboard = generate_dashboard(
                page_data, page_analysis,
                output_path=os.path.join(artifacts_dir, f"{ARTIFACT_PREFIX}_dashboard.html"),
                platform=PLATFORM)

        report_path = os.path.join(artifacts_dir, f"{ARTIFACT_PREFIX}_report.txt")
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        written = [os.path.basename(p) for p in (report_path, str(pdf), str(dashboard))]
        log(f"wrote {len(written)} report artifact(s): {', '.join(written)}")
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc(file=sys.stderr)
        log(f"report rendering failed: {type(exc).__name__}: {exc}")
        summary["render_error"] = f"{type(exc).__name__}: {exc}"

    summary["artifacts"] = written
    return summary


# --- session ----------------------------------------------------------------


def export_refreshed_session(data_dir: str) -> bool:
    """Hand the platform the session as it stands after the scrape.

    WHY THIS EXISTS: the site rotates its session cookies server-side DURING a
    session, and the collector writes the rotated jar back to
    $TIKTOK_DATA_DIR/tiktok_auth.json. That
    directory is a mkdtemp inside a --rm container, so without this the refresh
    is destroyed when the container exits and every stored session can only get
    staler -- a working account quietly dies after a few weeks of scheduled runs,
    presenting as the site blocking you rather than as a bug.

    REFUSES A LOGGED-OUT JAR. A run that ended on the login wall holds a jar
    missing `sessionid`; exporting it would overwrite a good stored session with a dead
    one. Same guard the collector's own save_storage_state applies, for the same
    reason -- and the platform checks again on its side, because an Actor is not
    a trusted component.

    Best-effort throughout: a failure here must never fail a scrape that worked.
    """
    out = os.environ.get(SESSION_OUT_ENV, "").strip()
    if not out:
        return False  # the platform did not ask for one

    source = os.path.join(data_dir, "tiktok_auth.json")
    if not os.path.exists(source):
        return False

    try:
        with open(source, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log(f"could not read the refreshed session: {exc}")
        return False

    cookies = state.get("cookies") if isinstance(state, dict) else None
    cookies = cookies if isinstance(cookies, list) else []
    if not all(
        any(c.get("name") == name and c.get("value") for c in cookies)
        for name in SESSION_COOKIES
    ):
        log(f"not exporting the session: {len(cookies)} cookie(s), not a live login")
        return False

    try:
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.chmod(out, 0o600)
    except OSError as exc:
        log(f"could not export the refreshed session: {exc}")
        return False

    # The COUNT, never the contents: stdout and stderr become log.txt, which is
    # served over HTTP by GET /runs/{id}/logs.
    log(f"exported the refreshed session ({len(cookies)} cookies)")
    return True


def _clear_stale_locks(profile: str) -> int:
    """Drop Chrome's single-instance files. -> how many were removed.

    See SINGLETON_FILES: one killed container otherwise makes every later run
    on that account fail at browser launch, for ever.
    """
    removed = 0
    for name in SINGLETON_FILES:
        path = os.path.join(profile, name)
        # islink, not exists: these are SYMLINKS to a host/pid that no longer
        # resolves, and os.path.exists follows the link and answers False.
        if os.path.islink(path) or os.path.exists(path):
            try:
                os.unlink(path)
                removed += 1
            except OSError as exc:
                log(f"WARNING: could not clear {name} ({exc}); the browser may "
                    "refuse to start on this profile")
    if removed:
        log(f"cleared {removed} stale browser lock file(s) left by an earlier run")
    return removed


def install_browser_profile() -> str | None:
    """Point the collector's persistent profile at the durable mount. -> path.

    The whole integration is one environment variable: the collector already
    resolves its profile from $TIKTOK_PROFILE_DIR, so this decides WHERE and
    changes nothing about how the browser is driven.

    Returns None when the platform gave this run no durable mount -- which is
    every direct `docker run`, and any run with no connected account -- and the
    collector then uses its own scratch profile exactly as it did before.

    NOTHING FROM INSIDE THE PROFILE IS READ, LOGGED OR RETURNED. It holds a
    logged-in browser's cookies and local storage; this function knows only its
    path, and the path is all that is ever printed.
    """
    state = os.environ.get(STATE_DIR_ENV, "").strip()
    if not state:
        return None
    if not os.path.isdir(state):
        log(f"WARNING: {STATE_DIR_ENV} is set to {state}, which is not a directory "
            "- falling back to a scratch profile for this run")
        return None

    profile = os.path.join(state, PROFILE_SUBDIR)
    try:
        os.makedirs(profile, exist_ok=True)
        os.chmod(profile, 0o700)
    except OSError as exc:
        log(f"WARNING: could not use the persistent profile at {profile} ({exc}) "
            "- falling back to a scratch profile for this run")
        return None

    os.environ[PROFILE_DIR_ENV] = profile
    _clear_stale_locks(profile)
    # Whether this run STARTS from an existing profile is the single most
    # useful line in this log when someone asks "why am I being verified every
    # time?" -- so it is reported, as a boolean, with no contents.
    existing = bool(os.listdir(profile))
    log(f"using the persistent browser profile at {profile} "
        f"({'reusing existing state' if existing else 'new, will be created'})")
    return profile


def install_session(data_dir: str) -> bool:
    """Materialise the forwarded session as the tiktok_auth.json the collector reads.

    The collector resolves its session from $TIKTOK_DATA_DIR/tiktok_auth.json, so this
    is the whole integration: write the file, and the existing auth code is
    unchanged. Accepts either a Playwright storage_state document or a plain
    "sessionid=..." cookie header, because both are things people have.

    The VALUE is never logged -- only whether one arrived.
    """
    raw = os.environ.get(SESSION_ENV, "").strip()
    if not raw:
        return False

    state: dict[str, Any] | None = None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("cookies"), list):
            state = parsed
    except json.JSONDecodeError:
        state = None

    if state is None:
        cookies = []
        for pair in raw.split(";"):
            name, _, value = pair.strip().partition("=")
            if name and value:
                cookies.append(
                    {"name": name.strip(), "value": value.strip(),
                     "domain": ".tiktok.com", "path": "/"}
                )
        if not cookies:
            raise InputError(
                f"{SESSION_ENV} is set but is neither a storage_state document "
                "nor a 'name=value; ...' cookie string"
            )
        state = {"cookies": cookies, "origins": []}

    path = os.path.join(data_dir, "tiktok_auth.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.chmod(path, 0o600)
    log(f"installed a session with {len(state.get('cookies', []))} cookie(s)")
    return True


# --- output -----------------------------------------------------------------


def write_output(path: str, payload: Any) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def build_result(posts_doc: dict, comments_doc: dict | None) -> dict[str, Any]:
    """One result document, assembled from the collector's OWN documents.

    Deliberately not a second TikTok data model: page_information, posts and
    date_window are passed through exactly as the collector built them, so the
    date-window accounting (stop_reason, complete, window_reached, reach) and
    the per-post comment accounting survive to the caller intact. Comments are
    attached to their post rather than left in a parallel list, because the
    platform stores ONE output artifact per run.
    """
    result = {
        "platform": PLATFORM,
        "target": posts_doc.get("page_information", {}).get("page_url"),
        "status": posts_doc.get("status"),
        "page_information": posts_doc.get("page_information", {}),
        "posts": posts_doc.get("posts", []),
        "post_count": len(posts_doc.get("posts", [])),
    }
    if posts_doc.get("date_window"):
        result["date_window"] = posts_doc["date_window"]
    # WHY THE RUN ENDED THE WAY IT DID, when the collector had something to say
    # about it: the session state, the retry policy, the HTTP statuses seen, and
    # the structured error (status / error_code / message / retryable) for a
    # refused run. Passed through exactly as built, like every other block here.
    #
    # It carries NO SESSION MATERIAL. The collector builds it from statuses,
    # counts and a URL with its query string removed, which matters because this
    # document is served by GET /runs/{id}/output.
    if posts_doc.get("reliability"):
        result["reliability"] = posts_doc["reliability"]
    if comments_doc is not None:
        by_url = {r.get("post_url"): r for r in comments_doc.get("posts", []) if r}
        total = 0
        for post in result["posts"]:
            record = by_url.get(post.get("post_url"))
            if not record:
                continue
            # The whole record: comments AND their accounting (stop_reason,
            # collection_complete, counts), which is what tells a reader
            # whether 16 comments was the platform's cap or the post's total.
            post["comments"] = record.get("comments", [])
            post["comment_collection"] = {
                key: record[key]
                # THIS COLLECTOR'S accounting keys. Copying another platform's
                # list yielded an empty block on every post -- the comprehension
                # filters by `if key in record`, so a wrong list is silently
                # nothing rather than an error, and the reader loses the only
                # signal distinguishing "this post has two comments" from "the
                # scrape stopped early".
                # THIS COLLECTOR'S accounting keys. `comment_responses` is the
                # one that matters: it counts the signed responses TikTok
                # actually served, so zero comments with zero responses is the
                # site declining to answer, while zero comments with responses
                # is a video that genuinely has none.
                # The per-STAGE keys are the ones added last and they are the
                # point of the block: `status` is the video's outcome and was
                # `success` on a video that handed over its metrics and not one
                # comment. `comments_status` / `replies_status` /
                # `collection_complete` / `stop_reason` are what make a partial
                # collection readable instead of indistinguishable from a whole
                # one. `stop_reason` already existed on refused records and had
                # never reached output.json, because this list did not name it.
                for key in (
                    "comments_scraped", "replies_scraped", "comments_count",
                    "comment_responses", "status", "login_wall",
                    "comments_status", "replies_status", "collection_complete",
                    "stop_reason", "failure_reason", "requested_comments",
                    "comment_pages_collected", "reply_threads_attempted",
                    "replies_dropped", "comment_source",
                )
                if key in record
            }
            total += len(post["comments"])
        result["comment_count"] = total
        result["reply_count"] = count_replies(result["posts"])
        result["comment_collection_summary"] = _comment_stage_summary(
            comments_doc.get("posts") or [])
    return result


def _comment_stage_summary(records: list[dict]) -> dict[str, Any]:
    """How the comment stage went across the whole batch, in one block.

    A RUN-LEVEL answer to "did the comments work?", which until now had to be
    reconstructed by reading every post. The exit code deliberately stays with
    the POSTS document - a comment failure must not fail a good post scrape, and
    `FAILURE_STATUSES` is read against the posts status alone - so this is how a
    partial run says so without pretending to be a failed one.
    """
    by_status: dict[str, int] = {}
    for record in records:
        key = str(record.get("comments_status") or "unknown")
        by_status[key] = by_status.get(key, 0) + 1
    incomplete = [r for r in records if not r.get("collection_complete")]
    summary: dict[str, Any] = {
        "videos": len(records),
        "comments_status": by_status,
        "complete": not incomplete,
    }
    if incomplete:
        # Named, not counted: a reader chasing a short result wants to know
        # WHICH videos fell short and why, and the reasons differ per video.
        summary["incomplete_videos"] = [
            {"video_id": r.get("video_id"),
             "comments_scraped": r.get("comments_scraped"),
             "requested": r.get("requested_comments"),
             "stop_reason": r.get("stop_reason")}
            for r in incomplete
        ]
    dropped = sum(r.get("replies_dropped") or 0 for r in records)
    if dropped:
        summary["replies_dropped"] = dropped
    return summary


def count_replies(posts: list[dict]) -> int:
    """Replies-to-comments: what sits under each comment's `reply_comments`.

    `comment_count` is TOP-LEVEL comments only, and every Actor defines it that
    way. The collectors' own summaries count both levels, so a result reading
    `comment_count: 86` beside a log saying "96 unique replies" looked like ten
    had been lost. They had not - they were nested. Reporting this beside it
    makes the two numbers add up instead of contradict. Every collector nests
    under the same key, which is what lets one rule serve all of them.
    """
    return sum(len(comment.get("reply_comments") or [])
               for post in posts or []
               for comment in post.get("comments") or [])


def summarise(result: dict[str, Any]) -> dict[str, Any]:
    """The compact JSON printed on stdout.

    The FULL result goes to ACTOR_OUTPUT_PATH, which is what the platform reads
    and serves from GET /runs/{id}/output. Echoing all of it here too would
    duplicate an 88 KB document into log.txt, since the platform captures
    stdout and stderr into one log. This is the same split the platform's own
    CLI makes: metadata inline, artifact by reference.
    """
    summary = {
        "status": result.get("status"),
        "target": result.get("target"),
        "post_count": result.get("post_count"),
    }
    if "comment_count" in result:
        summary["comment_count"] = result["comment_count"]
    if "reply_count" in result:
        summary["reply_count"] = result["reply_count"]
    stage = result.get("comment_collection_summary")
    if stage and not stage.get("complete"):
        # PARTIAL, said on stdout. The run succeeded and its comments did not,
        # and a summary that reports only counts cannot express the difference.
        summary["comments_partial"] = len(stage.get("incomplete_videos") or [])
    reliability = result.get("reliability") or {}
    if reliability.get("error_code"):
        summary["error_code"] = reliability["error_code"]
        summary["retryable"] = reliability.get("retryable", False)
    window = result.get("date_window")
    if window:
        summary["date_window"] = {
            key: window.get(key)
            for key in ("start", "end", "complete", "stop_reason", "window_reached")
            if key in window
        }
    return summary


# --- run --------------------------------------------------------------------


@contextlib.contextmanager
def stdout_to_stderr():
    """Send everything the collector PRINTS to stderr for the duration.

    Not defensive tidiness -- measured. The collector reports through both
    `logging` and bare print() ("Hit the login wall", "Saved 0 post(s)"),
    and those landed on stdout ahead of the result, making the Actor's stdout
    invalid JSON. Redirecting the stream is the fix that needs no change to the
    collector, which is the code this milestone must not rewrite.
    """
    original = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = original


DATASET_FILE = "dataset.jsonl"


def push_items(collection: str, rows: list[tuple[str | None, Any]]) -> int:
    """Append items to the run's dataset, the platform's optional streaming part.

    The platform tails this file into its database while the run goes, so what
    is written here is reachable before output.json exists -- and survives a run
    that dies later. output.json is still written as before and is still the
    result; this is additive.

    Beside ACTOR_OUTPUT_PATH, because that is the storage mount. NEVER FATAL: a
    full disk here must not cost the scrape the result it is about to write.
    """
    if not rows:
        return 0
    path = os.path.join(os.path.dirname(os.environ.get("ACTOR_OUTPUT_PATH")
                                        or DEFAULT_OUTPUT_PATH), DATASET_FILE)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            for key, data in rows:
                fh.write(json.dumps({"collection": collection, "key": key, "data": data},
                                    ensure_ascii=False) + "\n")
    except OSError as exc:
        log(f"could not append {len(rows)} {collection} item(s): {exc}")
        return 0
    return len(rows)


def _video_id(record: dict[str, Any]) -> str | None:
    vid = record.get("video_id")
    if vid:
        return str(vid)
    match = re.search(r"/(?:video|photo)/(\d+)", str(record.get("post_url") or ""))
    return match.group(1) if match else None


def comment_items(comments_doc: dict[str, Any] | None) -> list[tuple[str | None, Any]]:
    """One item per top-level comment, replies nested, each naming its post.

    Keyed by the video id -- NOT unique, and it need not be: the platform's
    identity for an item is its position, and the key is what a reader filters
    or joins on. Each item carries post_url so it stands on its own.
    """
    rows: list[tuple[str | None, Any]] = []
    for record in (comments_doc or {}).get("posts", []) or []:
        vid = _video_id(record)
        for comment in record.get("comments", []) or []:
            rows.append((vid, {"video_id": vid, "post_url": record.get("post_url"),
                               **comment}))
    return rows


def run(settings: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Drive the collector. -> (result, exit code).

    Imported HERE rather than at module scope so that a bad input is reported
    before the collector's import-time work happens.
    """
    from collectors.daterange import parse_window
    from collectors.tiktok import (
        AuthError,
        BrowserUnavailable,
        collect_hashtag_posts,
        collect_profile_comments,
        collect_profile_posts,
    )
    # The collector's OWN default for an unset cap, rather than a number
    # repeated here - two copies of it would drift.
    from collectors.tiktok import PROFILE_COMMENTS_PER_POST
    from collectors.tiktok.pipeline import worklist

    try:
        window = parse_window(settings["start_date"], settings["end_date"])
    except ValueError as exc:
        raise InputError(str(exc)) from exc

    hashtag = settings["mode"] == "hashtag"
    log(f"scraping {'hashtag' if hashtag else 'profile'} {settings['target']}"
        + (f" over {settings['start_date'] or '...'}..{settings['end_date'] or '...'}" if window else "")
        + (f", up to {settings['max_posts']} post(s)" if settings["max_posts"] else "")
        + ((f", then up to {settings['max_comments_per_post']} comment(s) per post"
            if settings["max_comments_per_post"] else ", then each post's comments")
           if settings["collect_comments"] else ", comments OFF (collect_comments is false)"))

    # A HASHTAG FEED IS RANKED, NOT CHRONOLOGICAL -- top posts first, then
    # recent. The collector therefore scans and filters it rather than stopping
    # early, so a window over one can only narrow what the feed happens to
    # serve; it cannot page backwards to cover a period the way a profile can.
    # Said here because a caller who gets few posts back deserves to know which
    # of the two they asked for.
    if hashtag and window:
        log("note: a hashtag feed is ranked, so a date range filters it rather "
            "than covering it; use a profile target to cover a period")

    collect = collect_hashtag_posts if hashtag else collect_profile_posts
    try:
        with stdout_to_stderr():
            posts_doc = collect(
                settings["target"],
                num_posts=settings["max_posts"],
                window=window,
            )
    except BrowserUnavailable as exc:
        # NOT a session problem, and saying so matters: a real run installed 86
        # cookies and passed the robots gate before the CDP connect was refused,
        # and reporting "no usable TikTok session" sent the reader to re-login
        # when what they needed was to start a browser. The collector's message
        # is already written for whoever operates this, so it is passed through
        # rather than replaced.
        log(f"the collector reported: {exc}")
        raise InputError(str(exc)) from exc
    except AuthError as exc:
        # A dead or missing session is an ACTOR failure, not a platform one:
        # the platform started this container perfectly well. The collector's
        # own message names login_tiktok.py and a path inside this container,
        # neither of which the caller can act on, so it is replaced rather than
        # passed through.
        log(f"the collector reported: {exc}")
        raise InputError(
            f"no usable TikTok session. Set {SESSION_ENV} in the Worker's "
            f"environment and list it in ACTOR_FORWARD_ENV; the value is a "
            f"Playwright storage_state document or a "
            f"'sessionid=...' cookie string"
        ) from exc

    status = posts_doc.get("status")
    posts = posts_doc.get("posts", [])
    log(f"posts stage finished: status={status}, {len(posts)} post(s)")
    # Reachable NOW, while the (much longer) comment stage runs.
    push_items("posts", [(_video_id(p), p) for p in posts])

    comments_doc = None
    if settings["collect_comments"] and posts:
        urls = [p.get("post_url") for p in posts if p.get("post_url")]
        log(f"collecting comments from {len(urls)} post(s)")
        # NO window= here, and that is the collector's shape rather than an
        # omission: collect_profile_comments takes only a per-post cap, so the
        # date range bounds which POSTS are scraped and every comment on those
        # posts is in scope. The optional kwarg is passed only when the caller
        # set one, so the collector's own default survives.
        kwargs: dict[str, Any] = {}
        if settings["max_comments_per_post"] is not None:
            kwargs["max_comments_per_post"] = settings["max_comments_per_post"]
        if settings["max_replies_per_comment"] is not None:
            kwargs["max_replies_per_comment"] = settings["max_replies_per_comment"]
        # THE WORK LIST COMES FROM THE COLLECTOR'S OWN STAGE, not from a second
        # computation here. This adapter and the CLI used to work out "which
        # videos still need comments" independently; one wiring is the point of
        # the staged pipeline, and two would drift the moment either changed.
        with stdout_to_stderr():
            pending, stage = worklist({"posts": posts})
            comments_doc = collect_profile_comments(pending, **kwargs)
        log(f"work list: {stage.as_dict()}")
        # After the whole batch for now; per video once the comment fan-out
        # lands and hands each finished video over as it completes.
        pushed = push_items("comments", comment_items(comments_doc))
        log(f"streamed {pushed} comment item(s)")

    result = build_result(posts_doc, comments_doc)

    # AFTER the data is assembled, so a failure here cannot cost the scrape.
    if settings["analyse"] and posts_doc.get("posts"):
        result["analysis"] = analyse_and_report(
            posts_doc, comments_doc, settings["ai_prompt"],
            os.path.join(os.path.dirname(os.environ.get("ACTOR_OUTPUT_PATH")
                                         or DEFAULT_OUTPUT_PATH), "artifacts"),
        )

    # STATUS DECIDES THE EXIT CODE, and `no_posts_found` is a SUCCESS.
    #
    # An empty result from a valid date window is the collector answering the
    # question truthfully -- there were no posts in that range. Reporting it as
    # a failure would turn a real answer into a platform error, and the
    # collector's own CLI is explicit that this is not fetch_failed.
    if status in FAILURE_STATUSES:
        log(f"ERROR: the scrape failed with status={status}")
        return result, EXIT_ACTOR_FAILED
    return result, EXIT_OK


def _fail(output_path: str, reason: str) -> int:
    """Report a failure the same way as a success: a result, and JSON on stdout.

    A caller parsing stdout should not have to handle "sometimes it is JSON":
    a failed run is still a machine-readable answer, it just carries an error.
    The full document goes to ACTOR_OUTPUT_PATH so `GET /runs/{id}/output`
    works for failures too, rather than 404ing and leaving only a log to read.
    """
    print(f"[tiktok] ERROR: {reason}", file=sys.stderr, flush=True)
    result = {"platform": PLATFORM, "status": "error", "error": reason,
              "posts": [], "post_count": 0}
    try:
        write_output(output_path, result)
    except OSError as exc:
        print(f"[tiktok] could not write the result: {exc}", file=sys.stderr, flush=True)
    print(json.dumps({"status": "error", "error": reason, "post_count": 0}, ensure_ascii=False))
    return EXIT_ACTOR_FAILED


def point_collector_at_scratch() -> str:
    """Create this run's scratch directory and point the collector at it. -> path.

    MUST RUN BEFORE ANYTHING IMPORTS THE COLLECTOR, which is why it is a named
    step at the very top of main() rather than a line in the middle of it.
    collectors/tiktok/common.py computes DATA_DIR - and STORAGE_STATE_FILE under
    it - from TIKTOK_DATA_DIR at IMPORT time, so an import that happens first
    freezes the image's default (/actor/data/tiktok) and nothing later can move
    it.

    MEASURED, because this was an implicit ordering contract until it broke:
    commit 7a714c7 put a collector import inside configure_logging(), nine lines
    above the assignment this function now owns, and run 20261003-152946496031
    failed with `installed a session with 57 cookie(s)` followed immediately by
    `No usable /actor/data/tiktok/tiktok_auth.json found` - two halves of one
    bug. install_session writes the jar to the scratch directory; the collector
    was looking at the frozen default. Every other collector import in this file
    is deliberately local to a function for the same reason.

    The scratch directory holds the collector's own intermediate documents - its
    internal bootstrap, since the comment stage reads the posts file back - not
    this Actor's result. The platform owns the run directory.
    """
    workdir = tempfile.mkdtemp(prefix="tt-")
    os.environ["TIKTOK_DATA_DIR"] = workdir
    return workdir


def configure_logging() -> None:
    """Send every log line to stderr, and keep credentials out of them.

    A FUNCTION RATHER THAN INLINE SETUP so the second half can be tested: a pin
    that silently stops working leaks a credential on every run afterwards, and
    nothing about the run would look different.
    """
    # EVERY log line to stderr, the collector's included. It logs through the
    # `logging` module, so this one call is what keeps stdout clean enough to
    # carry JSON.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # THE TRANSPORT'S OWN REQUEST LINE IS A CREDENTIAL ON THIS PLATFORM, and it
    # is logged before anything in the collector can sanitise it. Observed live
    # on run 20261003-145833118800: one line per comment video, each carrying a
    # live token out of the signed query string. stdout and stderr become
    # log.txt, which `GET /runs/{id}/logs` serves over HTTP, and the Worker
    # redacts only the secrets IT injected -- one minted inside the browser at
    # run time is in no such list.
    #
    # The collector owns this, not the adapter: it is the half of its own
    # sanitize_url() that a third-party logger puts out of reach, and this
    # Actor has no business naming a transport library (the drift test says so).
    from collectors.tiktok.challenge import quiet_transport_logging
    quiet_transport_logging()


def main() -> int:
    input_path = os.environ.get("ACTOR_INPUT_PATH", DEFAULT_INPUT_PATH)
    output_path = os.environ.get("ACTOR_OUTPUT_PATH", DEFAULT_OUTPUT_PATH)
    run_id = os.environ.get("ACTOR_RUN_ID", "local")

    # FIRST, BEFORE ANYTHING CAN IMPORT THE COLLECTOR. See the docstring.
    workdir = point_collector_at_scratch()

    configure_logging()

    log(f"run {run_id} starting")

    # The scratch directory above stays scratch -- it holds this run's own
    # intermediate documents and the session jar handed back at the end. The
    # BROWSER PROFILE is the one thing that must not live there, so it is
    # redirected to the durable mount when the platform provided one.
    install_browser_profile()

    # And let a person rescue this run if the site challenges it. Must happen
    # BEFORE run() imports the collector: the handler is read when a
    # BrowserManager is built, and the first one is built inside that import's
    # first entry point.

    try:
        payload = read_input(input_path)
        settings = parse_settings(payload)
        if not install_session(workdir):
            # Not fatal here -- the collector raises AuthError with the full
            # explanation -- but saying it early makes the cause obvious in the
            # log rather than looking like a scrape failure.
            log(f"WARNING: no {SESSION_ENV} was forwarded; the scrape will fail without one")
        result, code = run(settings)
    except InputError as exc:
        return _fail(output_path, str(exc))
    except Exception as exc:  # noqa: BLE001 - the Actor reports its own failures
        import traceback
        traceback.print_exc(file=sys.stderr)
        return _fail(output_path, f"{type(exc).__name__}: {exc}")

    export_refreshed_session(workdir)

    write_output(output_path, result)
    log(f"wrote the result to {output_path}")
    print(json.dumps(summarise(result), ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
