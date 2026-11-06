#!/usr/bin/env python3
"""The GhostFetch demo pipeline, end to end, offline.

    python3 test_demo_pipeline.py

The public repository ships demo collectors (collectors/demo.py) in place of
the real scrapers. This suite proves the rest of the system still works on top
of them, for every platform:

  * each collector keeps the public API the CLI and the Actors call, writes the
    raw documents to its data directory, and opens NO network connection;
  * posts and comments JOIN on post_url, and a date window is honoured;
  * the real processors turn the raw documents into the processed shape;
  * the offline analysis (no ANTHROPIC_API_KEY) and the topic/sentiment
    segmentation produce a result;
  * the text report, the PDF and the HTML dashboard are generated.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Everything this suite writes goes to a scratch directory, and it must be set
# BEFORE a collector is imported: each one reads its data directory at import.
SCRATCH = Path(tempfile.mkdtemp(prefix="ghostfetch-demo-"))
DATA_ENV = {"facebook": "FB_DATA_DIR", "instagram": "IG_DATA_DIR",
            "tiktok": "TIKTOK_DATA_DIR", "linkedin": "LINKEDIN_DATA_DIR",
            "x": "X_DATA_DIR", "threads": "THREADS_DATA_DIR"}
for _platform, _env in DATA_ENV.items():
    os.environ[_env] = str(SCRATCH / _platform)
# No key means the offline demo analysis, never an API call.
os.environ["ANTHROPIC_API_KEY"] = ""

from collectors.daterange import parse_window  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


class NetworkUsed(AssertionError):
    pass


class no_network:
    """Fail the test if anything opens a socket while the block runs."""

    def __enter__(self):
        self._orig = socket.socket.connect
        self._orig_ex = socket.socket.connect_ex

        def refuse(sock, address, *a, **k):
            raise NetworkUsed(f"network connection attempted to {address!r}")

        socket.socket.connect = refuse  # type: ignore[method-assign]
        socket.socket.connect_ex = refuse  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc):
        socket.socket.connect = self._orig  # type: ignore[method-assign]
        socket.socket.connect_ex = self._orig_ex  # type: ignore[method-assign]
        return False


# platform -> (posts call, comments call taking (posts_doc, per_post))
def _feed_comments(module, posts_doc, n):
    return module.collect_feed_comments(posts_doc["posts"], n)


def _url_comments(fn_name):
    def call(module, posts_doc, n):
        return getattr(module, fn_name)([p["post_url"] for p in posts_doc["posts"]], n)
    return call


PLATFORMS = {
    "facebook": (lambda m, n, w: m.collect_page_posts("lumen", n, window=w),
                 _url_comments("collect_page_comments")),
    "instagram": (lambda m, n, w: m.collect_profile_posts("lumen", n, window=w),
                  _url_comments("collect_profile_comments")),
    "tiktok": (lambda m, n, w: m.collect_profile_posts("lumen", n, window=w),
               _url_comments("collect_profile_comments")),
    "linkedin": (lambda m, n, w: m.collect_company_posts("lumen", n, window=w),
                 _feed_comments),
    "x": (lambda m, n, w: m.collect_profile_posts("lumen", n, window=w), _feed_comments),
    "threads": (lambda m, n, w: m.collect_profile_posts("lumen", n, window=w), _feed_comments),
}


def collect(platform, num_posts=6, per_post=5, window=None):
    module = importlib.import_module(f"collectors.{platform}")
    posts_call, comments_call = PLATFORMS[platform]
    with no_network():
        posts_doc = posts_call(module, num_posts, window)
        comments_doc = comments_call(module, posts_doc, per_post)
    return module, posts_doc, comments_doc


# --- collectors -------------------------------------------------------------


@test
def test_every_collector_writes_its_raw_documents_offline():
    for platform in PLATFORMS:
        module, posts_doc, comments_doc = collect(platform)
        assert posts_doc["status"] == "success", (platform, posts_doc["status"])
        assert posts_doc["demo"] is True and comments_doc["demo"] is True, platform
        assert len(posts_doc["posts"]) == 6, (platform, len(posts_doc["posts"]))
        for name in ("POSTS_OUTPUT_FILE", "COMMENTS_OUTPUT_FILE"):
            path = Path(getattr(module, name))
            assert path.is_relative_to(SCRATCH), (platform, path)
            assert json.loads(path.read_text(encoding="utf-8"))["demo"] is True


@test
def test_comments_join_posts_and_respect_the_cap():
    for platform in PLATFORMS:
        _, posts_doc, comments_doc = collect(platform, per_post=3)
        post_urls = {p["post_url"] for p in posts_doc["posts"]}
        records = comments_doc["posts"]
        assert {r["post_url"] for r in records} == post_urls, platform
        for record in records:
            assert 0 < len(record["comments"]) <= 3, (platform, len(record["comments"]))
            for comment in record["comments"]:
                assert comment["comment"] and comment["username"], platform


@test
def test_collection_is_deterministic():
    for platform in ("facebook", "x"):
        _, first, _ = collect(platform)
        _, second, _ = collect(platform)
        strip = lambda doc: [(p["post_url"], p["caption"], p["likes"]) for p in doc["posts"]]  # noqa: E731
        assert strip(first) == strip(second), platform


@test
def test_a_date_window_is_honoured():
    window = parse_window("2026-03-01", "2026-03-31")
    for platform in PLATFORMS:
        _, posts_doc, _ = collect(platform, num_posts=None, window=window)
        posts = posts_doc["posts"]
        assert posts, platform
        assert all("2026-03-01" <= p["post_date"][:10] <= "2026-03-31" for p in posts), platform
        assert posts_doc["date_window"]["complete"] is True, platform


@test
def test_the_facebook_group_and_youtube_paths():
    from collectors.facebook_groups import FAILURE_STATUSES, collect_group
    from collectors.youtube import collect_channel_data

    with no_network():
        run = collect_group("https://www.facebook.com/groups/123", max_posts=5,
                            max_comments_per_post=3)
        bad = collect_group("https://example.com/not-a-group")
        videos = collect_channel_data("demo-channel", video_count=3, comments_per_video=4)
    assert run.status == "success" and len(run.document["posts"]) == 5
    assert run.posts_doc["page_information"]["page_type"] == "group"
    assert bad.status in FAILURE_STATUSES
    assert len(videos) == 3 and all(0 < len(v.comments) <= 4 for v in videos)


@test
def test_no_real_scraper_is_shipped():
    """Every collector package answers from collectors/demo.py and nothing in
    collectors/ imports a browser or an HTTP client."""
    banned = ("playwright", "httpx", "requests", "googleapiclient", "urllib.request")
    for path in (ROOT / "collectors").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in banned:
            assert f"import {name}" not in text and f"from {name}" not in text, (path, name)


# --- processors, analysis and reports ---------------------------------------


@test
def test_processors_analysis_and_reports():
    from ai import claude, segmentation
    from reports.dashboard import generate_dashboard
    from reports.facebook_report import format_facebook_text_report, generate_facebook_pdf

    assert claude.client is None, "the demo suite must run without an API key"
    out = SCRATCH / "reports"
    out.mkdir(exist_ok=True)
    names = {"facebook": "Facebook", "instagram": "Instagram", "tiktok": "TikTok",
             "linkedin": "LinkedIn", "x": "X", "threads": "Threads"}
    for platform, label in names.items():
        _, posts_doc, comments_doc = collect(platform)
        page_proc = importlib.import_module(f"processors.{platform}_page_processor")
        comm_proc = importlib.import_module(f"processors.{platform}_comments_processor")
        page_data = page_proc.process(posts_doc)
        comments_data = comm_proc.process(comments_doc)
        assert page_data["statistics"]["total_posts"] == 6, platform

        with no_network():
            page_analysis = claude.analyze_page(page_data)
            comment_analysis = claude.analyze_comments(comments_data)
            result = segmentation.segment(page_data, comments_data)
        assert page_analysis.executive_summary.startswith("[Demo analysis"), platform
        assert -1.0 <= comment_analysis.sentiment_score <= 1.0, platform
        assert result and result.topics and not result.unlabelled, platform
        segmentation.apply_labels(page_data, comments_data, result)

        text = format_facebook_text_report(page_data, page_analysis, comment_analysis,
                                           comments_data, platform=label,
                                           segmentation=result)
        assert "Lumen Coffee Co." in text, platform
        pdf = generate_facebook_pdf(page_data, page_analysis, comments_data,
                                    comment_analysis, out / f"{platform}.pdf",
                                    platform=label, segmentation=result)
        html = generate_dashboard(page_data, page_analysis, comments_data,
                                  comment_analysis, out / f"{platform}.html",
                                  platform=label, segmentation=result)
        assert Path(pdf).read_bytes().startswith(b"%PDF"), platform
        assert "Topic &amp; Sentiment" in Path(html).read_text(encoding="utf-8"), platform


@test
def test_offline_sentiment_lexicon():
    from ai.demo_analysis import sentiment_of, topic_of

    assert sentiment_of("This is the best cold brew in town")[0] == "Positive"
    assert sentiment_of("20 cents more for the same cup? Not great.")[0] == "Negative"
    assert sentiment_of("Is the oat milk sweetened?")[0] == "Neutral"
    assert topic_of("The app charged me twice") == "App & Loyalty"
    assert topic_of("أسعار القهوة ارتفعت") == "Pricing"


def main() -> int:
    failed = 0
    print(f"running {len(TESTS)} demo pipeline tests (scratch: {SCRATCH})\n")
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
    sys.exit(main())
