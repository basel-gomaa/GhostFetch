"""Instagram collector (demo build).

Demo build: the real Instagram collector is not part of the public GhostFetch
repository. This package keeps its public API and answers with synthetic
data from ``collectors/demo.py`` - it never contacts Instagram.
"""

from .. import demo
from ..daterange import resolve_count
from .auth import AuthError
from .common import (COMMENTS_OUTPUT_FILE, MAX_COMMENTS_PER_POST, MAX_POSTS_LIMIT,
                     NUM_POSTS, POSTS_OUTPUT_FILE, PROFILE_COMMENTS_PER_POST)

__all__ = ["collect_profile_posts", "collect_hashtag_posts", "collect_post_comments",
           "collect_profile_comments", "AuthError", "POSTS_OUTPUT_FILE",
           "COMMENTS_OUTPUT_FILE"]

PLATFORM = "instagram"


def _posts(target, num_posts, window):
    count = resolve_count(num_posts, window, NUM_POSTS, MAX_POSTS_LIMIT)
    doc = demo.posts_document(PLATFORM, target, count, window)
    demo.save(doc, POSTS_OUTPUT_FILE)
    return doc


def collect_profile_posts(profile, num_posts=NUM_POSTS, window=None):
    return _posts(profile, num_posts, window)


def collect_hashtag_posts(hashtag, num_posts=NUM_POSTS, window=None):
    return _posts("#" + str(hashtag).lstrip("#"), num_posts, window)


def collect_post_comments(post_url, max_comments=MAX_COMMENTS_PER_POST):
    return collect_profile_comments([post_url], max_comments)


def collect_profile_comments(post_urls, max_comments_per_post=PROFILE_COMMENTS_PER_POST):
    posts = demo.posts_for_urls(POSTS_OUTPUT_FILE, post_urls)
    doc = demo.comments_document(PLATFORM, posts, max_comments_per_post)
    demo.save(doc, COMMENTS_OUTPUT_FILE)
    return doc
