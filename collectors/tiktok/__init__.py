"""TikTok collector (demo build).

Demo build: the real TikTok collector is not part of the public GhostFetch
repository. This package keeps its public API and answers with synthetic
data from ``collectors/demo.py`` - it never contacts TikTok.
"""

from .. import demo
from ..daterange import resolve_count
from .auth import AuthError, BrowserUnavailable
from .common import (COMBINED_OUTPUT_FILE, COMMENTS_OUTPUT_FILE,
                     MAX_COMMENTS_PER_POST, MAX_POSTS_LIMIT, NUM_POSTS,
                     POSTS_OUTPUT_FILE, PROFILE_COMMENTS_PER_POST)

__all__ = ["collect_profile_posts", "collect_hashtag_posts", "collect_post_comments",
           "collect_profile_comments", "AuthError", "BrowserUnavailable",
           "PROFILE_COMMENTS_PER_POST", "POSTS_OUTPUT_FILE", "COMMENTS_OUTPUT_FILE"]

PLATFORM = "tiktok"
MAX_REPLIES_PER_COMMENT = 2


def _posts(target, num_posts, window):
    count = resolve_count(num_posts, window, NUM_POSTS, MAX_POSTS_LIMIT)
    doc = demo.posts_document(PLATFORM, target, count, window)
    demo.save(doc, POSTS_OUTPUT_FILE)
    return doc


def collect_profile_posts(profile, num_posts=NUM_POSTS, window=None, connection_id=None):
    return _posts(profile, num_posts, window)


def collect_hashtag_posts(hashtag, num_posts=NUM_POSTS, window=None, connection_id=None):
    return _posts("#" + str(hashtag).lstrip("#"), num_posts, window)


def collect_post_comments(video_url, max_comments=MAX_COMMENTS_PER_POST,
                          connection_id=None, max_replies_per_comment=None):
    return collect_profile_comments([video_url], max_comments,
                                    max_replies_per_comment=max_replies_per_comment)


def collect_profile_comments(video_urls, max_comments_per_post=PROFILE_COMMENTS_PER_POST,
                             connection_id=None, workers=None,
                             max_replies_per_comment=None):
    replies = MAX_REPLIES_PER_COMMENT if max_replies_per_comment is None else max_replies_per_comment
    posts = demo.posts_for_urls(POSTS_OUTPUT_FILE, video_urls)
    doc = demo.comments_document(PLATFORM, posts, max_comments_per_post, replies)
    demo.save(doc, COMMENTS_OUTPUT_FILE)
    posts_doc = demo.read_posts(POSTS_OUTPUT_FILE)
    if posts_doc:
        demo.save(demo.combined_document(posts_doc, doc), COMBINED_OUTPUT_FILE)
    return doc
