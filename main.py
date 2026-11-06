"""
main.py

GhostFetch DEMO BUILD: the collectors this file calls return synthetic data
(see collectors/demo.py), so every command below runs offline. Without an
ANTHROPIC_API_KEY the analysis step uses a canned demo analysis as well.

Orchestrator only. This file wires the modules together in order --
it should never contain platform-specific logic, Claude prompt logic,
or report-formatting logic. If a step's implementation changes, this
file shouldn't have to.

Facebook pipeline:
    main.py -> Facebook Collector -> facebook_posts.json / facebook_comments.json
            -> Facebook Page Processor -> facebook_page_processed.json
            -> Facebook Comments Processor -> facebook_comments_processed.json
            -> claude.py analyze_page() + analyze_facebook_comments()
            -> combined report -> PDF -> Dashboard -> Gmail notification

Instagram pipeline:
    main.py -> Instagram Collector -> instagram_posts_raw.json / instagram_comments_raw.json
            -> Instagram Page Processor -> instagram_page_processed.json
            -> Instagram Comments Processor -> instagram_comments_processed.json
            -> claude.py analyze_page() + analyze_facebook_comments()
            -> combined report -> PDF -> Dashboard -> Gmail notification

Usage:
    python3 main.py                      # interactive menu (asks what to do)
    python3 main.py facebook --page <name-or-url> --posts 50 \
        --post-url "https://www.facebook.com/..." [--comments 1000] [--no-email]
    python3 main.py instagram --profile <username-or-url> --posts 50 \
        [--comments 1000] [--no-email]
    python3 main.py instagram --hashtag <tag> --posts 50 \
        [--comments 1000] [--no-email]
    python3 main.py x --profile <handle-or-url> --posts 50 \
        [--comments 1000] [--no-email]
    python3 main.py x --post-url "https://x.com/<handle>/status/<id>" [--comments 1000]
    python3 main.py youtube [--channel <channel_id>]
"""

import os
import sys

# Always run inside the project's virtualenv, no matter which interpreter
# launched us - `python3 main.py` without activating .venv would otherwise
# fail with ModuleNotFoundError (playwright, anthropic, ...).
_VENV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv")
_VENV_PY = os.path.join(_VENV_DIR, "bin", "python")
if (os.path.exists(_VENV_PY)
        and os.path.realpath(sys.prefix) != os.path.realpath(_VENV_DIR)):
    os.execv(_VENV_PY, [_VENV_PY] + sys.argv)

import argparse
import json
import logging
import re
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# YouTube defaults (prototype placeholders)
CHANNEL_ID = "UCG-48Ki-b6W_siaUkukJOSw"  # Google Developers
VIDEO_COUNT = 5
COMMENTS_PER_VIDEO = 20


# ---------------------------------------------------------------------------
# Facebook pipeline
# ---------------------------------------------------------------------------

# URL path segments that are content types, not page usernames.
_NON_USERNAME_SEGMENTS = {
    "watch", "reel", "reels", "share", "posts", "photo", "photos", "video",
    "videos", "story.php", "permalink.php", "photo.php", "video.php", "groups",
}


def _fb_username(url) -> str | None:
    """Extract the page username (or numeric profile id) from a Facebook URL.

    A GROUP has no username - "groups" is a path segment, not an identity - so
    this used to return None for every group URL, which silently switched OFF
    the staleness check below: a posts file and a comments file from two
    different groups compared as (None, None) and never reported a mismatch.
    Groups get their own namespaced identity instead.
    """
    if not url:
        return None
    group = re.search(r"facebook\.com/groups/([^/?#]+)", str(url))
    if group:
        return f"groups/{group.group(1).lower()}"
    match = re.search(
        r"facebook\.com/(?:profile\.php\?id=(\d+)|([A-Za-z0-9.\-_]+))", str(url)
    )
    if not match:
        return None
    username = (match.group(1) or match.group(2)).lower()
    return None if username in _NON_USERNAME_SEGMENTS else username


def normalize_group_target(value: str) -> str:
    """Accept a group id, a vanity name, or a full URL - always yield a group URL.

    Canonical implementation now lives in ``collectors.facebook.common`` (shared
    with the groups service); this delegates so there is a single source of truth.
    """
    from collectors.facebook.common import normalize_group_target as _canonical
    return _canonical(value)


