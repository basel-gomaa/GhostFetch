"""facebook_groups Actor - PUBLIC groups, no account, no browser.

DEMO BUILD: the vendored collector returns synthetic data (see
collectors/demo.py in the repository). It makes no network request; the
notes below describe the adapter and the real collector it was built for.

    input JSON  ->  collectors.facebook_groups.collect_group  ->  output.json
                                                             ->  report artifacts

WHAT MAKES THIS ACTOR DIFFERENT FROM THE `facebook` ONE, and why it is a
separate Actor rather than a flag on that one:

  * IT HAS NO SESSION. No SESSION_ENV, no session install, no write-back, no
    cookie of any kind. It collects exactly what a logged-out visitor can see.
    The `facebook` Actor requires a real account's session and reaches far more;
    this one requires nothing and reaches what is public. Neither replaces the
    other, and a target is never silently routed between them.
  * IT REFUSES A PAGE. `collectors.facebook_groups.normalize_group_url` answers
    `not_a_group` for a Page or profile URL before any request is made, so a
    Page can never be scraped here by accident.
  * IT NEVER ATTEMPTS PRIVATE CONTENT. A private group is REPORTED as private -
    Facebook publishes its name and member count, so those are returned - and
    nothing further is tried. No login, no CAPTCHA handling, no fingerprint
    spoofing; if Facebook does not serve something publicly, this Actor says so.

THE LIMITS ARE THE CALLER'S. `max_posts` (1000) and `max_comments_per_post` (20)
are ceilings the caller chooses and can lift - 0 means NO CAP on either, and an
uncapped run is then bounded only by what Facebook serves (measured: its cursor
chain gave out at 349 pages on one group while 2,651 pages of budget went
unused, so the site, not this Actor, is what ends a deep run). They are hard
while they are set, the date range is applied BEFORE the post cap, and matching
posts are taken newest first. A post exposing three public comments yields
three - nothing is chased to reach a limit, and `comment_count` (what Facebook
reports) is kept distinct from `comments_collected` (what it actually served).

MEASURED, anonymously, before this Actor was written: 150 feed pages carried 650
stories spanning three years in 251 s; a post reporting 27 comments served 23
over three pages and then said it was done. So a default 1000-post run is
roughly 235 feed pages, and its comment stage is the expensive half.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
from typing import Any

DEFAULT_INPUT_PATH = "/actor/storage/input.json"
DEFAULT_OUTPUT_PATH = "/actor/storage/output.json"

# The vocabulary the shared report layer switches on. This collector produces
# Facebook-shaped documents (the groups collector emits the same raw shape on
# purpose), so the platform word is Facebook - "shares" are shares here, and the
# `page_type: "group"` in the document is what makes the report say Members and
# Group rather than Followers and Page.
PLATFORM = "Facebook"

# Artifacts are named after THIS Actor, never bare. Six Actors writing
# "report.pdf" into one download folder give a person three files that say
# nothing about which scrape produced them.
ARTIFACT_PREFIX = "facebook_groups"

# Streamed delivery: a finished slice of posts WITH their comments, pushed while
# the run is still going. A default run is an hour of work, and a result that
# only appears at the end reads as a hang.
BATCH_SIZE = 100
DATASET_FILE = "dataset.jsonl"

EXIT_OK = 0
EXIT_ACTOR_FAILED = 1


def log(message: str) -> None:
    """Every line to stderr - stdout carries the result JSON and nothing else."""
    print(f"[facebook_groups] {message}", file=sys.stderr, flush=True)


class InputError(ValueError):
    """The input could not be used. The Actor's own defensive validation."""


# --- input ------------------------------------------------------------------


