#!/usr/bin/env python3
"""facebook Actor -- an ADAPTER around the existing Facebook collector.

DEMO BUILD: the vendored collector returns synthetic data (see
collectors/demo.py in the repository). It makes no network request; the
notes below describe the adapter and the real collector it was built for.

This file contains NO scraping logic and is not where scraping bugs get fixed.
The collector under vendor/collectors/facebook/ is the real thing, developed in
the pipeline repository and copied in verbatim by sync_collector.sh. Everything
here is translation, in four directions:

    platform input JSON   ->  collect_page_posts / collect_page_comments args
    collector's logging   ->  stderr
    collector's documents ->  one result at ACTOR_OUTPUT_PATH
    collector's status    ->  an exit code the platform understands

WHY AN ADAPTER AND NOT A REWRITE: the collector carries a great deal of
measured, hard-won behaviour -- the page feed's server-side date seek, the
verified reverse-chronological early stop and its disarm rule, the separation of
raw-harvest counting from in-window counting, the tier ladder, the transport's
undecodable-vs-soft-block distinction. All of it is reached by calling the
documented entry points, and none of it is reimplemented here.

    python3 actor.py                    # reads ACTOR_INPUT_PATH
"""

from __future__ import annotations

import json
import logging
import contextlib
import os
import sys
import re
import tempfile
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# Contract paths, duplicated rather than imported: an Actor image must not
# depend on the platform's source tree.
DEFAULT_INPUT_PATH = "/actor/storage/input.json"
DEFAULT_OUTPUT_PATH = "/actor/storage/output.json"

# The session, forwarded BY NAME through ACTOR_FORWARD_ENV. The platform reads
# the value from the Worker's environment and hands it to the container; it
# never passes through the input document, the queue, the registry or a log.
SESSION_ENV = "FB_SESSION"

# Where to hand a REFRESHED session back to the platform, when it asked for one.
# Set by the worker on a private mount OUTSIDE the run directory, so a live
# session never lands in the tree the API serves artifacts from.
SESSION_OUT_ENV = "ACTOR_SESSION_OUT"

# The two cookies that together prove a live session. Both required: `c_user`
# survives a logout, so a jar with it and no `xs` looks like a session and is
# not one -- writing that back would replace a good stored session with a dead
# one and turn a single bounced run into "log in again".
SESSION_COOKIES = ("c_user", "xs")

EXIT_OK = 0
EXIT_ACTOR_FAILED = 1

# Collector feed statuses that mean the platform's request could not be served.
# `no_posts_found` is deliberately NOT here: an empty result from a valid window
# is a real answer, not a failure.
FAILURE_STATUSES = {"login_wall", "page_unavailable", "error", "fetch_failed"}

# Report artifacts are named after the Actor, not "report.pdf". They are served
# by name (`GET /runs/{id}/artifacts/<name>`) and downloaded into one folder,
# where six Actors all writing report.pdf / report.txt / dashboard.html give a
# person three files whose names say nothing about which scrape produced them.
ARTIFACT_PREFIX = "facebook"

# BATCHED DELIVERY for big GROUP runs. A windowed group scrape is long -- a
# month of a busy group is ~1,700 feed pages and ~2,600 posts, then hours of
# comments -- and output.json only exists at the very end. So once a group run
# has more than BATCH_SIZE posts, its comments are collected BATCH_SIZE posts at
# a time and each finished batch (posts + their own comments) is streamed to the
# platform as item collection "data#1", "data#2", ... which the console's Data
# tab shows while the run is still going. See _deliver_in_batches.
BATCH_SIZE = 100
BATCH_COLLECTION = "data#{n}"

# The platform tails this file into its database while the run goes (the
# optional streaming part of the Actor contract). Beside ACTOR_OUTPUT_PATH,
# because that is the storage mount.
DATASET_FILE = "dataset.jsonl"


def log(message: str) -> None:
    """Actor progress goes to STDERR, so stdout carries only the result JSON."""
    print(f"[facebook] {message}", file=sys.stderr, flush=True)


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