def facebook_data_sources() -> tuple[str | None, str | None]:
    """
    Which page do the raw data files on disk belong to?
    Returns (page_scrape_username, comments_scrape_username); either may be
    None when the file is missing or the URL shape hides the username.
    """
    from collectors.facebook import POSTS_OUTPUT_FILE, COMMENTS_OUTPUT_FILE

    page_user = comments_user = None
    try:
        with open(POSTS_OUTPUT_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        page_user = _fb_username((raw.get("page_information") or {}).get("page_url"))
    except (OSError, json.JSONDecodeError):
        pass
    try:
        with open(COMMENTS_OUTPUT_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        posts = raw.get("posts") or ([raw["post"]] if raw.get("post") else [])
        first = posts[0] if posts else {}
        comments_user = _fb_username(first.get("resolved_url") or first.get("post_url"))
    except (OSError, json.JSONDecodeError):
        pass
    return page_user, comments_user


def facebook_data_mismatch() -> tuple[str, str] | None:
    """Return (page_user, comments_user) when the two datasets disagree."""
    page_user, comments_user = facebook_data_sources()
    if page_user and comments_user and page_user != comments_user:
        return page_user, comments_user
    return None


def _save_segmented(page_data: dict, comments_data: dict) -> None:
    """Persist the label-enriched processed docs, best effort.

    Re-running the processors would overwrite these labels, which is correct -
    they belong to the analysis, not the scrape - so this only rewrites what is
    already on disk and never fails the report if it cannot.
    """
    from processors import facebook_page_processor, facebook_comments_processor
    for module, data in ((facebook_page_processor, page_data),
                         (facebook_comments_processor, comments_data)):
        try:
            module.save_json(module.OUTPUT_FILE, data)
        except OSError as exc:
            logger.warning("Could not write segmented data to %s (%s)",
                           module.OUTPUT_FILE, exc)


def generate_facebook_reports(send_email: bool = True, segment: bool = True):
    """
    Claude analysis + combined report + PDF + dashboard + Gmail, from the
    processed JSON files currently in data/facebook/. Both the page scrape
    and the comment scrape must have been run (at some point) first.

    ``segment`` additionally labels every post and comment with a topic and a
    sentiment (ai/segmentation.py). It costs a few extra Claude calls and never
    fails the run - a failed segmentation just leaves the section out.
    """
    from processors import facebook_page_processor, facebook_comments_processor
    from ai.claude import analyze_page, analyze_facebook_comments
    from reports.facebook_report import format_facebook_text_report, generate_facebook_pdf
    from reports.dashboard import generate_dashboard
    from notifications.gmail import send_report_email, gmail_is_configured

    try:
        with open(facebook_page_processor.OUTPUT_FILE, encoding="utf-8") as f:
            page_data = json.load(f)
        with open(facebook_comments_processor.OUTPUT_FILE, encoding="utf-8") as f:
            comments_data = json.load(f)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Both processed files are needed for the combined report - run "
            "public page scraping AND comment scraping first. Missing: "
            f"{exc.filename}"
        ) from exc

    mismatch = facebook_data_mismatch()
    if mismatch:
        page_user, comments_user = mismatch
        raise RuntimeError(
            f"Data mismatch: the scraped posts are from page '{page_user}' but "
            f"the scraped comments are from a post on '{comments_user}'. "
            "Refusing to build a report that mixes two different pages - "
            "scrape comments from one of this page's posts (or re-scrape the "
            "page that matches the comments) and try again."
        )

    logger.info("Claude analysis - page performance")
    page_analysis = analyze_page(page_data)

    logger.info("Claude analysis - comments/audience")
    comment_analysis = analyze_facebook_comments(comments_data)

    segmentation = None
    if segment:
        from ai.segmentation import segment as segment_items, apply_labels
        logger.info("Claude analysis - topic/sentiment segmentation")
        segmentation = segment_items(page_data, comments_data)
        # Written back onto the processed docs so the per-item labels are on
        # disk too, not only in the rendered report.
        apply_labels(page_data, comments_data, segmentation)
        if segmentation:
            _save_segmented(page_data, comments_data)

    logger.info("Building combined report")
    report_text = format_facebook_text_report(page_data, page_analysis,
                                              comment_analysis, comments_data,
                                              segmentation=segmentation)

    logger.info("Generating PDF report")
    pdf_path = generate_facebook_pdf(page_data, page_analysis, comments_data,
                                     comment_analysis, segmentation=segmentation)
    logger.info("PDF written to %s", pdf_path.resolve())

    logger.info("Generating dashboard")
    dashboard_path = generate_dashboard(page_data, page_analysis, comments_data,
                                        comment_analysis, segmentation=segmentation)
    logger.info("Dashboard written to %s", dashboard_path.resolve())

    if send_email:
        if gmail_is_configured():
            logger.info("Sending Gmail notification")
            send_report_email(
                subject=f"Facebook report: {page_data.get('page_name')} "
                        f"({page_analysis.performance_rating} / {comment_analysis.overall_sentiment})",
                body=(
                    "Your Facebook intelligence report is ready.\n\n"
                    + report_text
                    + "\n\nThe PDF report and dashboard are attached."
                ),
                attachments=[pdf_path, dashboard_path],
            )
        else:
            logger.warning("Gmail not configured - skipping email "
                           "(set GMAIL_ADDRESS / GMAIL_APP_PASSWORD in .env).")

    return report_text, pdf_path, dashboard_path


def run_facebook_page_scrape(page_name: str, num_posts=None, window=None) -> dict:
    """Public page scraping: collector + page processor.

    ``num_posts`` is None when no count was requested - see collect_page_posts.
    """
    from collectors.facebook import collect_page_posts
    from processors import facebook_page_processor

    logger.info("Collecting %s from %s",
                f"every post in {window.describe()}" if num_posts is None
                else f"up to {num_posts} post(s)", page_name)
    raw = collect_page_posts(page_name, num_posts, window=window)

    status = raw.get("status")
    if status == "login_wall":
        _facebook_auth_failure("expired")
    kind = (raw.get("page_information") or {}).get("page_type") or "page"
    if status == "page_unavailable":
        raise RuntimeError(
            f"Facebook says the {kind} '{page_name}' isn't available. Check the "
            "spelling - use the exact username from the page's URL "
            "(facebook.com/<username>, no spaces), or paste the full URL."
        )
    dw = raw.get("date_window") or {}
    window_note = dw.get("requested")
    posts = raw.get("posts") or []

    if window_note:
        # A windowed run is judged by WHETHER IT REACHED THE WINDOW, never by the
        # post count and never as fetch_failed (only genuine load failures above
        # are that). window_reached answers "did paging get back past the window
        # start?" directly (see WindowResult.window_reached), which a post count
        # cannot: measured, a July window on one group returned 4 posts and an
        # August one 1,795, both page_budget - the four were older stragglers
        # interleaved from further back, not a sample of July.
        window_reached = dw.get("window_reached")
        stop_reason = dw.get("stop_reason")

        # PARTIAL, a different cause: Facebook THROTTLED the run (HTTP 200 with
        # "Rate limit exceeded", code 1675004) before paging reached the window.
        # The advice is the opposite of Test C's: the range is fine, the timing
        # was not.
        if stop_reason == "rate_limited" and window_reached is False:
            reach = (dw.get("reach_oldest") or "an unknown date")[:10]
            print(
                f"\nPARTIAL: Facebook rate-limited this run before it reached "
                f"{window_note} at '{page_name}'.\n"
                f"  stop_reason  = rate_limited\n"
                f"  window_reached = false\n"
                f"  {dw.get('scanned_pages')} page(s) scanned, paging got back to "
                f"{reach} before Facebook answered \"Rate limit exceeded\" and kept "
                f"answering it after the waits.\n"
                f"  The range is fine: wait a while (tens of minutes) and run it again."
            )
            sys.exit(EXIT_PARTIAL)

        # Test C - PARTIAL: ran out of budget before reaching the window.
        if stop_reason == "page_budget" and window_reached is False:
            reach = (dw.get("reach_oldest") or "an unknown date")[:10]
            print(
                f"\nPARTIAL: ran out of paging budget before reaching "
                f"{window_note} at '{page_name}'.\n"
                f"  stop_reason  = page_budget\n"
                f"  window_reached = false\n"
                f"  {kind} loaded fine; {dw.get('scanned_pages')} page(s) scanned, "
                f"paging only got back to {reach}.\n"
                f"  The {len(posts)} post(s) collected are older items interleaved "
                f"into a newer feed, not a sample of the range.\n"
                f"  This window is further back than this feed can be paged in one "
                f"run: use a MORE RECENT range, or raise "
                f"the collector page budget."
            )
            sys.exit(EXIT_PARTIAL)

        # Test B - SUCCESS but empty: the window was genuinely covered (or the
        # feed exhausted before it, proving nothing that old exists) and simply
        # holds no posts. There is nothing to analyse, so leave cleanly at exit 0
        # rather than raising - this is NOT a failure and NOT fetch_failed.
        if not posts:
            print(
                f"\nSUCCESS: no posts at '{page_name}' within {window_note}.\n"
                f"  status = no_posts_found\n"
                f"  window_reached = "
                f"{'true' if window_reached is not False else 'true (source exhausted before window)'}\n"
                f"  The {kind} loaded fine and the window is simply empty - "
                f"nothing to report."
            )
            sys.exit(0)
        # Window reached AND posts present -> normal processing below.
    elif not posts:
        # No window and nothing found: almost always a name that didn't lead to a
        # real page/group. Distinct from the windowed empty case above.
        raise RuntimeError(
            f"Logged in fine, but no posts were found at '{page_name}'. "
            f"This usually means the name didn't lead to a real {kind} - use the "
            "username from the page's URL or paste the full URL."
        )

    logger.info("Processing page/post data")
    return facebook_page_processor.run()


def run_facebook_comment_scrape(post_url: str, max_comments: int,
                                comment_sort: str = "relevant",
                                window=None) -> dict:
    """Comment scraping for ONE post: collector + comments processor."""
    from collectors.facebook import collect_post_comments
    from processors import facebook_comments_processor

    logger.info("Collecting up to %d %s comment(s) from post %s%s",
                max_comments, comment_sort, post_url,
                f" within {window.describe()}" if window else "")
    raw = collect_post_comments(post_url, max_comments,
                                comment_sort=comment_sort, window=window)
    _guard_comment_login_wall(raw)

    logger.info("Processing comments data")
    return facebook_comments_processor.run()


def generate_facebook_page_report():
    """Page-only report: Claude analysis + text + PDF + dashboard."""
    from processors import facebook_page_processor
    from ai.claude import analyze_page
    from reports.facebook_report import format_page_text_report, generate_page_pdf
    from reports.dashboard import generate_dashboard

    with open(facebook_page_processor.OUTPUT_FILE, encoding="utf-8") as f:
        page_data = json.load(f)

    logger.info("Claude analysis - page performance")
    page_analysis = analyze_page(page_data)
    report_text = format_page_text_report(page_data, page_analysis)
    pdf_path = generate_page_pdf(page_data, page_analysis)
    dashboard_path = generate_dashboard(page_data, page_analysis)
    return report_text, pdf_path, dashboard_path


def generate_facebook_comments_report():
    """Comments-only report (one post): Claude analysis + text + PDF."""
    from processors import facebook_comments_processor
    from ai.claude import analyze_facebook_comments
    from reports.facebook_report import format_comments_text_report, generate_comments_pdf

    with open(facebook_comments_processor.OUTPUT_FILE, encoding="utf-8") as f:
        comments_data = json.load(f)

    logger.info("Claude analysis - comments/audience")
    comment_analysis = analyze_facebook_comments(comments_data)
    report_text = format_comments_text_report(comments_data, comment_analysis)
    pdf_path = generate_comments_pdf(comments_data, comment_analysis)
    return report_text, pdf_path


# Exit code for "the session is gone and a human has to log in again", kept
# distinct from a crash (1) so a scheduler can react to it differently - the
# whole point of running this unattended.
EXIT_AUTH = 2

# Exit code for "the run was PARTIAL": a windowed page/group scrape ran out of
# paging budget before reaching the requested window. Distinct from a crash (1),
# a dead session (2), and success (0) so a scheduler can tell "widen/retry the
# range" apart from "the request failed". It is explicitly NOT fetch_failed.
EXIT_PARTIAL = 3


def _facebook_auth_failure(reason: str, detail: str = "") -> None:
    """Report a dead Facebook session and leave with EXIT_AUTH.

    Mirrors _threads_guard / _x_guard in exiting at the CLI boundary rather
    than raising, so an unattended run gets one clear line instead of a
    traceback. Also e-mails, when Gmail is configured, because an autonomous
    run nobody is watching is exactly the case where a silent stop is useless.
    """
    message = {
        "missing": ("No data/facebook/fb_auth.json - run "
                    "the login script (not in the demo build) to save a session."),
        "incomplete": ("data/facebook/fb_auth.json has no c_user/xs cookies - "
                       "re-run the login script (not in the demo build)."),
        "expired": ("The saved Facebook session has expired - re-run "
                    "the login script (not in the demo build) to log in again."),
    }.get(reason, detail or "Facebook authentication failed.")
    # A known reason still gets the caller's context appended - "the session
    # expired" alone does not say WHERE, and the comment stage's wall reads very
    # differently from the pre-flight's.
    if detail and reason in ("missing", "incomplete", "expired"):
        message = f"{message} ({detail})"
    try:
        from notifications.gmail import gmail_is_configured, send_report_email
        if gmail_is_configured():
            send_report_email(
                subject="[social-ai-pipeline] Facebook session needs attention",
                body=(f"{message}\n\nThe scheduled Facebook run stopped before "
                      "scraping, so no report was produced and no Claude "
                      "credits were spent."),
            )
    except Exception as exc:                            # noqa: BLE001
        # Alerting must never be the thing that breaks the run - same rule the
        # report e-mail step follows.
        logger.warning("Could not send the auth alert e-mail: %s", exc)
    # sys.exit(<int>) prints NOTHING, so say it first - an unattended run that
    # exits 2 in silence tells whoever reads the log afterwards nothing at all.
    print(message, file=sys.stderr)
    sys.exit(EXIT_AUTH)


def run_facebook_batch_comment_scrape(post_urls: list, per_post: int,
                                      comment_sort: str = "relevant",
                                      window=None) -> dict:
    """Comment scraping for EVERY post just scraped: collector + processor.

    The whole-target comment pass. It existed only inside the interactive menu,
    which is why the CLI could not scrape a page's or group's comments at all -
    `--post-url` was required and did one post.
    """
    from collectors.facebook import collect_page_comments
    from processors import facebook_comments_processor

    logger.info("Collecting %d %s comment(s) from each of %d post(s)%s",
                per_post, comment_sort, len(post_urls),
                f" within {window.describe()}" if window else "")
    raw = collect_page_comments(post_urls, per_post, comment_sort=comment_sort,
                                window=window)
    _guard_comment_login_wall(raw)

    logger.info("Processing comments data")
    return facebook_comments_processor.run()


def _guard_comment_login_wall(raw: dict) -> None:
    """Stop a fully login-walled comment pass BEFORE any Claude call.

    The posts stage already exits EXIT_AUTH on `login_wall`; the comment stage
    checked nothing, so a walled run returned N posts with 0 comments, exited 0,
    spent two Claude analyses plus segmentation on an empty document and wrote a
    `Nposts_0comments` report. Measured: 314 posts, every record walled.

    Deliberately requires EVERY record to be walled. Facebook walls
    intermittently, and aborting on a single bounced post would turn a mostly
    good run into a hard failure - the same fail-open discipline
    auth.preflight() uses.
    """
    # A batch scrape stores a list under "posts"; a single-post scrape stores
    # one record under "post" (builder.build_comments_output). Read both, or the
    # --post-url path silently keeps its pre-guard behaviour.
    raw = raw or {}
    records = raw.get("posts")
    if records is None:
        records = [raw["post"]] if raw.get("post") else []
    if records and all(r.get("status") == "login_wall" for r in records):
        _facebook_auth_failure(
            "expired",
            f"every one of the {len(records)} post(s) hit the login wall "
            "during the comment pass, so no comments were collected.")


def resolve_comment_sort(comment_sort, window=None) -> str:
    """Which comment sort a run should actually use.

    ``comment_sort`` is None when nobody chose one - that is the ONLY case this
    may override. Comparing against DEFAULT_COMMENT_SORT instead cannot tell
    `--comment-sort relevant` from an omitted flag, so an explicitly requested
    sort was silently switched out from under the caller.

    A date window changes the right default. "Most relevant" is Facebook's own
    ranking and is NOT date-ordered, so against a window it can only ever
    SAMPLE - it will hand back whichever relevant comments happen to fall in
    range and stop. "Newest" walks the connection in time order, so it is the
    only sort that can actually COVER a window.
    """
    from collectors.facebook.pagination import (COMMENT_SORT_NEWEST,
                                                DEFAULT_COMMENT_SORT)
    if comment_sort:
        return comment_sort
    if window:
        logger.info("Date window active and no --comment-sort given - using "
                    "'newest', the only sort that can cover a date range "
                    "('relevant' is not date-ordered and would only sample it)")
        return COMMENT_SORT_NEWEST
    return DEFAULT_COMMENT_SORT


def run_facebook_pipeline(page_name: str, num_posts=None, post_url: str | None = None,
                          max_comments: int = 1000, send_email: bool = True,
                          window=None, comment_sort=None,
                          segment: bool = True):
    """Full pipeline: both scrapes, then the combined report chain.

    ``post_url`` scrapes comments from that ONE post. Omit it and every post the
    feed scrape returned gets its comments collected instead, which is what a
    whole-page or whole-group run wants.
    """
    # Check the session BEFORE the browser work and, more importantly, before
    # the two Claude calls - a dead session should cost seconds and no credits.
    from collectors.facebook.auth import preflight
    ok, reason = preflight()
    if not ok:
        _facebook_auth_failure(reason)
    logger.info("Facebook session pre-flight: %s", reason)

    comment_sort = resolve_comment_sort(comment_sort, window)
    page_data = run_facebook_page_scrape(page_name, num_posts, window=window)
    if post_url:
        run_facebook_comment_scrape(post_url, max_comments, comment_sort,
                                    window=window)
    else:
        post_urls = [p.get("post_url") for p in page_data.get("posts") or []]
        run_facebook_batch_comment_scrape(post_urls, max_comments, comment_sort,
                                          window=window)
    return generate_facebook_reports(send_email=send_email, segment=segment)


# ---------------------------------------------------------------------------
# Instagram pipeline
#
# Same chain as Facebook: collector -> raw JSON -> processor -> processed JSON
# -> Claude -> report. The processors emit the Facebook schema, so the Claude
# analysis and the report/dashboard layer are reused as-is (only the platform
# label differs).
# ---------------------------------------------------------------------------

PLATFORM_IG = "Instagram"


def run_instagram_page_scrape(profile: str, num_posts: int, window=None) -> dict:
    """Profile post scraping: collector + page processor."""
    from collectors.instagram import collect_profile_posts
    from processors import instagram_page_processor

    logger.info("Collecting %s post(s) from @%s", _count_note(num_posts), profile)
    raw = collect_profile_posts(profile, num_posts, window=window)

    status = raw.get("status")
    if status == "login_wall":
        raise RuntimeError(
            "Instagram showed a login wall - the saved session in "
            "data/instagram/ig_auth.json is missing or has expired. "
            "Re-run the login script (not in the demo build) to log in again."
        )
    if status == "private_profile":
        raise RuntimeError(
            f"'{profile}' is a private account - its posts cannot be scraped. "
            "Use a public profile, or one the logged-in account follows."
        )
    if status == "profile_unavailable":
        raise RuntimeError(
            f"Instagram says the profile '{profile}' isn't available. Check the "
            "spelling - use the exact username from the profile URL "
            "(instagram.com/<username>)."
        )
    if not raw.get("posts"):
        raise RuntimeError(
            f"Logged in fine, but no posts were found for '{profile}'. The "
            "account may have no posts, or Instagram did not return its media."
        )

    logger.info("Processing profile/post data")
    return instagram_page_processor.run()


def run_instagram_hashtag_scrape(hashtag: str, num_posts: int, window=None) -> dict:
    """Hashtag post scraping: collector + page processor.

    Emits the same processed shape as the profile scrape (the processor is
    reused as-is), so the report/dashboard chain is identical - only the
    scrape target is a hashtag rather than one profile.
    """
    from collectors.instagram import collect_hashtag_posts
    from processors import instagram_page_processor

    logger.info("Collecting %s post(s) for #%s", _count_note(num_posts), hashtag)
    raw = collect_hashtag_posts(hashtag, num_posts, window=window)

    status = raw.get("status")
    if status == "login_wall":
        raise RuntimeError(
            "Instagram showed a login wall - the saved session in "
            "data/instagram/ig_auth.json is missing or has expired. "
            "Re-run the login script (not in the demo build) to log in again."
        )
    if status == "hashtag_unavailable":
        raise RuntimeError(
            f"Instagram says the hashtag '#{hashtag}' isn't available. Check the "
            "spelling - enter the tag with or without the leading '#'."
        )
    if not raw.get("posts"):
        raise RuntimeError(
            f"Logged in fine, but no posts were found for '#{hashtag}'. The "
            "hashtag may have no public posts, or Instagram did not return them."
        )

    logger.info("Processing hashtag/post data")
    return instagram_page_processor.run()


def run_instagram_comment_scrape(post_url: str, max_comments: int) -> dict:
    """Comment scraping for ONE post: collector + comments processor."""
    from collectors.instagram import collect_post_comments
    from processors import instagram_comments_processor

    logger.info("Collecting %d comment(s) from post %s", max_comments, post_url)
    collect_post_comments(post_url, max_comments)

    logger.info("Processing comments data")
    return instagram_comments_processor.run()


def generate_instagram_page_report():
    """Page-only report: Claude analysis + text + PDF + dashboard."""
    from processors import instagram_page_processor
    from ai.claude import analyze_page
    from reports.facebook_report import format_page_text_report, generate_page_pdf
    from reports.dashboard import generate_dashboard

    with open(instagram_page_processor.OUTPUT_FILE, encoding="utf-8") as f:
        page_data = json.load(f)

    logger.info("Claude analysis - profile performance")
    page_analysis = analyze_page(page_data)
    report_text = format_page_text_report(page_data, page_analysis, platform=PLATFORM_IG)
    pdf_path = generate_page_pdf(page_data, page_analysis, platform=PLATFORM_IG)
    dashboard_path = generate_dashboard(page_data, page_analysis, platform=PLATFORM_IG)
    return report_text, pdf_path, dashboard_path


def generate_instagram_comments_report():
    """Comments-only report (one post): Claude analysis + text + PDF."""
    from processors import instagram_comments_processor
    from ai.claude import analyze_facebook_comments
    from reports.facebook_report import format_comments_text_report, generate_comments_pdf

    with open(instagram_comments_processor.OUTPUT_FILE, encoding="utf-8") as f:
        comments_data = json.load(f)

    logger.info("Claude analysis - comments/audience")
    comment_analysis = analyze_facebook_comments(comments_data)
    report_text = format_comments_text_report(comments_data, comment_analysis,
                                              platform=PLATFORM_IG)
    pdf_path = generate_comments_pdf(comments_data, comment_analysis,
                                     platform=PLATFORM_IG)
    return report_text, pdf_path


def generate_instagram_reports(send_email: bool = True):
    """Combined report from the processed profile + comments files on disk."""
    from processors import instagram_page_processor, instagram_comments_processor
    from ai.claude import analyze_page, analyze_facebook_comments
    from reports.facebook_report import format_facebook_text_report, generate_facebook_pdf
    from reports.dashboard import generate_dashboard
    from notifications.gmail import send_report_email, gmail_is_configured

    try:
        with open(instagram_page_processor.OUTPUT_FILE, encoding="utf-8") as f:
            page_data = json.load(f)
        with open(instagram_comments_processor.OUTPUT_FILE, encoding="utf-8") as f:
            comments_data = json.load(f)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Both processed files are needed for the combined report - run the "
            f"Instagram profile scrape first. Missing: {exc.filename}"
        ) from exc

    logger.info("Claude analysis - profile performance")
    page_analysis = analyze_page(page_data)

    logger.info("Claude analysis - comments/audience")
    comment_analysis = analyze_facebook_comments(comments_data)

    logger.info("Building combined report")
    report_text = format_facebook_text_report(page_data, page_analysis,
                                              comment_analysis, comments_data,
                                              platform=PLATFORM_IG)

    logger.info("Generating PDF report")
    pdf_path = generate_facebook_pdf(page_data, page_analysis, comments_data,
                                     comment_analysis, platform=PLATFORM_IG)
    logger.info("PDF written to %s", pdf_path.resolve())

    logger.info("Generating dashboard")
    dashboard_path = generate_dashboard(page_data, page_analysis, comments_data,
                                        comment_analysis, platform=PLATFORM_IG)
    logger.info("Dashboard written to %s", dashboard_path.resolve())

    if send_email:
        if gmail_is_configured():
            logger.info("Sending Gmail notification")
            send_report_email(
                subject=f"Instagram report: {page_data.get('page_name')} "
                        f"({page_analysis.performance_rating} / {comment_analysis.overall_sentiment})",
                body=(
                    "Your Instagram intelligence report is ready.\n\n"
                    + report_text
                    + "\n\nThe PDF report and dashboard are attached."
                ),
                attachments=[pdf_path, dashboard_path],
            )
        else:
            logger.warning("Gmail not configured - skipping email "
                           "(set GMAIL_ADDRESS / GMAIL_APP_PASSWORD in .env).")

    return report_text, pdf_path, dashboard_path