def read_input(path: str) -> dict:
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

    The API checks input_schema.json before a run is created, but an Actor is
    runnable by hand and through the direct executor path, so it validates its
    own input rather than trusting that it was checked.
    """
    target = payload.get("target")
    if not isinstance(target, str) or not target.strip():
        raise InputError('input field "target" is required and must be a non-empty string')

    def optional_int(name: str, low: int, high: int, default=None,
                     zero_means_unlimited: bool = False):
        value = payload.get(name)
        if value is None:
            return default
        # 0 = NO CAP, for the fields that say so. It reaches the collector as
        # None, which is the repo-wide "no count target" convention rather than
        # a large number standing in for one - a substituted number is
        # indistinguishable downstream from a cap the caller actually asked for.
        if value == 0 and not isinstance(value, bool) and zero_means_unlimited:
            return None
        # bool is an int subclass; True would otherwise pass as 1.
        if isinstance(value, bool) or not isinstance(value, int):
            raise InputError(f'input field "{name}" must be an integer')
        if not low <= value <= high:
            raise InputError(f'input field "{name}" must be between {low} and {high}')
        return value

    def optional_bool(name: str, default: bool) -> bool:
        value = payload.get(name)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise InputError(f'input field "{name}" must be true or false')
        return value

    def optional_str(name: str) -> str | None:
        value = payload.get(name)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise InputError(f'input field "{name}" must be a non-empty string')
        return value.strip()

    # public_only exists so a caller can STATE the intent, and it is refused if
    # set false rather than quietly ignored: this Actor cannot collect private
    # content, and accepting the flag would imply it could.
    public_only = optional_bool("public_only", True)
    if not public_only:
        raise InputError(
            'input field "public_only" cannot be false: this Actor collects '
            "public groups only and has no account to reach anything else")

    ai_prompt = payload.get("ai_prompt")
    if ai_prompt is not None and not isinstance(ai_prompt, str):
        raise InputError('input field "ai_prompt" must be a string')

    return {
        "target": target.strip(),
        # 0 means "no cap" on both: take every post the feed reaches, and every
        # comment it serves. The public comment ceiling still applies - Facebook
        # stops long before any number here does.
        "max_posts": optional_int("max_posts", 0, 1_000_000, default=1000,
                                  zero_means_unlimited=True),
        "max_comments_per_post": optional_int("max_comments_per_post", 0, 100_000,
                                              default=20,
                                              zero_means_unlimited=True),
        "include_replies": optional_bool("include_replies", False),
        "collect_comments": optional_bool("collect_comments", True),
        "public_only": True,
        "start_date": optional_str("start_date"),
        "end_date": optional_str("end_date"),
        "analyse": optional_bool("analyse", True),
        "ai_prompt": (ai_prompt or "").strip(),
    }


# --- streamed delivery ------------------------------------------------------


def push_items(collection: str, rows: list[dict]) -> int:
    """Append items to the run's dataset - the platform's streaming channel.

    Readable through GET /runs/{id}/items while the run goes, which output.json
    cannot be: it only exists once the container has exited. Best effort: a
    delivery problem must not fail a scrape.
    """
    output_path = os.environ.get("ACTOR_OUTPUT_PATH") or DEFAULT_OUTPUT_PATH
    path = os.path.join(os.path.dirname(output_path), DATASET_FILE)
    written = 0
    try:
        with open(path, "a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(
                    {"collection": collection,
                     "key": row.get("post_url") or row.get("post_id"),
                     "data": row}, ensure_ascii=False) + "\n")
                written += 1
    except OSError as exc:
        log(f"could not stream {collection}: {exc}")
        return 0
    return written


# --- analysis ---------------------------------------------------------------


def analyse_and_report(posts_doc: dict, comments_doc: dict | None,
                       ai_prompt: str, artifacts_dir: str) -> dict[str, Any]:
    """Turn the scrape into an executive report. -> a summary of what was made.

    BEST EFFORT, ALWAYS. A scrape is minutes to hours of work and the analysis
    is two API calls; losing the first because the second failed would be
    absurd. Every failure is logged and returned as `analysis.error`, and the
    run still succeeds with its data intact.

    THE ADAPTER CONTAINS NO LOGIC. Reshaping, analysis and rendering are all
    upstream code, vendored verbatim; this only decides the order and where the
    files land.
    """
    summary: dict[str, Any] = {"attempted": True}
    try:
        from processors import facebook_groups_comments_processor as comments_processor
        from processors import facebook_groups_page_processor as page_processor
        from ai.claude import analyze_comments, analyze_page
        from reports.dashboard import generate_dashboard
        from reports.facebook_groups_report import (
            format_combined_text_report,
            generate_combined_pdf,
        )
    except ImportError as exc:
        log(f"analysis unavailable: {exc}")
        return {"attempted": False, "error": f"analysis layer not installed: {exc}"}

    os.makedirs(artifacts_dir, exist_ok=True)

    try:
        # process() is a PURE reshape of the collector's own document, so the
        # documents already in memory are passed straight in.
        page_data = page_processor.process(posts_doc)
        comments_data = (
            comments_processor.process(comments_doc) if comments_doc else {"posts": []}
        )

        log("analysing the group" + (" with your instructions" if ai_prompt else ""))
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

    # Rendering is separate from analysis and fails separately: a report that
    # cannot be drawn must not discard an analysis that succeeded.
    written: list[str] = []
    try:
        if comment_analysis is not None:
            text = format_combined_text_report(page_data, page_analysis,
                                               comment_analysis, comments_data,
                                               platform=PLATFORM)
            pdf = generate_combined_pdf(
                page_data, page_analysis, comments_data, comment_analysis,
                output_path=os.path.join(artifacts_dir, f"{ARTIFACT_PREFIX}_report.pdf"),
                platform=PLATFORM)
            dashboard = generate_dashboard(
                page_data, page_analysis, comments_data, comment_analysis,
                output_path=os.path.join(artifacts_dir, f"{ARTIFACT_PREFIX}_dashboard.html"),
                platform=PLATFORM)
        else:
            from reports.facebook_groups_report import (format_page_text_report,
                                                        generate_page_pdf)

            text = format_page_text_report(page_data, page_analysis, platform=PLATFORM)
            pdf = generate_page_pdf(
                page_data, page_analysis,
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


# --- output -----------------------------------------------------------------


def write_output(path: str, payload: Any) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def summarise(result: dict[str, Any]) -> dict[str, Any]:
    """The compact JSON printed on stdout. The full document goes to the output
    path; echoing all of it here would duplicate it into log.txt."""
    keys = ("status", "source_type", "group_url", "authenticated", "posts_found",
            "posts_collected", "max_posts", "limit_reached", "comments_collected",
            "max_comments_per_post", "include_replies")
    compact = {key: result[key] for key in keys if key in result}
    if result.get("error"):
        compact["error"] = result["error"]
    if isinstance(result.get("analysis"), dict):
        compact["analysis"] = {k: v for k, v in result["analysis"].items()
                               if k in ("attempted", "error", "artifacts")}
    return compact


@contextlib.contextmanager
def stdout_to_stderr():
    """The collector prints; stdout carries JSON. Keep them apart."""
    original = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = original


# --- run --------------------------------------------------------------------


def run(settings: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Drive the collector. -> (result, exit code).

    Imported HERE rather than at module scope so a bad input is reported before
    the collector's import-time work happens.
    """
    from collectors.daterange import parse_window
    from collectors.facebook_groups import FAILURE_STATUSES, collect_group

    try:
        window = parse_window(settings["start_date"], settings["end_date"])
    except ValueError as exc:
        raise InputError(str(exc)) from exc

    log(f"collecting {settings['target']}"
        + (f" over {settings['start_date'] or '...'}..{settings['end_date'] or '...'}" if window else "")
        + (f", up to {settings['max_posts']} post(s)" if settings["max_posts"]
           else ", NO post cap")
        + (", no comments" if not settings["collect_comments"]
           else f" and {settings['max_comments_per_post']} comment(s) each"
           if settings["max_comments_per_post"] else " and every public comment"))

    delivered = {"batches": 0, "items": 0}

    def on_batch(posts):
        """One finished slice - posts WITH their comments - streamed out."""
        delivered["batches"] += 1
        name = f"data#{delivered['batches']}"
        delivered["items"] += push_items(name, posts)
        log(f"{name}: {len(posts)} post(s) delivered while the run continues")

    with stdout_to_stderr():
        group_run = collect_group(
            settings["target"],
            max_posts=settings["max_posts"],
            max_comments_per_post=settings["max_comments_per_post"],
            include_replies=settings["include_replies"],
            collect_comments=settings["collect_comments"],
            window=window,
            progress=log,
            batch_size=BATCH_SIZE,
            on_batch=on_batch,
        )

    result = dict(group_run.document)
    log(f"status={result['status']}, {result['posts_collected']} post(s), "
        f"{result['comments_collected']} comment(s)")

    if settings["analyse"] and group_run.posts_doc and result.get("posts"):
        result["analysis"] = analyse_and_report(
            group_run.posts_doc, group_run.comments_doc, settings["ai_prompt"],
            os.path.join(os.path.dirname(os.environ.get("ACTOR_OUTPUT_PATH")
                                         or DEFAULT_OUTPUT_PATH), "artifacts"))

    if result["status"] in FAILURE_STATUSES:
        log(f"ERROR: {result.get('error') or result['status']}")
        return result, EXIT_ACTOR_FAILED
    return result, EXIT_OK


