"""Public Facebook GROUPS collector (demo build).

Demo build: the real collector is not part of the public GhostFetch
repository. This package keeps its public API (``collect_group`` -> ``GroupRun``)
and answers with synthetic data from ``collectors/demo.py``. It writes no files,
like the real one.
"""

import re
from datetime import datetime, timezone

from .. import demo

SUCCESS = "success"
PARTIAL_SUCCESS = "partial_success"
NO_POSTS_FOUND = "no_posts_found"
WINDOW_NOT_REACHED = "window_not_reached"
INVALID_URL = "invalid_url"
NOT_A_GROUP = "not_a_group"
PRIVATE_GROUP = "private_group"
LOGIN_REQUIRED = "login_required"
GROUP_UNAVAILABLE = "group_unavailable"
UNEXPECTED_STRUCTURE = "unexpected_structure"
PAGINATION_FAILED = "pagination_failed"

FAILURE_STATUSES = frozenset({
    INVALID_URL, NOT_A_GROUP, PRIVATE_GROUP, LOGIN_REQUIRED,
    GROUP_UNAVAILABLE, UNEXPECTED_STRUCTURE, PAGINATION_FAILED, WINDOW_NOT_REACHED,
})

DEFAULT_MAX_POSTS = 1000
DEFAULT_MAX_COMMENTS_PER_POST = 20
DEFAULT_INCLUDE_REPLIES = False
SOURCE_TYPE = "facebook_group"

_GROUP = re.compile(r"(?:^|/)groups/([^/?#]+)")

__all__ = [
    "collect_group", "GroupRun", "GroupTargetError", "normalize_group_url",
    "DEFAULT_MAX_POSTS", "DEFAULT_MAX_COMMENTS_PER_POST",
    "DEFAULT_INCLUDE_REPLIES", "FAILURE_STATUSES",
    "SUCCESS", "PARTIAL_SUCCESS", "NO_POSTS_FOUND", "INVALID_URL",
    "NOT_A_GROUP", "PRIVATE_GROUP", "LOGIN_REQUIRED", "GROUP_UNAVAILABLE",
    "UNEXPECTED_STRUCTURE", "PAGINATION_FAILED", "WINDOW_NOT_REACHED",
]


class GroupTargetError(ValueError):
    """The target cannot be collected. Carries the status a caller reports."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class GroupRun:
    """One finished run: the caller-facing document plus the two raw documents
    the processors read."""

    def __init__(self, document, posts_doc, comments_doc, status):
        self.document = document
        self.posts_doc = posts_doc
        self.comments_doc = comments_doc
        self.status = status

    @property
    def ok(self):
        return self.status not in FAILURE_STATUSES


def normalize_group_url(value):
    """A group URL from a full URL, a bare id or a ``groups/<id>`` path."""
    text = str(value or "").strip().strip("/")
    if not text:
        raise GroupTargetError(INVALID_URL, "no group given")
    match = _GROUP.search(text)
    if match:
        return f"https://www.facebook.com/groups/{match.group(1)}"
    if "/" in text or "." in text:
        raise GroupTargetError(NOT_A_GROUP, f"{value!r} is not a Facebook group")
    return f"https://www.facebook.com/groups/{text}"


def collect_group(group_url, *, max_posts=DEFAULT_MAX_POSTS,
                  max_comments_per_post=DEFAULT_MAX_COMMENTS_PER_POST,
                  include_replies=DEFAULT_INCLUDE_REPLIES, window=None,
                  collect_comments=True, progress=None, batch_size=None,
                  on_batch=None):
    try:
        url = normalize_group_url(group_url)
    except GroupTargetError as exc:
        document = {"platform": "Facebook", "source_type": SOURCE_TYPE,
                    "status": exc.status, "error": str(exc), "authenticated": False,
                    "posts": [], "posts_found": 0, "posts_collected": 0,
                    "comments_collected": 0}
        return GroupRun(document, None, None, exc.status)

    posts_doc = demo.posts_document("facebook", url, max_posts, window, page_type="group")
    posts_doc["page_information"]["page_url"] = url
    posts = posts_doc["posts"]
    comments_doc = None
    comments_total = 0
    if collect_comments and posts:
        comments_doc = demo.comments_document(
            "facebook", posts, max_comments_per_post,
            max_replies=2 if include_replies else 0)
        by_url = {r["post_url"]: r for r in comments_doc["posts"]}
        for post in posts:
            post["comments"] = by_url.get(post["post_url"], {}).get("comments") or []
            comments_total += len(post["comments"])
    if on_batch and posts:
        size = batch_size or len(posts)
        for start in range(0, len(posts), size):
            on_batch(posts[start:start + size])
    if progress:
        progress(f"demo: {len(posts)} post(s), {comments_total} comment(s)")

    status = SUCCESS if posts else NO_POSTS_FOUND
    document = {
        "platform": "Facebook",
        "source_type": SOURCE_TYPE,
        "scraper": "public_groups",
        "status": status,
        "demo": True,
        "group_url": url,
        "scrape_date": datetime.now(timezone.utc).isoformat(),
        "authenticated": False,
        "posts_found": len(posts),
        "posts_collected": len(posts),
        "max_posts": max_posts,
        "limit_reached": max_posts is not None and len(posts) >= max_posts,
        "comments_collected": comments_total,
        "max_comments_per_post": max_comments_per_post,
        "include_replies": include_replies,
        "page_information": posts_doc["page_information"],
        "posts": posts,
    }
    if posts_doc.get("date_window"):
        document["date_window"] = posts_doc["date_window"]
    return GroupRun(document, posts_doc, comments_doc, status)