def _instagram_comments_and_report(page_data: dict, comments_per_post: int,
                                   send_email: bool):
    """Shared tail: comments from each scraped post, then the combined report.

    Used by both the profile and hashtag pipelines - once the posts are on
    disk, the rest of the chain is identical regardless of how they were found.
    """
    from collectors.instagram import collect_profile_comments
    from processors import instagram_comments_processor

    post_urls = [p.get("post_url") for p in page_data.get("posts") or []]
    collect_profile_comments(post_urls, comments_per_post)
    instagram_comments_processor.run()
    return generate_instagram_reports(send_email=send_email)


def run_instagram_pipeline(profile: str, num_posts: int, comments_per_post: int = 1000,
                           send_email: bool = True,
                           window=None):
    """Full pipeline: profile scrape + comments from each post, then reports."""
    page_data = run_instagram_page_scrape(profile, num_posts, window=window)
    return _instagram_comments_and_report(page_data, comments_per_post, send_email)


def run_instagram_hashtag_pipeline(hashtag: str, num_posts: int,
                                   comments_per_post: int = 1000,
                                   send_email: bool = True,
                                   window=None):
    """Full pipeline: hashtag scrape + comments from each post, then reports."""
    page_data = run_instagram_hashtag_scrape(hashtag, num_posts, window=window)
    return _instagram_comments_and_report(page_data, comments_per_post, send_email)


# ---------------------------------------------------------------------------
# TikTok pipeline
#
# Same chain as Instagram: collector -> raw JSON -> processor -> processed JSON
# -> Claude -> report. The processors emit the Facebook schema, so the Claude
# analysis and the report/dashboard layer are reused as-is. TikTok has real
# shares (unlike Instagram's reposts), so the default "shares" wording applies.
# ---------------------------------------------------------------------------

PLATFORM_TT = "TikTok"


def _tiktok_reliability_guard(raw: dict) -> None:
    """Raise when TikTok REFUSED the run, rather than letting it read as "no posts".

    The three statuses below come from the collector's reliability layer and
    are new only in the sense that there was previously no way to say them: a
    verification page, a rate limit and a block all used to arrive as
    `no_posts_found`, i.e. as "this account has nothing", which sends whoever
    reads it looking in the wrong place entirely.

    Nothing here inspects a page or a session - it maps a status the collector
    already decided onto a message and an exception, which is what an
    orchestrator does.
    """
    status = raw.get("status")
    if status not in ("challenge", "rate_limited", "blocked", "robots_disallowed",
                      "robots_unavailable"):
        return
    detail = (raw.get("reliability") or {}).get("message") or ""
    advice = {
        "challenge": (
            "TikTok is showing a verification challenge (CAPTCHA). Nothing was "
            "scraped, and it was NOT retried - retrying a challenge is what "
            "turns a flagged session into a blocked one. Re-run headed "
            "(TIKTOK_HEADLESS=0) and solve it once; the solved session is "
            "saved, or attach to your own Chrome with TIKTOK_CDP=1."
        ),
        "rate_limited": (
            "TikTok rate-limited this session. The run backed off and retried "
            "before giving up. Wait a while before trying again, and scrape "
            "fewer posts per run."
        ),
        "blocked": (
            "TikTok refused the request (HTTP 403 and no content). This is "
            "usually the IP rather than the session - a datacenter/VPS address "
            "gets refused even with a good login. Run from a residential "
            "connection, or attach to your own Chrome with TIKTOK_CDP=1."
        ),
        "robots_disallowed": (
            "TikTok's robots.txt disallows this target, so nothing was "
            "requested. This is the collector's own rule rather than a refusal "
            "by TikTok - no session or IP change will alter it. Scrape a "
            "profile, video or tag URL instead (a search URL is disallowed), "
            "or set TIKTOK_ROBOTS=0 if you have a reason to override it."
        ),
        "robots_unavailable": (
            "TikTok's robots.txt could not be read (after retries) and no copy "
            "from the last 7 days is cached, so the collector cannot tell "
            "whether these paths are permitted and did not ask. Usually a "
            "network problem: try again shortly. TIKTOK_ROBOTS=0 overrides it "
            "if you have a reason to."
        ),
    }[status]
    raise RuntimeError(advice + (f" ({detail})" if detail else ""))


def run_tiktok_page_scrape(profile: str, num_posts: int, window=None) -> dict:
    """Profile post scraping: collector + page processor."""
    from collectors.tiktok import collect_profile_posts
    from processors import tiktok_page_processor

    logger.info("Collecting %s post(s) from @%s", _count_note(num_posts), profile)
    raw = collect_profile_posts(profile, num_posts, window=window)

    status = raw.get("status")
    if status == "login_wall":
        raise RuntimeError(
            "TikTok showed a login wall - the saved session in "
            "data/tiktok/tiktok_auth.json is missing or has expired. "
            "Re-run the login script (not in the demo build) to log in again."
        )
    if status == "private_profile":
        raise RuntimeError(
            f"'{profile}' is a private account - its posts cannot be scraped. "
            "Use a public profile, or one the logged-in account follows."
        )
    if status == "profile_unavailable":
        raise RuntimeError(
            f"TikTok says the profile '{profile}' isn't available. Check the "
            "spelling - use the exact username from the profile URL "
            "(tiktok.com/@<username>)."
        )
    _tiktok_reliability_guard(raw)
    if not raw.get("posts"):
        raise RuntimeError(
            f"Logged in fine, but no posts were found for '{profile}'. The "
            "account may have no posts, or TikTok did not return its media."
        )

    logger.info("Processing profile/post data")
    return tiktok_page_processor.run()


def run_tiktok_hashtag_scrape(hashtag: str, num_posts: int, window=None) -> dict:
    """Hashtag post scraping: collector + page processor.

    Emits the same processed shape as the profile scrape (the processor is
    reused as-is), so the report/dashboard chain is identical - only the
    scrape target is a hashtag rather than one profile.
    """
    from collectors.tiktok import collect_hashtag_posts
    from processors import tiktok_page_processor

    logger.info("Collecting %s post(s) for #%s", _count_note(num_posts), hashtag)
    raw = collect_hashtag_posts(hashtag, num_posts, window=window)

    status = raw.get("status")
    if status == "login_wall":
        raise RuntimeError(
            "TikTok showed a login wall - the saved session in "
            "data/tiktok/tiktok_auth.json is missing or has expired. "
            "Re-run the login script (not in the demo build) to log in again."
        )
    if status == "hashtag_unavailable":
        raise RuntimeError(
            f"TikTok says the hashtag '#{hashtag}' isn't available. Check the "
            "spelling - enter the tag with or without the leading '#'."
        )
    _tiktok_reliability_guard(raw)
    if not raw.get("posts"):
        raise RuntimeError(
            f"Logged in fine, but no posts were found for '#{hashtag}'. The "
            "hashtag may have no public posts, or TikTok did not return them."
        )

    logger.info("Processing hashtag/post data")
    return tiktok_page_processor.run()


def run_tiktok_comment_scrape(video_url: str, max_comments: int) -> dict:
    """Comment scraping for ONE video: collector + comments processor."""
    from collectors.tiktok import collect_post_comments
    from processors import tiktok_comments_processor

    logger.info("Collecting %d comment(s) from video %s", max_comments, video_url)
    collect_post_comments(video_url, max_comments)

    logger.info("Processing comments data")
    return tiktok_comments_processor.run()


def generate_tiktok_page_report():
    """Page-only report: Claude analysis + text + PDF + dashboard."""
    from processors import tiktok_page_processor
    from ai.claude import analyze_page
    from reports.facebook_report import format_page_text_report, generate_page_pdf
    from reports.dashboard import generate_dashboard

    with open(tiktok_page_processor.OUTPUT_FILE, encoding="utf-8") as f:
        page_data = json.load(f)

    logger.info("Claude analysis - profile performance")
    page_analysis = analyze_page(page_data)
    report_text = format_page_text_report(page_data, page_analysis, platform=PLATFORM_TT)
    pdf_path = generate_page_pdf(page_data, page_analysis, platform=PLATFORM_TT)
    dashboard_path = generate_dashboard(page_data, page_analysis, platform=PLATFORM_TT)
    return report_text, pdf_path, dashboard_path


def generate_tiktok_comments_report():
    """Comments-only report (one video): Claude analysis + text + PDF."""
    from processors import tiktok_comments_processor
    from ai.claude import analyze_facebook_comments
    from reports.facebook_report import format_comments_text_report, generate_comments_pdf

    with open(tiktok_comments_processor.OUTPUT_FILE, encoding="utf-8") as f:
        comments_data = json.load(f)

    logger.info("Claude analysis - comments/audience")
    comment_analysis = analyze_facebook_comments(comments_data)
    report_text = format_comments_text_report(comments_data, comment_analysis,
                                              platform=PLATFORM_TT)
    pdf_path = generate_comments_pdf(comments_data, comment_analysis,
                                     platform=PLATFORM_TT)
    return report_text, pdf_path


def generate_tiktok_reports(send_email: bool = True):
    """Combined report from the processed profile + comments files on disk."""
    from processors import tiktok_page_processor, tiktok_comments_processor
    from ai.claude import analyze_page, analyze_facebook_comments
    from reports.facebook_report import format_facebook_text_report, generate_facebook_pdf
    from reports.dashboard import generate_dashboard
    from notifications.gmail import send_report_email, gmail_is_configured

    try:
        with open(tiktok_page_processor.OUTPUT_FILE, encoding="utf-8") as f:
            page_data = json.load(f)
        with open(tiktok_comments_processor.OUTPUT_FILE, encoding="utf-8") as f:
            comments_data = json.load(f)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Both processed files are needed for the combined report - run the "
            f"TikTok profile scrape first. Missing: {exc.filename}"
        ) from exc

    logger.info("Claude analysis - profile performance")
    page_analysis = analyze_page(page_data)

    logger.info("Claude analysis - comments/audience")
    comment_analysis = analyze_facebook_comments(comments_data)

    logger.info("Building combined report")
    report_text = format_facebook_text_report(page_data, page_analysis,
                                              comment_analysis, comments_data,
                                              platform=PLATFORM_TT)

    logger.info("Generating PDF report")
    pdf_path = generate_facebook_pdf(page_data, page_analysis, comments_data,
                                     comment_analysis, platform=PLATFORM_TT)
    logger.info("PDF written to %s", pdf_path.resolve())

    logger.info("Generating dashboard")
    dashboard_path = generate_dashboard(page_data, page_analysis, comments_data,
                                        comment_analysis, platform=PLATFORM_TT)
    logger.info("Dashboard written to %s", dashboard_path.resolve())

    if send_email:
        if gmail_is_configured():
            logger.info("Sending Gmail notification")
            send_report_email(
                subject=f"TikTok report: {page_data.get('page_name')} "
                        f"({page_analysis.performance_rating} / {comment_analysis.overall_sentiment})",
                body=(
                    "Your TikTok intelligence report is ready.\n\n"
                    + report_text
                    + "\n\nThe PDF report and dashboard are attached."
                ),
                attachments=[pdf_path, dashboard_path],
            )
        else:
            logger.warning("Gmail not configured - skipping email "
                           "(set GMAIL_ADDRESS / GMAIL_APP_PASSWORD in .env).")

    return report_text, pdf_path, dashboard_path


def _tiktok_comments_and_report(page_data: dict, comments_per_post: int,
                                send_email: bool):
    """Shared tail: comments from each scraped video, then the combined report.

    Used by both the profile and hashtag pipelines - once the posts are on
    disk, the rest of the chain is identical regardless of how they were found.

    The WORK LIST comes from the collector's own pipeline stage rather than
    being rebuilt here: this used to compute it one way while the Actor
    computed it another, which is the drift the staged pipeline exists to end.
    """
    from collectors.tiktok import collect_profile_comments
    from collectors.tiktok.pipeline import worklist
    from processors import tiktok_comments_processor

    video_urls, stage = worklist({"posts": page_data.get("posts") or []})
    logger.info("TikTok work list: %s", stage.as_dict())
    collect_profile_comments(video_urls, comments_per_post)
    tiktok_comments_processor.run()
    return generate_tiktok_reports(send_email=send_email)