def _fail(output_path: str, reason: str) -> int:
    """A failed run is still a machine-readable answer, it just carries an error."""
    print(f"[facebook_groups] ERROR: {reason}", file=sys.stderr, flush=True)
    result = {"platform": "Facebook", "source_type": "facebook_group",
              "status": "error", "error": reason, "authenticated": False,
              "posts": [], "posts_found": 0, "posts_collected": 0,
              "comments_collected": 0}
    try:
        write_output(output_path, result)
    except OSError as exc:
        print(f"[facebook_groups] could not write the result: {exc}",
              file=sys.stderr, flush=True)
    print(json.dumps({"status": "error", "error": reason, "posts_collected": 0},
                     ensure_ascii=False))
    return EXIT_ACTOR_FAILED


def main() -> int:
    input_path = os.environ.get("ACTOR_INPUT_PATH", DEFAULT_INPUT_PATH)
    output_path = os.environ.get("ACTOR_OUTPUT_PATH", DEFAULT_OUTPUT_PATH)
    run_id = os.environ.get("ACTOR_RUN_ID", "local")

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # One line per request would otherwise bury the run's own progress: a deep
    # windowed walk issues hundreds of them.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    log(f"run {run_id} starting")

    # Scratch space for the vendored Facebook collector's module-level paths,
    # which are computed at import time whether or not anything writes to them.
    # NOTHING IS INSTALLED INTO IT - there is no session here to write, and
    # writing one would not help: this Actor collects only what is public.
    workdir = tempfile.mkdtemp(prefix="fbg-")
    os.environ["FB_DATA_DIR"] = workdir

    try:
        payload = read_input(input_path)
        settings = parse_settings(payload)
        result, code = run(settings)
    except InputError as exc:
        return _fail(output_path, str(exc))
    except Exception as exc:  # noqa: BLE001 - the Actor reports its own failures
        import traceback
        traceback.print_exc(file=sys.stderr)
        return _fail(output_path, f"{type(exc).__name__}: {exc}")

    write_output(output_path, result)
    log(f"wrote the result to {output_path}")
    print(json.dumps(summarise(result), ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