# A target may be a slug, `groups/<id>`, or a full URL -- and a full URL used to
# be fetched exactly as typed, so `http://169.254.169.254/...` or a service on
# the container's network was one input field away (SSRF), with the run status
# acting as a response oracle. The collector refuses those too
# (collectors/facebook/common.py); this is the Actor's own boundary, checked
# before anything is imported, because an Actor validates its input itself.
TARGET_MAX_LENGTH = 2048
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


def safe_target(target: str) -> str:
    """The target, with any URL rebuilt on https facebook.com. Raises InputError."""
    if len(target) > TARGET_MAX_LENGTH:
        raise InputError(f'input field "target" is longer than {TARGET_MAX_LENGTH} characters')
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in target):
        raise InputError('input field "target" contains control characters')
    if not (_SCHEME.match(target) or target.startswith("//")):
        return target

    refusal = ('input field "target" must be a facebook.com address '
               "(https://www.facebook.com/<page> or https://www.facebook.com/groups/<id>)")
    if any(ch.isspace() or ch == "\\" for ch in target):
        raise InputError(refusal)
    try:
        parts = urlsplit(target)
        port = parts.port
    except ValueError:
        raise InputError(refusal) from None
    host = (parts.hostname or "").lower().rstrip(".")
    if (parts.scheme.lower() not in ("http", "https")
            or parts.username is not None or parts.password is not None
            or port not in (None, 80, 443)
            or not (host == "facebook.com" or host.endswith(".facebook.com"))):
        raise InputError(refusal)
    # Rebuilt from the checked parts, so what is fetched is what was checked.
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def parse_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate defensively.

    The API already checks input_schema.json before a run is created, but an
    Actor is runnable by hand and through the direct executor path, so it
    validates its own input rather than trusting that it was checked.
    """
    target = payload.get("target")
    if not isinstance(target, str) or not target.strip():
        raise InputError('input field "target" is required and must be a non-empty string')
    # A DISPLAY NAME ("<compound name> العاصمة الإدارية") is not a target. The
    # browser-free path turns a bare word into facebook.com/<word>, which is
    # right for a slug and meaningless for a name with spaces: measured
    # 2026-09-21, such a run reported "could not resolve a numeric id" once per
    # date chunk, retried every chunk, and ended page_unavailable. Only the old
    # browser path could search by name. Say so up front instead.
    bare = target.strip()
    if "facebook.com" not in bare.lower() and any(ch.isspace() for ch in bare):
        raise InputError(
            f'input field "target" looks like a page or group NAME ({bare!r}); '
            "give its URL instead, e.g. https://www.facebook.com/<page> or "
            "https://www.facebook.com/groups/<id>")

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

    # ON unless explicitly turned off. Off-by-default meant a run started
    # from the form without ticking the box collected no comments at all --
    # even one that set max_comments_per_post -- and its CSV/JSON download
    # then had an empty comment section under every post.
    collect_comments = payload.get("collect_comments", True)
    if not isinstance(collect_comments, bool):
        raise InputError('input field "collect_comments" must be true or false')

    comment_sort = payload.get("comment_sort")
    if comment_sort is not None and comment_sort not in ("relevant", "newest"):
        raise InputError('input field "comment_sort" must be "relevant" or "newest"')

    analyse = payload.get("analyse", True)
    if not isinstance(analyse, bool):
        raise InputError('input field "analyse" must be true or false')

    ai_prompt = payload.get("ai_prompt")
    if ai_prompt is not None and not isinstance(ai_prompt, str):
        raise InputError('input field "ai_prompt" must be a string')

    return {
        "target": safe_target(target.strip()),
        # max_posts stays None when absent, and that is load-bearing: the
        # collector documents None + a window as "the date boundary is the
        # stopping condition", and substituting a number makes every tier
        # measure itself against a threshold the caller never asked for.
        "max_posts": optional_int("max_posts", 1, 5000),
        "start_date": optional_str("start_date"),
        "end_date": optional_str("end_date"),
        "collect_comments": collect_comments,
        "max_comments_per_post": optional_int("max_comments_per_post", 1, 5000),
        "comment_sort": comment_sort,
        "page_concurrency": optional_int("page_concurrency", 1, 8),
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
        from processors import facebook_comments_processor as comments_processor
        from processors import facebook_page_processor as page_processor
        from ai.claude import analyze_facebook_comments, analyze_page
        from reports.dashboard import generate_dashboard
        from reports.facebook_report import (
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
            comment_analysis = analyze_facebook_comments(comments_data, ai_prompt)
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
            text = format_combined_text_report(page_data, page_analysis,
                                               comment_analysis, comments_data)
            pdf = generate_combined_pdf(page_data, page_analysis, comments_data,
                                        comment_analysis,
                                        output_path=os.path.join(artifacts_dir, f"{ARTIFACT_PREFIX}_report.pdf"))
            dashboard = generate_dashboard(
                page_data, page_analysis, comments_data, comment_analysis,
                output_path=os.path.join(artifacts_dir, f"{ARTIFACT_PREFIX}_dashboard.html"))
        else:
            from reports.facebook_report import format_page_text_report, generate_page_pdf

            text = format_page_text_report(page_data, page_analysis)
            pdf = generate_page_pdf(page_data, page_analysis,
                                    output_path=os.path.join(artifacts_dir, f"{ARTIFACT_PREFIX}_report.pdf"))
            dashboard = generate_dashboard(
                page_data, page_analysis,
                output_path=os.path.join(artifacts_dir, f"{ARTIFACT_PREFIX}_dashboard.html"))

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

    WHY THIS EXISTS: the site rotates `xs` server-side DURING a session, and the
    collector writes the rotated jar back to $FB_DATA_DIR/fb_auth.json. That
    directory is a mkdtemp inside a --rm container, so without this the refresh
    is destroyed when the container exits and every stored session can only get
    staler -- a working account quietly dies after a few weeks of scheduled runs,
    presenting as the site blocking you rather than as a bug.

    REFUSES A LOGGED-OUT JAR. A run that ended on the login wall holds a jar
    missing `xs`; exporting it would overwrite a good stored session with a dead
    one. Same guard the collector's own save_storage_state applies, for the same
    reason -- and the platform checks again on its side, because an Actor is not
    a trusted component.

    Best-effort throughout: a failure here must never fail a scrape that worked.
    """
    out = os.environ.get(SESSION_OUT_ENV, "").strip()
    if not out:
        return False  # the platform did not ask for one

    source = os.path.join(data_dir, "fb_auth.json")
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