def run_tiktok_pipeline(profile: str, num_posts: int, comments_per_post: int = 1000,
                        send_email: bool = True, window=None):
    """Full pipeline: profile scrape + comments from each video, then reports."""
    page_data = run_tiktok_page_scrape(profile, num_posts, window=window)
    return _tiktok_comments_and_report(page_data, comments_per_post, send_email)


def run_tiktok_hashtag_pipeline(hashtag: str, num_posts: int,
                                comments_per_post: int = 1000,
                                send_email: bool = True, window=None):
    """Full pipeline: hashtag scrape + comments from each video, then reports."""
    page_data = run_tiktok_hashtag_scrape(hashtag, num_posts, window=window)
    return _tiktok_comments_and_report(page_data, comments_per_post, send_email)


# ---------------------------------------------------------------------------
# LinkedIn pipeline (company / profile / hashtag). The LinkedIn processors emit
# the same shape as the Facebook ones, so the Claude analysis and the
# report/dashboard layer are reused as-is. LinkedIn has real shares, so the
# default "shares" wording applies.
# ---------------------------------------------------------------------------

PLATFORM_LI = "LinkedIn"


def _linkedin_has_session() -> bool:
    """Does linkedin_auth.json still carry an li_at cookie?"""
    from collectors.linkedin.common import STORAGE_STATE_FILE
    try:
        with open(STORAGE_STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return False
    return any(c.get("name") == "li_at" and c.get("value")
               for c in state.get("cookies") or [])


def _linkedin_authwall_message() -> str:
    """Advice that matches which failure actually happened.

    An auth wall with NO saved cookie means log in again. An auth wall with a
    perfectly good cookie means LinkedIn bounced a valid session - telling that
    user to re-login sends them to fix something that is not broken."""
    if not _linkedin_has_session():
        return (
            "LinkedIn showed an auth wall and data/linkedin/linkedin_auth.json "
            "has no li_at cookie - the session really is missing. Run "
            "the login script (not in the demo build) to log in again (or "
            "the cookie-paste script (not in the demo build) to paste li_at + JSESSIONID)."
        )
    return (
        "LinkedIn showed an auth wall on every attempt, but the saved session in "
        "data/linkedin/linkedin_auth.json still has an li_at cookie - so this is "
        "most likely LinkedIn throttling this client, not an expired login. "
        "Wait a few minutes and re-run; if it persists, run under LINKEDIN_CDP=1 "
        "against your own Chrome. Only re-run the login script (not in the demo build) if it "
        "keeps failing after that."
    )


def _linkedin_scrape(raw, target, unavailable_status, unavailable_msg):
    """Shared status handling for the three LinkedIn feed scrapes."""
    from processors import linkedin_page_processor

    status = raw.get("status")
    if status == "authwall":
        raise RuntimeError(_linkedin_authwall_message())
    if status == unavailable_status:
        raise RuntimeError(unavailable_msg)
    if not raw.get("posts"):
        raise RuntimeError(
            f"Logged in fine, but no posts were found for '{target}'. It may "
            "have no public posts, or LinkedIn did not return them."
        )

    logger.info("Processing LinkedIn post data")
    return linkedin_page_processor.run()


def _count_note(num_posts):
    """"up to 50" / "the date window" - num_posts is None when only a date
    range was given (see daterange.resolve_count), and "%d" on None makes
    logging print a traceback instead of the line."""
    return f"up to {num_posts}" if num_posts is not None else "the date window"


def run_linkedin_company_scrape(company: str, num_posts: int, window=None) -> dict:
    """Company post scraping: collector + page processor."""
    from collectors.linkedin import collect_company_posts

    logger.info("Collecting %s post(s) from company %s", _count_note(num_posts), company)
    raw = collect_company_posts(company, num_posts, window=window)
    return _linkedin_scrape(
        raw, company, "profile_unavailable",
        f"LinkedIn says the company '{company}' isn't available. Use the exact "
        "name from the /company/<name>/ URL.")


def run_linkedin_profile_scrape(profile: str, num_posts: int, window=None) -> dict:
    """Profile (person) post scraping: collector + page processor."""
    from collectors.linkedin import collect_profile_posts

    logger.info("Collecting %s post(s) from profile %s", _count_note(num_posts), profile)
    raw = collect_profile_posts(profile, num_posts, window=window)
    return _linkedin_scrape(
        raw, profile, "profile_unavailable",
        f"LinkedIn says the profile '{profile}' isn't available. Use the exact "
        "vanity name from the /in/<name>/ URL.")


def run_linkedin_hashtag_scrape(hashtag: str, num_posts: int, window=None) -> dict:
    """Hashtag post scraping: collector + page processor."""
    from collectors.linkedin import collect_hashtag_posts

    logger.info("Collecting %s post(s) for #%s", _count_note(num_posts), hashtag)
    raw = collect_hashtag_posts(hashtag, num_posts, window=window)
    return _linkedin_scrape(
        raw, hashtag, "hashtag_unavailable",
        f"LinkedIn says the hashtag '#{hashtag}' isn't available. Check the "
        "spelling - enter the tag with or without the leading '#'.")


def run_linkedin_comment_scrape(post_url: str, max_comments: int) -> dict:
    """Comment scraping for ONE post: collector + comments processor."""
    from collectors.linkedin import collect_post_comments
    from processors import linkedin_comments_processor

    logger.info("Collecting %d comment(s) from post %s", max_comments, post_url)
    collect_post_comments(post_url, max_comments)

    logger.info("Processing comments data")
    return linkedin_comments_processor.run()


def generate_linkedin_page_report():
    """Page-only report: Claude analysis + text + PDF + dashboard."""
    from processors import linkedin_page_processor
    from ai.claude import analyze_page
    from reports.facebook_report import format_page_text_report, generate_page_pdf
    from reports.dashboard import generate_dashboard

    with open(linkedin_page_processor.OUTPUT_FILE, encoding="utf-8") as f:
        page_data = json.load(f)

    logger.info("Claude analysis - page performance")
    page_analysis = analyze_page(page_data)
    report_text = format_page_text_report(page_data, page_analysis, platform=PLATFORM_LI)
    pdf_path = generate_page_pdf(page_data, page_analysis, platform=PLATFORM_LI)
    dashboard_path = generate_dashboard(page_data, page_analysis, platform=PLATFORM_LI)
    return report_text, pdf_path, dashboard_path


def generate_linkedin_comments_report():
    """Comments-only report (one post): Claude analysis + text + PDF."""
    from processors import linkedin_comments_processor
    from ai.claude import analyze_facebook_comments
    from reports.facebook_report import format_comments_text_report, generate_comments_pdf

    with open(linkedin_comments_processor.OUTPUT_FILE, encoding="utf-8") as f:
        comments_data = json.load(f)

    logger.info("Claude analysis - comments/audience")
    comment_analysis = analyze_facebook_comments(comments_data)
    report_text = format_comments_text_report(comments_data, comment_analysis,
                                              platform=PLATFORM_LI)
    pdf_path = generate_comments_pdf(comments_data, comment_analysis,
                                     platform=PLATFORM_LI)
    return report_text, pdf_path


def generate_linkedin_reports(send_email: bool = True):
    """Combined report from the processed page + comments files on disk."""
    from processors import linkedin_page_processor, linkedin_comments_processor
    from ai.claude import analyze_page, analyze_facebook_comments
    from reports.facebook_report import format_facebook_text_report, generate_facebook_pdf
    from reports.dashboard import generate_dashboard
    from notifications.gmail import send_report_email, gmail_is_configured

    try:
        with open(linkedin_page_processor.OUTPUT_FILE, encoding="utf-8") as f:
            page_data = json.load(f)
        with open(linkedin_comments_processor.OUTPUT_FILE, encoding="utf-8") as f:
            comments_data = json.load(f)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Both processed files are needed for the combined report - run the "
            f"LinkedIn page scrape first. Missing: {exc.filename}"
        ) from exc

    logger.info("Claude analysis - page performance")
    page_analysis = analyze_page(page_data)

    logger.info("Claude analysis - comments/audience")
    comment_analysis = analyze_facebook_comments(comments_data)

    logger.info("Building combined report")
    report_text = format_facebook_text_report(page_data, page_analysis,
                                              comment_analysis, comments_data,
                                              platform=PLATFORM_LI)

    logger.info("Generating PDF report")
    pdf_path = generate_facebook_pdf(page_data, page_analysis, comments_data,
                                     comment_analysis, platform=PLATFORM_LI)
    logger.info("PDF written to %s", pdf_path.resolve())

    logger.info("Generating dashboard")
    dashboard_path = generate_dashboard(page_data, page_analysis, comments_data,
                                        comment_analysis, platform=PLATFORM_LI)
    logger.info("Dashboard written to %s", dashboard_path.resolve())

    if send_email:
        if gmail_is_configured():
            logger.info("Sending Gmail notification")
            send_report_email(
                subject=f"LinkedIn report: {page_data.get('page_name')} "
                        f"({page_analysis.performance_rating} / {comment_analysis.overall_sentiment})",
                body=(
                    "Your LinkedIn intelligence report is ready.\n\n"
                    + report_text
                    + "\n\nThe PDF report and dashboard are attached."
                ),
                attachments=[pdf_path, dashboard_path],
            )
        else:
            logger.warning("Gmail not configured - skipping email "
                           "(set GMAIL_ADDRESS / GMAIL_APP_PASSWORD in .env).")

    return report_text, pdf_path, dashboard_path


def _linkedin_comments_and_report(page_data: dict, comments_per_post: int,
                                  send_email: bool, deep_comments: bool = False):
    """Shared tail: comments from each scraped post, then the combined report."""
    from collectors.linkedin import collect_feed_comments
    from processors import linkedin_comments_processor

    post_urls = [p.get("post_url") for p in page_data.get("posts") or []]
    collect_feed_comments(post_urls, comments_per_post, deep=deep_comments)
    linkedin_comments_processor.run()
    return generate_linkedin_reports(send_email=send_email)


def run_linkedin_company_pipeline(company: str, num_posts: int,
                                  comments_per_post: int = 1000,
                                  send_email: bool = True,
                                  deep_comments: bool = False,
                                  window=None):
    """Full pipeline: company scrape + comments from each post, then reports."""
    page_data = run_linkedin_company_scrape(company, num_posts, window=window)
    return _linkedin_comments_and_report(page_data, comments_per_post, send_email,
                                         deep_comments)


def run_linkedin_profile_pipeline(profile: str, num_posts: int,
                                  comments_per_post: int = 1000,
                                  send_email: bool = True,
                                  deep_comments: bool = False,
                                  window=None):
    """Full pipeline: profile scrape + comments from each post, then reports."""
    page_data = run_linkedin_profile_scrape(profile, num_posts, window=window)
    return _linkedin_comments_and_report(page_data, comments_per_post, send_email,
                                         deep_comments)


def run_linkedin_hashtag_pipeline(hashtag: str, num_posts: int,
                                  comments_per_post: int = 1000,
                                  send_email: bool = True,
                                  deep_comments: bool = False,
                                  window=None):
    """Full pipeline: hashtag scrape + comments from each post, then reports."""
    page_data = run_linkedin_hashtag_scrape(hashtag, num_posts, window=window)
    return _linkedin_comments_and_report(page_data, comments_per_post, send_email,
                                         deep_comments)


# ---------------------------------------------------------------------------
# X (Twitter) pipeline (profile / single post). The X processors emit the same
# shape as the Facebook ones, so the Claude analysis and the report/dashboard
# layer are reused as-is. X reposts are a real share metric, so the default
# "shares" wording applies.
# ---------------------------------------------------------------------------

PLATFORM_X = "X"


