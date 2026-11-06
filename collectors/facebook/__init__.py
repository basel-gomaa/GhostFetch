"""Facebook collector (demo build).

Demo build: the real Facebook collector is not part of the public GhostFetch
repository. This package keeps its public API and answers with synthetic
data from ``collectors/demo.py`` - it never contacts Facebook.
"""

from .. import demo
from ..daterange import resolve_count
from .auth import AuthError
from .common import (COMBINED_OUTPUT_FILE, COMMENTS_OUTPUT_FILE,
                     MAX_COMMENTS_PER_POST, MAX_POSTS_LIMIT, NUM_POSTS,
                     PAGE_COMMENTS_PER_POST, POSTS_OUTPUT_FILE, is_group_url,
                     resolve_page_url)
from .pagination import COMMENT_SORT_CHOICES, DEFAULT_COMMENT_SORT

__all__ = ["collect_page_posts", "collect_post_comments", "collect_page_comments",
           "write_combined_raw", "AuthError", "POSTS_OUTPUT_FILE",
           "COMMENTS_OUTPUT_FILE", "COMBINED_OUTPUT_FILE",
           "DEFAULT_COMMENT_SORT", "COMMENT_SORT_CHOICES"]

PLATFORM = "facebook"


def collect_page_posts(page_name, num_posts=NUM_POSTS, window=None, concurrency=None):
    """Posts for a page or group. Writes POSTS_OUTPUT_FILE and returns it."""
    count = resolve_count(num_posts, window, NUM_POSTS, MAX_POSTS_LIMIT)
    url = resolve_page_url(page_name)
    page_type = "group" if is_group_url(url) else "page"
    doc = demo.posts_document(PLATFORM, page_name, count, window, page_type=page_type)
    doc["page_information"]["page_url"] = url
    demo.save(doc, POSTS_OUTPUT_FILE)
    return doc


def _comments(post_urls, max_comments, comment_sort):
    posts = demo.posts_for_urls(POSTS_OUTPUT_FILE, post_urls)
    doc = demo.comments_document(PLATFORM, posts, max_comments)
    for record in doc["posts"]:
        record["comment_sort"] = comment_sort or DEFAULT_COMMENT_SORT
    demo.save(doc, COMMENTS_OUTPUT_FILE)
    return doc


def collect_post_comments(post_url, max_comments=MAX_COMMENTS_PER_POST,
                          comment_sort=DEFAULT_COMMENT_SORT, window=None):
    return _comments([post_url], max_comments, comment_sort)


def collect_page_comments(post_urls, max_comments_per_post=PAGE_COMMENTS_PER_POST,
                          comment_sort=DEFAULT_COMMENT_SORT, window=None):
    doc = _comments(post_urls, max_comments_per_post, comment_sort)
    posts_doc = demo.read_posts(POSTS_OUTPUT_FILE)
    if posts_doc:
        write_combined_raw(posts_doc, doc)
    return doc


def write_combined_raw(posts_doc, comments_doc):
    combined = demo.combined_document(posts_doc, comments_doc)
    demo.save(combined, COMBINED_OUTPUT_FILE)
    return combined