def install_session(data_dir: str) -> bool:
    """Materialise the forwarded session as the fb_auth.json the collector reads.

    The collector resolves its session from $FB_DATA_DIR/fb_auth.json, so this
    is the whole integration: write the file, and the existing auth code is
    unchanged. Accepts either a Playwright storage_state document or a plain
    "c_user=...; xs=..." cookie header, because both are things people have.

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
                     "domain": ".facebook.com", "path": "/"}
                )
        if not cookies:
            raise InputError(
                f"{SESSION_ENV} is set but is neither a storage_state document "
                "nor a 'name=value; ...' cookie string"
            )
        state = {"cookies": cookies, "origins": []}

    path = os.path.join(data_dir, "fb_auth.json")
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

    Deliberately not a second Facebook data model: page_information, posts and
    date_window are passed through exactly as the collector built them, so the
    date-window accounting (stop_reason, complete, window_reached, reach) and
    the per-post comment accounting survive to the caller intact. Comments are
    attached to their post rather than left in a parallel list, because the
    platform stores ONE output artifact per run.
    """
    result = {
        "platform": "Facebook",
        "target": posts_doc.get("page_information", {}).get("page_url"),
        "status": posts_doc.get("status"),
        "page_information": posts_doc.get("page_information", {}),
        "posts": posts_doc.get("posts", []),
        "post_count": len(posts_doc.get("posts", [])),
    }
    if posts_doc.get("date_window"):
        result["date_window"] = posts_doc["date_window"]

    if comments_doc is not None:
        result["comment_count"] = attach_comments(result["posts"], comments_doc)
        result["reply_count"] = count_replies(result["posts"])

    tiers = describe_tiers(posts_doc, comments_doc)
    if tiers:
        result["tiers"] = tiers
    return result


