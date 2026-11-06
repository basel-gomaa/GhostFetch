"""Threads collector (demo build).

Demo build: the real Threads collector is not part of the public GhostFetch
repository. This package keeps its public API and answers with synthetic
data from ``collectors/demo.py`` - it never contacts Threads.
"""

from .. import demo
from ..daterange import resolve_count
from .auth import AuthError
from .common import (COMBINED_OUTPUT_FILE, COMMENTS_OUTPUT_FILE,
                     MAX_COMMENTS_PER_POST, MAX_POSTS_LIMIT, NUM_POSTS,
                     POSTS_OUTPUT_FILE, PROFILE_COMMENTS_PER_POST)

__all__ = ["collect_profile_posts", "collect_tag_posts", "collect_post_comments", "collect_feed_comments",
           "write_combined_raw", "AuthError", "POSTS_OUTPUT_FILE",
           "COMMENTS_OUTPUT_FILE", "COMBINED_OUTPUT_FILE"]

PLATFORM = "threads"


def _posts(target, num_posts, window):
    count = resolve_count(num_posts, window, NUM_POSTS, MAX_POSTS_LIMIT)
    doc = demo.posts_document(PLATFORM, target, count, window)
    demo.save(doc, POSTS_OUTPUT_FILE)
    return doc



def collect_profile_posts(profile, num_posts=NUM_POSTS, window=None):
    return _posts(profile, num_posts, window)


def collect_tag_posts(tag, num_posts=NUM_POSTS, window=None):
    return _posts("#" + str(tag).lstrip("#"), num_posts, window)


def collect_post_comments(post_url, max_comments=MAX_COMMENTS_PER_POST):
    return collect_feed_comments([post_url], max_comments)


def collect_feed_comments(posts, max_comments_per_post=PROFILE_COMMENTS_PER_POST):
    """Comments for each post. ``posts`` may be post dicts or post URLs."""
    urls = [p if isinstance(p, str) else p.get("post_url") for p in posts or []]
    doc = demo.comments_document(PLATFORM, demo.posts_for_urls(POSTS_OUTPUT_FILE, urls),
                                 max_comments_per_post)
    demo.save(doc, COMMENTS_OUTPUT_FILE)
    posts_doc = demo.read_posts(POSTS_OUTPUT_FILE)
    if posts_doc:
        write_combined_raw(posts_doc, doc)
    return doc


def write_combined_raw(posts_doc, comments_doc):
    combined = demo.combined_document(posts_doc, comments_doc)
    demo.save(combined, COMBINED_OUTPUT_FILE)
    return combined