def _x_has_session() -> bool:
    """Does x_auth.json still carry an auth_token cookie?"""
    from collectors.x.common import STORAGE_STATE_FILE
    try:
        with open(STORAGE_STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return False
    return any(c.get("name") == "auth_token" and c.get("value")
               for c in state.get("cookies") or [])


def _x_login_wall_message() -> str:
    """Advice that matches which failure actually happened.

    X serves a logged-out timeline (5 posts, no cursor) rather than an error
    when it does not accept a session, so "login_wall" here means the session
    was ignored - which is a different fix depending on whether one exists."""
    if not _x_has_session():
        return (
            "X served the logged-out timeline and data/x/x_auth.json has no "
            "auth_token cookie - the session really is missing. Run "
            "the login script (not in the demo build) to log in (or the cookie-paste script (not in the demo build) to "
            "paste auth_token + ct0)."
        )
    return (
        "X served the logged-out timeline (a 5-post cap with no pagination "
        "cursor), but data/x/x_auth.json still has an auth_token cookie - so "
        "the saved session was rejected rather than missing. It has most likely "
        "expired; re-run the login script (not in the demo build) to refresh it."
    )


def _x_scrape(raw, target):
    """Shared status handling for the X feed scrape."""
    from processors import x_page_processor

    status = raw.get("status")
    if status == "login_wall":
        raise RuntimeError(_x_login_wall_message())
    if status == "profile_unavailable":
        raise RuntimeError(
            f"X says the profile '{target}' isn't available. Check the handle "
            "(the name in the x.com/<handle> URL); it may be suspended or renamed.")
    if not raw.get("posts"):
        raise RuntimeError(
            f"Logged in fine, but no posts were found for '{target}'. The "
            "account may have no public posts, or X did not return them."
        )

    logger.info("Processing X post data")
    return x_page_processor.run()


def _x_guard(call):
    """Run an X collector call, turning AuthError into a clean CLI exit.

    SystemExit belongs HERE, at the CLI boundary - never inside collectors/x/,
    where a SystemExit raised in a worker thread would die silently and be
    indistinguishable from X genuinely returning nothing.
    """
    from collectors.x import AuthError
    try:
        return call()
    except AuthError as exc:
        sys.exit(str(exc))


def run_x_profile_scrape(profile: str, num_posts: int, window=None) -> dict:
    """Profile post scraping: collector + page processor."""
    from collectors.x import collect_profile_posts

    logger.info("Collecting %s post(s) from X profile %s", _count_note(num_posts), profile)
    raw = _x_guard(lambda: collect_profile_posts(profile, num_posts, window=window))
    return _x_scrape(raw, profile)


def run_x_hashtag_scrape(hashtag: str, num_posts: int, window=None) -> dict:
    """Hashtag/search scraping: collector + page processor.

    Unlike the profile scrape this opens a browser - X's search endpoint needs
    a signed header only its own page can mint (see collectors/x/search.py)."""
    from collectors.x import collect_hashtag_posts

    logger.info("Collecting %s post(s) for X search %s", _count_note(num_posts), hashtag)
    raw = _x_guard(lambda: collect_hashtag_posts(hashtag, num_posts, window=window))
    return _x_scrape(raw, hashtag)


def run_x_comment_scrape(post_url: str, max_comments: int) -> dict:
    """Reply scraping for ONE post: collector + comments processor."""
    from collectors.x import collect_post_comments
    from processors import x_comments_processor

    logger.info("Collecting %d repl(y/ies) from post %s", max_comments, post_url)
    _x_guard(lambda: collect_post_comments(post_url, max_comments))

    logger.info("Processing comments data")
    return x_comments_processor.run()


def generate_x_page_report():
    """Page-only report: Claude analysis + text + PDF + dashboard."""
    from processors import x_page_processor
    from ai.claude import analyze_page
    from reports.facebook_report import format_page_text_report, generate_page_pdf
    from reports.dashboard import generate_dashboard

    with open(x_page_processor.OUTPUT_FILE, encoding="utf-8") as f:
        page_data = json.load(f)

    logger.info("Claude analysis - page performance")
    page_analysis = analyze_page(page_data)
    report_text = format_page_text_report(page_data, page_analysis, platform=PLATFORM_X)
    pdf_path = generate_page_pdf(page_data, page_analysis, platform=PLATFORM_X)
    dashboard_path = generate_dashboard(page_data, page_analysis, platform=PLATFORM_X)
    return report_text, pdf_path, dashboard_path


def generate_x_comments_report():
    """Comments-only report (one post): Claude analysis + text + PDF."""
    from processors import x_comments_processor
    from ai.claude import analyze_facebook_comments
    from reports.facebook_report import format_comments_text_report, generate_comments_pdf

    with open(x_comments_processor.OUTPUT_FILE, encoding="utf-8") as f:
        comments_data = json.load(f)

    logger.info("Claude analysis - comments/audience")
    comment_analysis = analyze_facebook_comments(comments_data)
    report_text = format_comments_text_report(comments_data, comment_analysis,
                                              platform=PLATFORM_X)
    pdf_path = generate_comments_pdf(comments_data, comment_analysis,
                                     platform=PLATFORM_X)
    return report_text, pdf_path


def generate_x_reports(send_email: bool = True):
    """Combined report from the processed page + comments files on disk."""
    from processors import x_page_processor, x_comments_processor
    from ai.claude import analyze_page, analyze_facebook_comments
    from reports.facebook_report import format_facebook_text_report, generate_facebook_pdf
    from reports.dashboard import generate_dashboard
    from notifications.gmail import send_report_email, gmail_is_configured

    try:
        with open(x_page_processor.OUTPUT_FILE, encoding="utf-8") as f:
            page_data = json.load(f)
        with open(x_comments_processor.OUTPUT_FILE, encoding="utf-8") as f:
            comments_data = json.load(f)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Both processed files are needed for the combined report - run the "
            f"X profile scrape first. Missing: {exc.filename}"
        ) from exc

    logger.info("Claude analysis - page performance")
    page_analysis = analyze_page(page_data)

    logger.info("Claude analysis - comments/audience")
    comment_analysis = analyze_facebook_comments(comments_data)

    logger.info("Building combined report")
    report_text = format_facebook_text_report(page_data, page_analysis,
                                              comment_analysis, comments_data,
                                              platform=PLATFORM_X)

    logger.info("Generating PDF report")
    pdf_path = generate_facebook_pdf(page_data, page_analysis, comments_data,
                                     comment_analysis, platform=PLATFORM_X)
    logger.info("PDF written to %s", pdf_path.resolve())

    logger.info("Generating dashboard")
    dashboard_path = generate_dashboard(page_data, page_analysis, comments_data,
                                        comment_analysis, platform=PLATFORM_X)
    logger.info("Dashboard written to %s", dashboard_path.resolve())

    if send_email:
        if gmail_is_configured():
            logger.info("Sending Gmail notification")
            send_report_email(
                subject=f"X report: {page_data.get('page_name')} "
                        f"({page_analysis.performance_rating} / {comment_analysis.overall_sentiment})",
                body=(
                    "Your X intelligence report is ready.\n\n"
                    + report_text
                    + "\n\nThe PDF report and dashboard are attached."
                ),
                attachments=[pdf_path, dashboard_path],
            )
        else:
            logger.warning("Gmail not configured - skipping email "
                           "(set GMAIL_ADDRESS / GMAIL_APP_PASSWORD in .env).")

    return report_text, pdf_path, dashboard_path


def _x_comments_and_report(page_data: dict, comments_per_post: int,
                           send_email: bool):
    """Shared tail: replies from each scraped post, then the combined report."""
    from collectors.x import collect_feed_comments
    from processors import x_comments_processor

    posts = page_data.get("posts") or []
    _x_guard(lambda: collect_feed_comments(posts, comments_per_post))
    x_comments_processor.run()
    return generate_x_reports(send_email=send_email)


def run_x_profile_pipeline(profile: str, num_posts: int,
                           comments_per_post: int = 1000,
                           send_email: bool = True,
                           window=None):
    """Full pipeline: profile scrape + replies from each post, then reports."""
    page_data = run_x_profile_scrape(profile, num_posts, window=window)
    return _x_comments_and_report(page_data, comments_per_post, send_email)


def run_x_hashtag_pipeline(hashtag: str, num_posts: int,
                           comments_per_post: int = 1000,
                           send_email: bool = True,
                           window=None):
    """Full pipeline: hashtag scrape + replies from each post, then reports."""
    page_data = run_x_hashtag_scrape(hashtag, num_posts, window=window)
    return _x_comments_and_report(page_data, comments_per_post, send_email)


# ---------------------------------------------------------------------------
# Threads pipeline (profile / tag / single post). The Threads processors emit
# the same shape as the Facebook ones, so the Claude analysis and the
# report/dashboard layer are reused as-is. Threads reposts are a real share
# metric, so the default "shares" wording applies.
#
# Unlike every other platform here, Threads needs NO authentication at all -
# there is no threads_auth.json, no login script and no session to expire. So
# there is no _threads_has_session / login-wall helper: the failure modes are
# "this profile does not exist" and "Threads is throttling us", nothing else.
# ---------------------------------------------------------------------------

PLATFORM_THREADS = "Threads"


def _threads_guard(call):
    """Run a Threads collector call, turning its typed errors into clean exits.

    SystemExit belongs HERE, at the CLI boundary - never inside
    collectors/threads/, which is library code.
    """
    from collectors.threads.auth import AuthError
    from collectors.threads.http_replay import Challenged, RateLimited
    try:
        return call()
    except (AuthError, Challenged, RateLimited) as exc:
        sys.exit(str(exc))


def _threads_scrape(raw, target):
    """Shared status handling for the Threads feed scrape."""
    from processors import threads_page_processor

    status = raw.get("status")
    if status == "profile_unavailable":
        raise RuntimeError(
            f"Threads says '{target}' isn't available. Check the username (the "
            "name in the threads.com/@<username> URL); it may not exist, or the "
            "account may be private.")
    if status == "rate_limited":
        raise RuntimeError(
            "Threads rate-limited this run before it collected anything. It "
            "answers HTTP 200 with an error body rather than a 429, so this is "
            "easy to mistake for an empty account. Wait a few minutes and "
            "re-run, or lower --posts.")
    if not raw.get("posts"):
        raise RuntimeError(
            f"No posts were found for '{target}'. The account may have no "
            "public posts, or the date range may exclude all of them.")

    logger.info("Processing Threads post data")
    return threads_page_processor.run()


def run_threads_profile_scrape(profile: str, num_posts: int, window=None) -> dict:
    """Profile post scraping: collector + page processor."""
    from collectors.threads import collect_profile_posts

    logger.info("Collecting %s post(s) from Threads profile %s",
                _count_note(num_posts), profile)
    raw = _threads_guard(lambda: collect_profile_posts(profile, num_posts,
                                                       window=window))
    return _threads_scrape(raw, profile)


def run_threads_tag_scrape(tag: str, num_posts: int, window=None) -> dict:
    """Tag/topic scraping: collector + page processor.

    Threads serves the tag feed ONE-SHOT - it carries no pagination cursor at
    all - so this returns whatever the public feed rendered (measured 16-19
    posts) regardless of --posts, and the raw document records that ceiling.
    """
    from collectors.threads import collect_tag_posts

    logger.info("Collecting %s post(s) for Threads tag %s", _count_note(num_posts), tag)
    raw = _threads_guard(lambda: collect_tag_posts(tag, num_posts, window=window))
    return _threads_scrape(raw, tag)


def run_threads_comment_scrape(post_url: str, max_comments: int) -> dict:
    """Reply scraping for ONE post: collector + comments processor."""
    from collectors.threads import collect_post_comments
    from processors import threads_comments_processor

    logger.info("Collecting %d repl(y/ies) from post %s", max_comments, post_url)
    _threads_guard(lambda: collect_post_comments(post_url, max_comments))

    logger.info("Processing comments data")
    return threads_comments_processor.run()


def generate_threads_page_report():
    """Page-only report: Claude analysis + text + PDF + dashboard."""
    from processors import threads_page_processor
    from ai.claude import analyze_page
    from reports.facebook_report import format_page_text_report, generate_page_pdf
    from reports.dashboard import generate_dashboard

    with open(threads_page_processor.OUTPUT_FILE, encoding="utf-8") as f:
        page_data = json.load(f)

    logger.info("Claude analysis - page performance")
    page_analysis = analyze_page(page_data)
    report_text = format_page_text_report(page_data, page_analysis,
                                          platform=PLATFORM_THREADS)
    pdf_path = generate_page_pdf(page_data, page_analysis, platform=PLATFORM_THREADS)
    dashboard_path = generate_dashboard(page_data, page_analysis,
                                        platform=PLATFORM_THREADS)
    return report_text, pdf_path, dashboard_path


def generate_threads_comments_report():
    """Comments-only report (one post): Claude analysis + text + PDF."""
    from processors import threads_comments_processor
    from ai.claude import analyze_facebook_comments
    from reports.facebook_report import format_comments_text_report, generate_comments_pdf

    with open(threads_comments_processor.OUTPUT_FILE, encoding="utf-8") as f:
        comments_data = json.load(f)

    logger.info("Claude analysis - comments/audience")
    comment_analysis = analyze_facebook_comments(comments_data)
    report_text = format_comments_text_report(comments_data, comment_analysis,
                                              platform=PLATFORM_THREADS)
    pdf_path = generate_comments_pdf(comments_data, comment_analysis,
                                     platform=PLATFORM_THREADS)
    return report_text, pdf_path


def generate_threads_reports(send_email: bool = True):
    """Combined report from the processed page + comments files on disk."""
    from processors import threads_page_processor, threads_comments_processor
    from ai.claude import analyze_page, analyze_facebook_comments
    from reports.facebook_report import format_facebook_text_report, generate_facebook_pdf
    from reports.dashboard import generate_dashboard
    from notifications.gmail import send_report_email, gmail_is_configured

    try:
        with open(threads_page_processor.OUTPUT_FILE, encoding="utf-8") as f:
            page_data = json.load(f)
        with open(threads_comments_processor.OUTPUT_FILE, encoding="utf-8") as f:
            comments_data = json.load(f)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Both processed files are needed for the combined report - run the "
            f"Threads profile scrape first. Missing: {exc.filename}"
        ) from exc

    logger.info("Claude analysis - page performance")
    page_analysis = analyze_page(page_data)

    logger.info("Claude analysis - comments/audience")
    comment_analysis = analyze_facebook_comments(comments_data)

    logger.info("Building combined report")
    report_text = format_facebook_text_report(page_data, page_analysis,
                                              comment_analysis, comments_data,
                                              platform=PLATFORM_THREADS)

    logger.info("Generating PDF report")
    pdf_path = generate_facebook_pdf(page_data, page_analysis, comments_data,
                                     comment_analysis, platform=PLATFORM_THREADS)
    logger.info("PDF written to %s", pdf_path.resolve())

    logger.info("Generating dashboard")
    dashboard_path = generate_dashboard(page_data, page_analysis, comments_data,
                                        comment_analysis, platform=PLATFORM_THREADS)
    logger.info("Dashboard written to %s", dashboard_path.resolve())

    if send_email:
        if gmail_is_configured():
            logger.info("Sending Gmail notification")
            send_report_email(
                subject=f"Threads report: {page_data.get('page_name')} "
                        f"({page_analysis.performance_rating} / {comment_analysis.overall_sentiment})",
                body=(
                    "Your Threads intelligence report is ready.\n\n"
                    + report_text
                    + "\n\nThe PDF report and dashboard are attached."
                ),
                attachments=[pdf_path, dashboard_path],
            )
        else:
            logger.warning("Gmail not configured - skipping email "
                           "(set GMAIL_ADDRESS / GMAIL_APP_PASSWORD in .env).")

    return report_text, pdf_path, dashboard_path