def describe_tiers(posts_doc: dict, comments_doc: dict | None) -> dict[str, Any]:
    """WHICH TIER SERVED THIS RUN, in the result rather than only the log.

    The collector's ladder is invisible in a document: on the default path a
    page or group run is browser-free, so the in-page fetch() and DOM tiers are
    skipped BY CONSTRUCTION -- and "the cheap tier carried everything" and "the
    cheap tier came up short and the browser saved it" produce results that look
    identical. Measured 2026-09-22 over 19 container runs: every one was served
    by the SSR bootstrap plus HTTP replay alone, which is worth being able to
    check per run instead of re-deriving from logs.

    Built only from what the collector already records (posts_doc["tiers"],
    each comment record's "comment_tier"), so it states facts rather than
    guesses, and is absent when the collector recorded nothing.
    """
    feed = (posts_doc.get("tiers") or {}).get("served_by") or []
    comment_tiers: list[str] = []
    for record in (comments_doc or {}).get("posts", []) or []:
        tier = (record or {}).get("comment_tier")
        if tier and tier not in comment_tiers:
            comment_tiers.append(tier)
    if not feed and not comment_tiers:
        return {}
    block: dict[str, Any] = {"feed": feed}
    if comments_doc is not None:
        block["comments"] = comment_tiers
    block["browser_used"] = bool(
        {"in_page_fetch", "dom"} & set(feed)) or "navigated" in comment_tiers
    return block


def attach_comments(posts: list[dict], comments_doc: dict) -> int:
    """Attach each post's comment record to it, in place. -> comments attached.

    ONE implementation, used by build_result AND by the batched delivery, so a
    post streamed as part of data#N and the same post in output.json cannot
    disagree about its comments.
    """
    by_url = {r.get("post_url"): r for r in comments_doc.get("posts", []) if r}
    total = 0
    for post in posts:
        record = by_url.get(post.get("post_url"))
        if not record:
            continue
        # The whole record: comments AND their accounting (stop_reason,
        # collection_complete, counts), which is what tells a reader
        # whether 16 comments was the platform's cap or the post's total.
        post["comments"] = record.get("comments", [])
        post["comment_collection"] = {
            key: record[key]
            for key in (
                "requested_comments", "reported_comment_count", "comment_sort",
                "collection_complete", "stop_reason", "replay_stop_cause",
                "comment_window", "comments_in_window", "comments_out_of_window",
            )
            if key in record
        }
        total += len(post["comments"])
    return total


def push_items(collection: str, rows: list[tuple[str | None, Any]]) -> int:
    """Append items to the run's dataset -- the platform's streaming channel.

    Readable through GET /runs/{id}/items while the run goes, which output.json
    is not. Additive: output.json is still written and is still the result.
    NEVER FATAL: a full disk here must not cost the scrape the result it is
    about to write. Same rules as actors/tiktok/actor.py::push_items.
    """
    if not rows:
        return 0
    path = os.path.join(os.path.dirname(os.environ.get("ACTOR_OUTPUT_PATH")
                                        or DEFAULT_OUTPUT_PATH), DATASET_FILE)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            for key, data in rows:
                fh.write(json.dumps({"collection": collection, "key": key,
                                     "data": data}, ensure_ascii=False) + "\n")
    except OSError as exc:
        log(f"could not append {len(rows)} {collection} item(s): {exc}")
        return 0
    return len(rows)


def should_batch(target: str, posts: list[dict]) -> bool:
    """Batch only a GROUP run bigger than one batch; everything else as before."""
    from collectors.facebook.common import is_group_url, resolve_page_url
    return len(posts) > BATCH_SIZE and is_group_url(resolve_page_url(target))


def _post_time(post: dict) -> float:
    """Sort key: the post's UTC timestamp; undated posts sort as oldest."""
    from datetime import datetime  # noqa: PLC0415
    value = post.get("post_date")
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return float("-inf")


def deliver_in_batches(posts: list[dict], collect_comments: bool,
                       comments_for) -> tuple[dict | None, list[dict]]:
    """Collect comments BATCH_SIZE posts at a time and stream each batch.

    -> (the merged comments document, a summary per batch).

    Each batch is complete -- its posts AND their own comments -- before it is
    pushed as "data#N", so what the console shows is never half a batch.
    `comments_for(urls)` is the collector's collect_page_comments bound to this
    run's options; one call per batch is cheap because the comment stage seeds
    every post's feedback_id from the posts file instead of re-walking the feed.

    WHAT THIS DOES NOT DO: split the FEED WALK. collect_page_posts only returns
    when paging has finished, so data#1 appears after the posts stage plus the
    first batch's comments -- minutes, instead of the hours the whole comment
    stage takes. Streaming posts during the feed walk would mean changing the
    collector's paging loop, which this adapter deliberately never touches.

    The comments in each pushed item are attached with attach_comments, the
    same function build_result uses, so the streamed batches and output.json
    agree post for post.
    """
    merged: dict | None = {"posts": []} if collect_comments else None
    summary: list[dict] = []
    # BATCHES ARE CHRONOLOGICAL, NEWEST FIRST - the order the scraper met the
    # posts - and NOT the order collect_page_posts returns them in, which is
    # RANKED BY ENGAGEMENT. Slicing the ranked list was measured live on a
    # 192-post group run: data#1 took the 100 most-engaged posts (87 with
    # comments) and data#2 was the 92-post tail of which not one had a single
    # comment, so the last batch of every big run was a wall of zeros. Sorting a
    # copy leaves output.json's order untouched; the dicts are shared, so the
    # comments attached here still land on output.json's posts too.
    posts = sorted(posts, key=_post_time, reverse=True)
    for start in range(0, len(posts), BATCH_SIZE):
        chunk = posts[start:start + BATCH_SIZE]
        n = start // BATCH_SIZE + 1
        name = BATCH_COLLECTION.format(n=n)
        comment_total = 0
        if collect_comments:
            urls = [p.get("post_url") for p in chunk if p.get("post_url")]
            log(f"{name}: collecting comments for posts "
                f"{start + 1}-{start + len(chunk)} of {len(posts)}")
            doc = comments_for(urls) if urls else {"posts": []}
            merged["posts"].extend((doc or {}).get("posts", []) or [])
            comment_total = attach_comments(chunk, doc or {"posts": []})
        reply_total = count_replies(chunk)
        pushed = push_items(name, [(p.get("post_url"), p) for p in chunk])
        log(f"{name}: {len(chunk)} post(s), {comment_total} comment(s) + "
            f"{reply_total} repl(y/ies) "
            f"{'delivered' if pushed else 'NOT streamed (see above)'}")
        summary.append({"name": name, "posts": len(chunk),
                        "comments": comment_total, "replies": reply_total})
    return merged, summary


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
    `logging` and bare print() ("Hit Facebook's login wall", "Saved 0 post(s)"),
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