def _threads_comments_and_report(page_data: dict, comments_per_post: int,
                                 send_email: bool):
    """Shared tail: replies from each scraped post, then the combined report."""
    from collectors.threads import collect_feed_comments
    from processors import threads_comments_processor

    posts = page_data.get("posts") or []
    _threads_guard(lambda: collect_feed_comments(posts, comments_per_post))
    threads_comments_processor.run()
    return generate_threads_reports(send_email=send_email)


def run_threads_profile_pipeline(profile: str, num_posts: int,
                                 comments_per_post: int = 1000,
                                 send_email: bool = True,
                                 window=None):
    """Full pipeline: profile scrape + replies from each post, then reports."""
    page_data = run_threads_profile_scrape(profile, num_posts, window=window)
    return _threads_comments_and_report(page_data, comments_per_post, send_email)


def run_threads_tag_pipeline(tag: str, num_posts: int,
                             comments_per_post: int = 1000,
                             send_email: bool = True,
                             window=None):
    """Full pipeline: tag scrape + replies from each post, then reports."""
    page_data = run_threads_tag_scrape(tag, num_posts, window=window)
    return _threads_comments_and_report(page_data, comments_per_post, send_email)


# ---------------------------------------------------------------------------
# YouTube pipeline (unchanged behavior)
# ---------------------------------------------------------------------------

def run_youtube_pipeline(channel_id: str = CHANNEL_ID, window=None):
    from collectors.youtube import collect_channel_data
    from ai.claude import analyze_youtube_comments
    from reports.formatter import format_text_report
    from reports.pdf_report import generate_pdf_report

    logger.info("Step 1-4: Collecting videos and comments for channel %s", channel_id)
    videos = collect_channel_data(
        channel_id=channel_id,
        video_count=VIDEO_COUNT,
        comments_per_video=COMMENTS_PER_VIDEO,
        window=window,
    )

    total_comments = sum(len(v.comments) for v in videos)
    logger.info("Collected %d videos, %d total comments", len(videos), total_comments)

    if total_comments == 0:
        raise RuntimeError(
            "No comments were collected. Check that the channel has public "
            "comments enabled, or try a different channel_id."
        )

    logger.info("Step 5: Sending comments to Claude for analysis")
    analysis = analyze_youtube_comments(videos)

    logger.info("Step 6: Formatting report")
    report = format_text_report(channel_id, len(videos), analysis)

    logger.info("Step 7: Generating PDF report")
    pdf_path = generate_pdf_report(channel_id, len(videos), analysis)
    logger.info("PDF report written to %s", pdf_path.resolve())

    return report, pdf_path


# ---------------------------------------------------------------------------
# Interactive menu (default when main.py is run without arguments)
# ---------------------------------------------------------------------------

def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or (default or "")


def _ask_int(prompt: str, default: int, maximum: int, minimum: int = 1) -> int:
    answer = _ask(f"{prompt} (max {maximum})", str(default))
    try:
        value = int(answer)
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _ask_comment_sort(window=None) -> str:
    """Which comments the per-post limit should pick.

    Worth a prompt rather than a silent default: with a small --comments the two
    answers are completely different reports. "Newest" on an old post returns the
    stragglers that arrived months after the conversation - real comments, but
    the ones nobody engaged with.
    """
    print("\nWhich comments should the per-post limit take?")
    print("  [1] Most relevant - Facebook's own ranking (what you see on the post)")
    print("  [2] Newest first  - every comment, most recent first (exhaustive dumps)")
    # Under a window the RECOMMENDED answer changes, so change the default
    # rather than overriding the answer afterwards - "relevant" is not
    # date-ordered and can only sample a range (see resolve_comment_sort).
    if window:
        print("  Note: a date range is set, so [2] is the default - 'relevant' "
              "is not date-ordered and can only sample the range.")
    default = "2" if window else "1"
    return "newest" if _ask("Select", default) == "2" else "relevant"


def _ask_yes_no(prompt: str, default_no: bool = True) -> bool:
    answer = _ask(f"{prompt} (y/n)", "n" if default_no else "y").lower()
    return answer.startswith("y")


def _ask_window():
    """Optional --start-date/--end-date window, asked once per menu.

    Returns a DateWindow or None. Both prompts accept an empty answer, so the
    default behaviour (newest-first until --posts is satisfied) is one Enter
    away and nobody is forced to think about dates to run a scrape.
    """
    from collectors.daterange import parse_window

    print("\nOptional date range - press Enter to skip and scrape newest-first.")
    print("  Give ONLY a start date to get everything from then until now.")
    while True:
        start = _ask("  Start date (YYYY-MM-DD, or 'YYYY-MM-DD HH:MM[:SS]')", "") or None
        # Say what BLANK means, on the prompt itself. A bare "[]" default reads
        # as "this is required and I have nothing to type", which is how a
        # start-only window - the common case, and the one with the early stop -
        # ends up being entered as a full range the user did not want.
        end = _ask("  End date  (blank = up to now)", "") or None
        if not start and not end:
            return None
        try:
            window = parse_window(start, end)
        except ValueError as exc:
            print(f"  {exc}")
            continue
        print(f"  Scraping posts {window.describe()}. Note this may page past "
              "newer posts to reach the window.")
        return window


def _print_report_outputs(report_text: str, pdf_path, dashboard_path) -> None:
    print(report_text)
    print(f"\nPDF report:  {pdf_path.resolve()}")
    print(f"Dashboard:   {dashboard_path.resolve()}")


def _ask_posts_or_window(target_word: str = "posts"):
    """Ask for the date range FIRST, and only ask "how many?" when there isn't one.

    The two questions answer the same thing - how much to collect - so asking
    both is asking the user to bound the run twice. A date range IS the bound:
    "every post in July" is a complete instruction, and following it with "how
    many posts?" invites an answer that silently truncates the window.

    Returns (num_posts, window).
    """
    from collectors.facebook.common import MAX_POSTS_LIMIT

    window = _ask_window()
    if window:
        # None, not MAX_POSTS_LIMIT. "No count needed" has to reach the
        # collector as an actual absence - a substituted cap becomes a target
        # every tier downstream then measures itself against.
        print(f"  Taking every {target_word[:-1]} in {window.describe()} "
              f"- no count needed; the date range is the bound.")
        return None, window
    return _ask_int(f"How many {target_word} to scrape", 50, MAX_POSTS_LIMIT), None


def _facebook_group_run(urls, num_posts, window, num_comments, comment_sort,
                        segment, send_email: bool = True):
    """Run one or more Facebook GROUPS through the same page/comment/report chain.

    Demo build: the real build ran groups through a concurrent, subprocess-
    isolated groups service. Here each group goes through the ordinary path one
    after another, which produces the same report shape.
    """
    for url in urls:
        page_data = run_facebook_page_scrape(url, num_posts, window=window)
        post_urls = [p.get("post_url") for p in page_data.get("posts") or []]
        run_facebook_batch_comment_scrape(post_urls, num_comments, comment_sort,
                                          window=window)
        print(f"\n{len(post_urls)} post(s) scraped and processed from group "
              f"'{page_data.get('page_name')}'. Sending to Claude...")
        _print_report_outputs(*generate_facebook_reports(send_email=send_email,
                                                         segment=segment))


def _facebook_target_run(target: str, is_group: bool):
    """The shared page/group flow: dates, then counts, then scrape + report."""
    from collectors.facebook.common import MAX_PAGE_COMMENTS

    noun = "group" if is_group else "page"
    num_posts, window = _ask_posts_or_window()
    num_comments = _ask_int("How many comments to scrape from EACH post",
                            1000, MAX_PAGE_COMMENTS)
    comment_sort = _ask_comment_sort(window)
    segment = not _ask_yes_no("Skip topic/sentiment segmentation?", default_no=True)

    page_data = run_facebook_page_scrape(target, num_posts, window=window)
    stats = page_data.get("statistics") or {}
    print(f"\n{stats.get('total_posts')} post(s) scraped and processed from "
          f"the {noun} '{page_data.get('page_name')}'.")

    post_urls = [p.get("post_url") for p in page_data.get("posts") or []]
    # The comment window is the POST window: only posts written in the range
    # were collected, and only comments written in the range are wanted on them.
    run_facebook_batch_comment_scrape(post_urls, num_comments, comment_sort,
                                      window=window)

    print("\nRaw + processed data saved. Sending to Claude...")
    _print_report_outputs(*generate_facebook_reports(segment=segment))


def _facebook_menu():
    print("\nFacebook - what do you want to do?")
    print("  [1] Public page scraping (posts + comments from each post)")
    print("  [2] Group scraping (posts + comments from each post)")
    print("  [3] Comment scraping (one post at a time)")
    choice = _ask("Select", "1")

    if choice == "2":
        raw = _ask("Facebook group URL(s) or id(s) - comma-separated for several "
                   "(facebook.com/groups/<id>)")
        urls = [normalize_group_target(u) for u in (raw or "").split(",") if u.strip()]
        if not urls:
            sys.exit("A group URL or id is required.")
        from collectors.facebook.common import MAX_PAGE_COMMENTS
        num_posts, window = _ask_posts_or_window()
        num_comments = _ask_int("How many comments to scrape from EACH post",
                                1000, MAX_PAGE_COMMENTS)
        comment_sort = _ask_comment_sort(window)
        segment = not _ask_yes_no("Skip topic/sentiment segmentation?", default_no=True)
        _facebook_group_run(urls, num_posts, window, num_comments, comment_sort,
                            segment)
        return

    if choice == "3":
        # Comment scraping: one post, its own comments report - fully
        # independent of any page scrape.
        post_url = _ask("Post URL (https://...)")
        if not post_url.startswith("http"):
            sys.exit("A full post URL (https://...) is required for comment scraping.")
        max_comments = _ask_int("How many comments to scrape", 1000, 1000)
        comment_sort = _ask_comment_sort()
        comments_data = run_facebook_comment_scrape(post_url, max_comments,
                                                    comment_sort)
        total = sum(len(p.get("comments") or []) for p in comments_data.get("posts") or [])
        print(f"\nRaw + processed data saved ({total} comment(s)). Sending to Claude...")

        report_text, pdf_path = generate_facebook_comments_report()
        print(report_text)
        print(f"\nPDF report:  {pdf_path.resolve()}")
    else:
        page_name = _ask("Facebook page username or URL (facebook.com/<username>)")
        if not page_name:
            sys.exit("A page name or URL is required.")
        if " " in page_name and not page_name.startswith("http"):
            # A display name with spaces is not a username - the URL built
            # from it will fail. Warn but let the search resolution try.
            print("Note: page usernames have no spaces - if this fails, copy the "
                  "username from the page's URL (facebook.com/<username>).")
        _facebook_target_run(page_name, is_group=False)


def _instagram_menu():
    print("\nInstagram - what do you want to do?")
    print("  [1] Public profile scraping (posts + comments from each post)")
    print("  [2] Comment scraping (one post at a time)")
    print("  [3] Hashtag scraping (posts + comments from each post)")
    choice = _ask("Select", "1")

    if choice == "2":
        # Comment scraping: one post, its own comments report - independent
        # of any profile scrape.
        post_url = _ask("Post URL (https://www.instagram.com/p/...)")
        if not post_url.startswith("http"):
            sys.exit("A full post URL (https://...) is required for comment scraping.")
        max_comments = _ask_int("How many comments to scrape", 1000, 1000)
        comments_data = run_instagram_comment_scrape(post_url, max_comments)
        total = sum(len(p.get("comments") or []) for p in comments_data.get("posts") or [])
        print(f"\nRaw + processed data saved ({total} comment(s)). Sending to Claude...")

        report_text, pdf_path = generate_instagram_comments_report()
        print(report_text)
        print(f"\nPDF report:  {pdf_path.resolve()}")
        return

    # Options [1] and [3] share everything after the post scrape - only the
    # target (a profile vs. a hashtag) and its scraper differ.
    if choice == "3":
        hashtag = _ask("Instagram hashtag (with or without #)")
        if not hashtag:
            sys.exit("A hashtag is required.")
        # Hashtag feeds are large and many-authored - default to the full cap.
        num_posts = _ask_int("How many posts to scrape", 1000, 1000)
        num_comments = _ask_int("How many comments to scrape from EACH post", 1000, 1000)
        window = _ask_window()
        page_data = run_instagram_hashtag_scrape(hashtag, num_posts, window=window)
    else:
        profile = _ask("Instagram username or profile URL (instagram.com/<username>)")
        if not profile:
            sys.exit("A username or profile URL is required.")
        num_posts = _ask_int("How many posts to scrape", 50, 1000)
        num_comments = _ask_int("How many comments to scrape from EACH post", 1000, 1000)
        window = _ask_window()
        page_data = run_instagram_page_scrape(profile, num_posts, window=window)

    stats = page_data.get("statistics") or {}
    print(f"\n{stats.get('total_posts')} post(s) scraped and processed from "
          f"'{page_data.get('page_name')}'.")

    from collectors.instagram import collect_profile_comments
    from processors import instagram_comments_processor

    post_urls = [p.get("post_url") for p in page_data.get("posts") or []]
    logger.info("Collecting %d comment(s) from each of %d post(s)",
                num_comments, len(post_urls))
    collect_profile_comments(post_urls, num_comments)
    instagram_comments_processor.run()

    print("\nRaw + processed data saved. Sending to Claude...")
    report_text, pdf_path, dashboard_path = generate_instagram_reports()

    print(report_text)
    print(f"\nPDF report:  {pdf_path.resolve()}")
    print(f"Dashboard:   {dashboard_path.resolve()}")