def run(settings: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Drive the collector. -> (result, exit code).

    Imported HERE rather than at module scope so that a bad input is reported
    before the collector's import-time work happens.
    """
    from collectors.daterange import parse_window
    from collectors.facebook import (
        AuthError,
        collect_page_comments,
        collect_page_posts,
    )

    try:
        window = parse_window(settings["start_date"], settings["end_date"])
    except ValueError as exc:
        raise InputError(str(exc)) from exc

    log(f"scraping {settings['target']}"
        + (f" over {settings['start_date'] or '...'}..{settings['end_date'] or '...'}" if window else "")
        + (f", up to {settings['max_posts']} post(s)" if settings["max_posts"] else ""))

    # A GROUP target is still collected here, exactly as before - this Actor's
    # session reaches far deeper into a group than an anonymous visitor can, and
    # removing that would take away something that works. It is only WORTH
    # SAYING that there is now a public path, because most callers scraping a
    # group do not need to spend an account's session on it. A log line and
    # nothing else: no refusal, no schema change, no change in behaviour.
    from collectors.facebook.common import is_group_url, resolve_page_url
    if is_group_url(resolve_page_url(settings["target"])):
        log("note: this is a GROUP. The facebook_groups Actor collects public "
            "groups with no account at all; this run will use the connected "
            "session, which reaches further but spends it.")

    try:
        with stdout_to_stderr():
            posts_doc = collect_page_posts(
                settings["target"],
                num_posts=settings["max_posts"],
                window=window,
                concurrency=settings["page_concurrency"],
            )
    except AuthError as exc:
        # A dead or missing session is an ACTOR failure, not a platform one:
        # the platform started this container perfectly well. The collector's
        # own message names login_facebook.py and a path inside this container,
        # neither of which the caller can act on, so it is replaced rather than
        # passed through.
        log(f"the collector reported: {exc}")
        raise InputError(
            f"no usable Facebook session. Set {SESSION_ENV} in the Worker's "
            f"environment and list it in ACTOR_FORWARD_ENV; the value is a "
            f"Playwright storage_state document or a 'c_user=...; xs=...' "
            f"cookie string"
        ) from exc

    status = posts_doc.get("status")
    posts = posts_doc.get("posts", [])
    log(f"posts stage finished: status={status}, {len(posts)} post(s)")

    kwargs: dict[str, Any] = {"window": window}
    if settings["max_comments_per_post"] is not None:
        kwargs["max_comments_per_post"] = settings["max_comments_per_post"]
    if settings["comment_sort"] is not None:
        kwargs["comment_sort"] = settings["comment_sort"]

    def comments_for(urls: list[str]) -> dict:
        with stdout_to_stderr():
            return collect_page_comments(urls, **kwargs)

    comments_doc = None
    batches: list[dict] = []
    if posts and should_batch(settings["target"], posts):
        log(f"big group run: delivering {len(posts)} post(s) in batches of "
            f"{BATCH_SIZE} as data#1..data#{-(-len(posts) // BATCH_SIZE)}")
        comments_doc, batches = deliver_in_batches(
            posts, settings["collect_comments"], comments_for)
    elif settings["collect_comments"] and posts:
        urls = [p.get("post_url") for p in posts if p.get("post_url")]
        log(f"collecting comments from {len(urls)} post(s)")
        comments_doc = comments_for(urls)

    result = build_result(posts_doc, comments_doc)
    if batches:
        result["batches"] = batches

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
    print(f"[facebook] ERROR: {reason}", file=sys.stderr, flush=True)
    result = {"platform": "Facebook", "status": "error", "error": reason,
              "posts": [], "post_count": 0}
    try:
        write_output(output_path, result)
    except OSError as exc:
        print(f"[facebook] could not write the result: {exc}", file=sys.stderr, flush=True)
    print(json.dumps({"status": "error", "error": reason, "post_count": 0}, ensure_ascii=False))
    return EXIT_ACTOR_FAILED


def main() -> int:
    input_path = os.environ.get("ACTOR_INPUT_PATH", DEFAULT_INPUT_PATH)
    output_path = os.environ.get("ACTOR_OUTPUT_PATH", DEFAULT_OUTPUT_PATH)
    run_id = os.environ.get("ACTOR_RUN_ID", "local")

    # EVERY log line to stderr, the collector's included. It logs through the
    # `logging` module, so this one call is what keeps stdout clean enough to
    # carry JSON.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    log(f"run {run_id} starting")

    # The collector writes its own intermediate documents beside its data dir.
    # Point that at scratch space: those files are its internal bootstrap (the
    # comment stage reads the posts file back), not this Actor's result, and
    # the platform owns the run directory.
    workdir = tempfile.mkdtemp(prefix="fb-")
    os.environ["FB_DATA_DIR"] = workdir

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

    # NOT after a login wall. The jar still carries c_user and xs, so the
    # cookie-name guard in export_refreshed_session passes it -- but the run
    # just proved it does not log in. Exporting it overwrote the stored session
    # with the dead one AND, because the platform treats an absorbed session as
    # a positive check, reset the account's auth-failure counter: measured on
    # 2026-09-21, five walled runs in a row across two accounts, every one
    # ending "exported the refreshed session", so neither account was ever
    # flagged as needing a reconnect.
    if result.get("status") == "login_wall":
        log("not exporting the session: the run hit the login wall")
        # The platform's generic "my session is dead" signal. A SOFT reason,
        # not "expired": the wall check reads a rendered page, so the account
        # is flagged only after two walled runs in a row.
        result["session_failure"] = "login_wall"
    else:
        export_refreshed_session(workdir)

    write_output(output_path, result)
    log(f"wrote the result to {output_path}")
    print(json.dumps(summarise(result), ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