def _tiktok_menu():
    print("\nTikTok - what do you want to do?")
    print("  [1] Public profile scraping (posts + comments from each post)")
    print("  [2] Comment scraping (one video at a time)")
    print("  [3] Hashtag scraping (posts + comments from each post)")
    choice = _ask("Select", "1")

    if choice == "2":
        # Comment scraping: one video, its own comments report - independent
        # of any profile scrape.
        video_url = _ask("Video URL (https://www.tiktok.com/@user/video/...)")
        if not video_url.startswith("http"):
            sys.exit("A full video URL (https://...) is required for comment scraping.")
        max_comments = _ask_int("How many comments to scrape", 1000, 1000)
        comments_data = run_tiktok_comment_scrape(video_url, max_comments)
        total = sum(len(p.get("comments") or []) for p in comments_data.get("posts") or [])
        print(f"\nRaw + processed data saved ({total} comment(s)). Sending to Claude...")

        report_text, pdf_path = generate_tiktok_comments_report()
        print(report_text)
        print(f"\nPDF report:  {pdf_path.resolve()}")
        return

    # Options [1] and [3] share everything after the post scrape - only the
    # target (a profile vs. a hashtag) and its scraper differ.
    if choice == "3":
        hashtag = _ask("TikTok hashtag (with or without #)")
        if not hashtag:
            sys.exit("A hashtag is required.")
        # Hashtag feeds are large and many-authored - default to the full cap.
        num_posts = _ask_int("How many posts to scrape", 1000, 1000)
        num_comments = _ask_int("How many comments to scrape from EACH post", 1000, 1000)
        window = _ask_window()
        page_data = run_tiktok_hashtag_scrape(hashtag, num_posts, window=window)
    else:
        profile = _ask("TikTok username or profile URL (tiktok.com/@<username>)")
        if not profile:
            sys.exit("A username or profile URL is required.")
        num_posts = _ask_int("How many posts to scrape", 50, 1000)
        num_comments = _ask_int("How many comments to scrape from EACH post", 1000, 1000)
        window = _ask_window()
        page_data = run_tiktok_page_scrape(profile, num_posts, window=window)

    stats = page_data.get("statistics") or {}
    print(f"\n{stats.get('total_posts')} post(s) scraped and processed from "
          f"'{page_data.get('page_name')}'.")

    from collectors.tiktok import collect_profile_comments
    from processors import tiktok_comments_processor

    video_urls = [p.get("post_url") for p in page_data.get("posts") or []]
    logger.info("Collecting %d comment(s) from each of %d video(s)",
                num_comments, len(video_urls))
    collect_profile_comments(video_urls, num_comments)
    tiktok_comments_processor.run()

    print("\nRaw + processed data saved. Sending to Claude...")
    report_text, pdf_path, dashboard_path = generate_tiktok_reports()

    print(report_text)
    print(f"\nPDF report:  {pdf_path.resolve()}")
    print(f"Dashboard:   {dashboard_path.resolve()}")


def _linkedin_menu():
    print("\nLinkedIn - what do you want to do?")
    print("  [1] Company page scraping (posts + comments from each post)")
    print("  [2] Profile scraping (a person's posts + comments from each post)")
    print("  [3] Hashtag scraping (posts + comments from each post)")
    print("  [4] Comment scraping (one post at a time)")
    choice = _ask("Select", "1")

    if choice == "4":
        # Comment scraping: one post, its own comments report - independent of
        # any page scrape.
        post_url = _ask("Post URL (https://www.linkedin.com/feed/update/...)")
        if not post_url.startswith("http"):
            sys.exit("A full post URL (https://...) is required for comment scraping.")
        max_comments = _ask_int("How many comments to scrape", 1000, 1000)
        comments_data = run_linkedin_comment_scrape(post_url, max_comments)
        total = sum(len(p.get("comments") or []) for p in comments_data.get("posts") or [])
        print(f"\nRaw + processed data saved ({total} comment(s)). Sending to Claude...")

        report_text, pdf_path = generate_linkedin_comments_report()
        print(report_text)
        print(f"\nPDF report:  {pdf_path.resolve()}")
        return

    # Options [1]/[2]/[3] share everything after the post scrape - only the
    # target and its scraper differ.
    if choice == "2":
        profile = _ask("LinkedIn profile vanity name or /in/ URL")
        if not profile:
            sys.exit("A profile name or URL is required.")
        num_posts = _ask_int("How many posts to scrape", 50, 1000)
        num_comments = _ask_int("How many comments to scrape from EACH post", 1000, 1000)
        window = _ask_window()
        page_data = run_linkedin_profile_scrape(profile, num_posts, window=window)
    elif choice == "3":
        hashtag = _ask("LinkedIn hashtag (with or without #)")
        if not hashtag:
            sys.exit("A hashtag is required.")
        num_posts = _ask_int("How many posts to scrape", 1000, 1000)
        num_comments = _ask_int("How many comments to scrape from EACH post", 1000, 1000)
        window = _ask_window()
        page_data = run_linkedin_hashtag_scrape(hashtag, num_posts, window=window)
    else:
        company = _ask("LinkedIn company name or /company/ URL")
        if not company:
            sys.exit("A company name or URL is required.")
        # A bare menu digit here is a mis-keyed selection, not a company. It is
        # otherwise accepted (`/company/1/` is a well-formed URL), and the run
        # only fails ~40 s later with "no posts were found for '1'".
        if company in {"1", "2", "3", "4"}:
            sys.exit(f"'{company}' looks like a menu selection, not a company. "
                     "Enter the name from the page's /company/<name>/ URL "
                     "(for example: pwc).")
        num_posts = _ask_int("How many posts to scrape", 50, 1000)
        num_comments = _ask_int("How many comments to scrape from EACH post", 1000, 1000)
        window = _ask_window()
        page_data = run_linkedin_company_scrape(company, num_posts, window=window)

    stats = page_data.get("statistics") or {}
    print(f"\n{stats.get('total_posts')} post(s) scraped and processed from "
          f"'{page_data.get('page_name')}'.")

    from collectors.linkedin import collect_feed_comments
    from processors import linkedin_comments_processor

    # LinkedIn's public post page serves at most 10 comments each, fetched
    # anonymously in seconds. Going past that means re-opening those posts in the
    # browser: far better coverage, but ~15 s per post - so it is asked for, not
    # assumed.
    deep = False
    if num_comments > 10:
        deep = _ask_yes_no(
            "The public page gives 10 comments per post in seconds. Read past "
            "that in the browser (much slower)", default_no=True)

    post_urls = [p.get("post_url") for p in page_data.get("posts") or []]
    logger.info("Collecting %d comment(s) from each of %d post(s)",
                num_comments, len(post_urls))
    collect_feed_comments(post_urls, num_comments, deep=deep)
    linkedin_comments_processor.run()

    print("\nRaw + processed data saved. Sending to Claude...")
    report_text, pdf_path, dashboard_path = generate_linkedin_reports()

    print(report_text)
    print(f"\nPDF report:  {pdf_path.resolve()}")
    print(f"Dashboard:   {dashboard_path.resolve()}")


def _x_menu():
    print("\nX (Twitter) - what do you want to do?")
    print("  [1] Profile scraping (posts + replies from each post)")
    print("  [2] Hashtag / search scraping (posts + replies from each post)")
    print("  [3] Reply scraping (one post at a time)")
    choice = _ask("Select", "1")

    if choice == "3":
        post_url = _ask("Post URL (https://x.com/<handle>/status/<id>)")
        if not post_url.startswith("http"):
            sys.exit("A full post URL (https://...) is required for reply scraping.")
        max_comments = _ask_int("How many replies to scrape", 1000, 1000)
        comments_data = run_x_comment_scrape(post_url, max_comments)
        total = sum(len(p.get("comments") or []) for p in comments_data.get("posts") or [])
        print(f"\nRaw + processed data saved ({total} repl(y/ies)). Sending to Claude...")

        report_text, pdf_path = generate_x_comments_report()
        print(report_text)
        print(f"\nPDF report:  {pdf_path.resolve()}")
        return

    if choice == "2":
        hashtag = _ask("Hashtag (e.g. space) or raw search query")
        if not hashtag:
            sys.exit("A hashtag or search query is required.")
        num_posts = _ask_int("How many posts to scrape", 50, 1000)
        comments_per_post = _ask_int("How many replies from EACH post", 1000, 1000)
        window = _ask_window()
        page_data = run_x_hashtag_scrape(hashtag, num_posts, window=window)
    else:
        profile = _ask("X handle (e.g. nasa) or profile URL", "nasa")
        num_posts = _ask_int("How many posts to scrape", 50, 1000)
        comments_per_post = _ask_int("How many replies from EACH post", 1000, 1000)
        window = _ask_window()
        page_data = run_x_profile_scrape(profile, num_posts, window=window)
    print(f"\nScraped {len(page_data.get('posts') or [])} post(s). "
          "Collecting replies...")
    report_text, pdf_path, dashboard_path = _x_comments_and_report(
        page_data, comments_per_post, send_email=True)

    print(report_text)
    print(f"\nPDF report:  {pdf_path.resolve()}")
    print(f"Dashboard:   {dashboard_path.resolve()}")


def _threads_menu():
    print("\nThreads - what do you want to do?")
    print("  [1] Profile scraping (posts + replies from each post)")
    print("  [2] Tag / topic scraping (posts + replies from each post)")
    print("  [3] Reply scraping (one post at a time)")
    choice = _ask("Select", "1")

    if choice == "3":
        post_url = _ask("Post URL (https://www.threads.com/@<user>/post/<code>)")
        if not post_url.startswith("http"):
            sys.exit("A full post URL (https://...) is required for reply scraping.")
        max_comments = _ask_int("How many replies to scrape", 1000, 1000)
        comments_data = run_threads_comment_scrape(post_url, max_comments)
        total = sum(len(p.get("comments") or []) for p in comments_data.get("posts") or [])
        print(f"\nRaw + processed data saved ({total} repl(y/ies)). Sending to Claude...")

        report_text, pdf_path = generate_threads_comments_report()
        print(report_text)
        print(f"\nPDF report:  {pdf_path.resolve()}")
        return

    if choice == "2":
        tag = _ask("Threads tag/topic (with or without #)")
        if not tag:
            sys.exit("A tag is required.")
        # Threads serves the tag feed one-shot - it carries no pagination
        # cursor - so asking for more than it renders cannot help.
        print("  Note: Threads serves the tag feed in one shot (no pagination), "
              "so this returns whatever the public feed rendered (~16-19 posts).")
        num_posts = _ask_int("How many posts to scrape", 25, 1000)
        comments_per_post = _ask_int("How many replies from EACH post", 1000, 1000)
        window = _ask_window()
        page_data = run_threads_tag_scrape(tag, num_posts, window=window)
    else:
        profile = _ask("Threads username (e.g. lumen) or profile URL", "lumen")
        num_posts = _ask_int("How many posts to scrape", 50, 1000)
        comments_per_post = _ask_int("How many replies from EACH post", 1000, 1000)
        window = _ask_window()
        page_data = run_threads_profile_scrape(profile, num_posts, window=window)

    print(f"\nScraped {len(page_data.get('posts') or [])} post(s). "
          "Collecting replies...")
    print("  Note: Threads serves only a subset of each post's replies to "
          "logged-out callers (~5-10%); the report states the coverage.")
    report_text, pdf_path, dashboard_path = _threads_comments_and_report(
        page_data, comments_per_post, send_email=True)

    print(report_text)
    print(f"\nPDF report:  {pdf_path.resolve()}")
    print(f"Dashboard:   {dashboard_path.resolve()}")


def interactive_main():
    print("GhostFetch (demo build: synthetic data)")
    print("  [1] Facebook")
    print("  [2] Instagram")
    print("  [3] YouTube")
    print("  [4] TikTok")
    print("  [5] LinkedIn")
    print("  [6] X (Twitter)")
    print("  [7] Threads")
    platform = _ask("Select platform", "1")

    if platform == "2":
        _instagram_menu()
    elif platform == "3":
        channel = _ask("YouTube channel id", CHANNEL_ID)
        window = _ask_window()
        report_text, pdf_path = run_youtube_pipeline(channel_id=channel,
                                                     window=window)
        print(report_text)
        print(f"\nPDF report saved to: {pdf_path.resolve()}")
    elif platform == "4":
        _tiktok_menu()
    elif platform == "5":
        _linkedin_menu()
    elif platform == "6":
        _x_menu()
    elif platform == "7":
        _threads_menu()
    else:
        _facebook_menu()


# ---------------------------------------------------------------------------
# CLI (used when arguments are passed - e.g. automation / cron)
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) == 1:
        interactive_main()
        return

    parser = argparse.ArgumentParser(description="GhostFetch (demo build: synthetic data)")
    subparsers = parser.add_subparsers(dest="platform", required=True)

    fb = subparsers.add_parser("facebook", help="Run the Facebook pipeline")
    fb_target = fb.add_mutually_exclusive_group(required=True)
    fb_target.add_argument("--page", help="Facebook page name or URL")
    fb_target.add_argument("--group",
                           help="Facebook group URL or id "
                                "(facebook.com/groups/<id>). The group feed is "
                                "read in CREATION order, so a date window means "
                                "posts WRITTEN then - not old posts bumped back "
                                "up by new comments, which is what the group "
                                "feed does by default")
    fb.add_argument("--posts", type=int, default=None,
                    help="Posts to scrape (max 1000). Omit it together with "
                         "--start-date/--end-date to take every post in the window")
    fb.add_argument("--post-url", default=None,
                    help="Scrape comments from this ONE post. Omit it and every "
                         "post the feed scrape returned gets its comments collected")
    fb.add_argument("--no-segment", action="store_true",
                    help="Skip the per-post/per-comment topic+sentiment "
                         "segmentation (saves a few Claude calls)")
    fb.add_argument("--comments", type=int, default=1000,
                    help="Max comments to scrape (max 1000)")
    fb.add_argument("--comment-sort", choices=("relevant", "newest"),
                    default=None,   # None = unspecified; see resolve_comment_sort
                    help="Which comments the --comments limit takes: 'relevant' "
                         "(Facebook's own ranking; the default, except under a "
                         "date range, where 'newest' is - it is the only sort "
                         "that can cover one) or 'newest' "
                         "(most recent first - exhaustive, but on an old post "
                         "the newest comments are the low-engagement stragglers)")
    fb.add_argument("--start-date", default=None,
                    help="Only collect posts on or after this date/time (YYYY-MM-DD, or 'YYYY-MM-DD HH:MM[:SS]', UTC; date-only = start of day). The run pages PAST newer posts to cover the window, so --posts caps the in-window results")
    fb.add_argument("--end-date", default=None,
                    help="Only collect posts on or before this date/time (YYYY-MM-DD, or 'YYYY-MM-DD HH:MM[:SS]', UTC; date-only = end of day)")
    fb.add_argument("--no-email", action="store_true", help="Skip the Gmail notification step")

    ig = subparsers.add_parser("instagram", help="Run the Instagram pipeline")
    ig_target = ig.add_mutually_exclusive_group(required=True)
    ig_target.add_argument("--profile", help="Instagram username or profile URL")
    ig_target.add_argument("--hashtag", help="Instagram hashtag (with or without #)")
    ig.add_argument("--posts", type=int, default=None,
                           help="Posts to scrape (max 1000). OMIT IT with a date "
                                "range: the range is then the stopping condition, "
                                "and a count here would cut the window short. "
                                "Defaults to 50 when no range is given.")
    ig.add_argument("--comments", type=int, default=1000,
                    help="Comments to scrape from EACH post (max 1000)")
    ig.add_argument("--start-date", default=None,
                    help="Only collect posts on or after this date (YYYY-MM-DD, UTC). The run pages PAST newer posts to cover the window, so --posts caps the in-window results")
    ig.add_argument("--end-date", default=None,
                    help="Only collect posts on or before this date (YYYY-MM-DD, UTC)")
    ig.add_argument("--no-email", action="store_true", help="Skip the Gmail notification step")

    tt = subparsers.add_parser("tiktok", help="Run the TikTok pipeline")
    tt_target = tt.add_mutually_exclusive_group(required=True)
    tt_target.add_argument("--profile", help="TikTok username or profile URL")
    tt_target.add_argument("--hashtag", help="TikTok hashtag (with or without #)")
    tt.add_argument("--posts", type=int, default=None,
                           help="Posts to scrape (max 1000). OMIT IT with a date "
                                "range: the range is then the stopping condition, "
                                "and a count here would cut the window short. "
                                "Defaults to 50 when no range is given.")
    tt.add_argument("--comments", type=int, default=1000,
                    help="Comments to scrape from EACH post (max 1000)")
    tt.add_argument("--start-date", default=None,
                    help="Only collect posts on or after this date (YYYY-MM-DD, UTC). The run pages PAST newer posts to cover the window, so --posts caps the in-window results")
    tt.add_argument("--end-date", default=None,
                    help="Only collect posts on or before this date (YYYY-MM-DD, UTC)")
    tt.add_argument("--no-email", action="store_true", help="Skip the Gmail notification step")

    li = subparsers.add_parser("linkedin", help="Run the LinkedIn pipeline")
    li_target = li.add_mutually_exclusive_group(required=True)
    li_target.add_argument("--company", help="LinkedIn company name or /company/ URL")
    li_target.add_argument("--profile", help="LinkedIn profile vanity name or /in/ URL")
    li_target.add_argument("--hashtag", help="LinkedIn hashtag (with or without #)")
    li.add_argument("--posts", type=int, default=None,
                           help="Posts to scrape (max 1000). OMIT IT with a date "
                                "range: the range is then the stopping condition, "
                                "and a count here would cut the window short. "
                                "Defaults to 50 when no range is given.")
    li.add_argument("--comments", type=int, default=1000,
                    help="Comments to scrape from EACH post (max 1000)")
    li.add_argument("--deep-comments", action="store_true",
                    help="Read past the public page's 10-comments-per-post ceiling "
                         "by re-opening those posts in the browser. Much higher "
                         "coverage, much slower (~15s per topped-up post)")
    li.add_argument("--start-date", default=None,
                    help="Only collect posts on or after this date (YYYY-MM-DD, UTC). The run pages PAST newer posts to cover the window, so --posts caps the in-window results")
    li.add_argument("--end-date", default=None,
                    help="Only collect posts on or before this date (YYYY-MM-DD, UTC)")
    li.add_argument("--no-email", action="store_true", help="Skip the Gmail notification step")

    # aliases: `x` is the current name, `twitter` the familiar one. NOTE that
    # add_subparsers(dest="platform") stores WHAT THE USER TYPED, so the
    # dispatch below must accept both spellings.
    xp = subparsers.add_parser("x", aliases=["twitter"], help="Run the X (Twitter) pipeline")
    xp_target = xp.add_mutually_exclusive_group(required=True)
    xp_target.add_argument("--profile", help="X handle (e.g. nasa) or profile URL")
    xp_target.add_argument("--hashtag", help="X hashtag (with or without #) or raw "
                                             "search query. Opens a browser: X's "
                                             "search endpoint requires a signed header")
    xp_target.add_argument("--post-url", help="Scrape replies for ONE post URL")
    xp.add_argument("--posts", type=int, default=None,
                           help="Posts to scrape (max 1000). OMIT IT with a date "
                                "range: the range is then the stopping condition, "
                                "and a count here would cut the window short. "
                                "Defaults to 50 when no range is given.")
    xp.add_argument("--comments", type=int, default=1000,
                    help="Replies to scrape from EACH post (max 1000)")
    xp.add_argument("--start-date", default=None,
                    help="Only collect posts on or after this date (YYYY-MM-DD, UTC). The run pages PAST newer posts to cover the window, so --posts caps the in-window results")
    xp.add_argument("--end-date", default=None,
                    help="Only collect posts on or before this date (YYYY-MM-DD, UTC)")
    xp.add_argument("--no-email", action="store_true", help="Skip the Gmail notification step")

    th = subparsers.add_parser("threads", help="Run the Threads pipeline")
    th_target = th.add_mutually_exclusive_group(required=True)
    th_target.add_argument("--profile", help="Threads username (e.g. lumen) or profile URL")
    th_target.add_argument("--tag", help="Threads tag/topic (with or without #). "
                                         "Served one-shot by Threads - no pagination")
    th_target.add_argument("--post-url", help="Scrape replies for ONE post URL")
    th.add_argument("--posts", type=int, default=None,
                           help="Posts to scrape (max 1000). OMIT IT with a date "
                                "range: the range is then the stopping condition, "
                                "and a count here would cut the window short. "
                                "Defaults to 50 when no range is given.")
    th.add_argument("--comments", type=int, default=1000,
                    help="Replies to scrape from EACH post (max 1000)")
    th.add_argument("--start-date", default=None,
                    help="Only collect posts on or after this date (YYYY-MM-DD, UTC). "
                         "The run pages PAST newer posts to cover the window, so "
                         "--posts caps the in-window results")
    th.add_argument("--end-date", default=None,
                    help="Only collect posts on or before this date (YYYY-MM-DD, UTC)")
    th.add_argument("--no-email", action="store_true", help="Skip the Gmail notification step")

    yt = subparsers.add_parser("youtube", help="Run the YouTube pipeline")
    yt.add_argument("--channel", default=CHANNEL_ID, help="YouTube channel id")
    yt.add_argument("--start-date", default=None,
                    help="Only collect videos on or after this date (YYYY-MM-DD, UTC)")
    yt.add_argument("--end-date", default=None,
                    help="Only collect videos on or before this date (YYYY-MM-DD, UTC)")

    args = parser.parse_args()

    # One place to turn the two CLI strings into a window - so an unparseable
    # or inverted range fails immediately with a clear message, before any
    # browser is launched or any request is made.
    from collectors.daterange import parse_window
    try:
        window = parse_window(getattr(args, "start_date", None),
                              getattr(args, "end_date", None))
    except ValueError as exc:
        sys.exit(str(exc))

    if args.platform == "facebook":
        # A date window defines the run, so --posts becomes optional - and when
        # it is omitted the count stays None ALL THE WAY DOWN. Substituting
        # MAX_POSTS_LIMIT here (which this did) is not a harmless ceiling: it is
        # indistinguishable from `--posts 1000` by the time it reaches the
        # collector, so the feed logged "1/1000 target" and fell into a browser
        # scroll for failing to reach a number nobody asked for. Without a
        # window there is no other stopping condition, so None keeps the
        # historical default of 50.
        num_posts = args.posts
        if num_posts is None and not window:
            num_posts = 50
        if args.group:
            # GROUPS go through the canonical Groups service (same as
            # the interactive menu). --group accepts a comma-separated list for
            # concurrent multi-group runs. _facebook_group_run prints the report(s)
            # and preserves the EXIT_AUTH / EXIT_PARTIAL semantics itself.
            urls = [normalize_group_target(u) for u in args.group.split(",") if u.strip()]
            _facebook_group_run(
                urls, num_posts, window, args.comments,
                resolve_comment_sort(args.comment_sort, window),
                segment=not args.no_segment, send_email=not args.no_email)
        else:
            report_text, pdf_path, dashboard_path = run_facebook_pipeline(
                page_name=args.page,
                num_posts=num_posts,
                post_url=args.post_url,
                max_comments=args.comments,
                comment_sort=args.comment_sort,
                send_email=not args.no_email,
                window=window,
                segment=not args.no_segment,
            )
            print(report_text)
            print(f"\nPDF report:  {pdf_path.resolve()}")
            print(f"Dashboard:   {dashboard_path.resolve()}")
    elif args.platform == "instagram":
        if args.hashtag:
            report_text, pdf_path, dashboard_path = run_instagram_hashtag_pipeline(
                hashtag=args.hashtag,
                num_posts=args.posts,
                comments_per_post=args.comments,
                send_email=not args.no_email,
                        window=window,
        )
        else:
            report_text, pdf_path, dashboard_path = run_instagram_pipeline(
                profile=args.profile,
                num_posts=args.posts,
                comments_per_post=args.comments,
                send_email=not args.no_email,
                        window=window,
        )
        print(report_text)
        print(f"\nPDF report:  {pdf_path.resolve()}")
        print(f"Dashboard:   {dashboard_path.resolve()}")
    elif args.platform == "tiktok":
        if args.hashtag:
            report_text, pdf_path, dashboard_path = run_tiktok_hashtag_pipeline(
                hashtag=args.hashtag,
                num_posts=args.posts,
                comments_per_post=args.comments,
                send_email=not args.no_email,
                window=window,
            )
        else:
            report_text, pdf_path, dashboard_path = run_tiktok_pipeline(
                profile=args.profile,
                num_posts=args.posts,
                comments_per_post=args.comments,
                send_email=not args.no_email,
                window=window,
            )
        print(report_text)
        print(f"\nPDF report:  {pdf_path.resolve()}")
        print(f"Dashboard:   {dashboard_path.resolve()}")
    elif args.platform == "linkedin":
        if args.hashtag:
            report_text, pdf_path, dashboard_path = run_linkedin_hashtag_pipeline(
                hashtag=args.hashtag,
                num_posts=args.posts,
                comments_per_post=args.comments,
                send_email=not args.no_email,
                deep_comments=args.deep_comments,
                        window=window,
        )
        elif args.profile:
            report_text, pdf_path, dashboard_path = run_linkedin_profile_pipeline(
                profile=args.profile,
                num_posts=args.posts,
                comments_per_post=args.comments,
                send_email=not args.no_email,
                deep_comments=args.deep_comments,
                        window=window,
        )
        else:
            report_text, pdf_path, dashboard_path = run_linkedin_company_pipeline(
                company=args.company,
                num_posts=args.posts,
                comments_per_post=args.comments,
                send_email=not args.no_email,
                deep_comments=args.deep_comments,
                        window=window,
        )
        print(report_text)
        print(f"\nPDF report:  {pdf_path.resolve()}")
        print(f"Dashboard:   {dashboard_path.resolve()}")
    elif args.platform in ("x", "twitter"):
        if args.hashtag:
            report_text, pdf_path, dashboard_path = run_x_hashtag_pipeline(
                hashtag=args.hashtag,
                num_posts=args.posts,
                comments_per_post=args.comments,
                send_email=not args.no_email,
                        window=window,
        )
            print(report_text)
            print(f"\nPDF report:  {pdf_path.resolve()}")
            print(f"Dashboard:   {dashboard_path.resolve()}")
        elif args.post_url:
            comments_data = run_x_comment_scrape(args.post_url, args.comments)
            total = sum(len(p.get("comments") or [])
                        for p in comments_data.get("posts") or [])
            print(f"Scraped {total} repl(y/ies). Sending to Claude...")
            report_text, pdf_path = generate_x_comments_report()
            print(report_text)
            print(f"\nPDF report:  {pdf_path.resolve()}")
        else:
            report_text, pdf_path, dashboard_path = run_x_profile_pipeline(
                profile=args.profile,
                num_posts=args.posts,
                comments_per_post=args.comments,
                send_email=not args.no_email,
                        window=window,
        )
            print(report_text)
            print(f"\nPDF report:  {pdf_path.resolve()}")
            print(f"Dashboard:   {dashboard_path.resolve()}")
    elif args.platform == "threads":
        if args.post_url:
            comments_data = run_threads_comment_scrape(args.post_url, args.comments)
            total = sum(len(p.get("comments") or [])
                        for p in comments_data.get("posts") or [])
            print(f"Scraped {total} repl(y/ies). Sending to Claude...")
            report_text, pdf_path = generate_threads_comments_report()
            print(report_text)
            print(f"\nPDF report:  {pdf_path.resolve()}")
        else:
            if args.tag:
                report_text, pdf_path, dashboard_path = run_threads_tag_pipeline(
                    tag=args.tag,
                    num_posts=args.posts,
                    comments_per_post=args.comments,
                    send_email=not args.no_email,
                    window=window,
                )
            else:
                report_text, pdf_path, dashboard_path = run_threads_profile_pipeline(
                    profile=args.profile,
                    num_posts=args.posts,
                    comments_per_post=args.comments,
                    send_email=not args.no_email,
                    window=window,
                )
            _print_report_outputs(report_text, pdf_path, dashboard_path)
    else:
        report_text, pdf_path = run_youtube_pipeline(channel_id=args.channel, window=window)
        print(report_text)
        print(f"\nPDF report saved to: {pdf_path.resolve()}")


if __name__ == "__main__":
    main()
