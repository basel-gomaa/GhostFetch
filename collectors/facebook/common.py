"""Paths, limits and URL helpers for the Facebook demo collector.

Demo build: the real Facebook collector is not part of the public GhostFetch
repository. This package keeps its public API and answers with synthetic
data from ``collectors/demo.py`` - it never contacts Facebook.
"""

import os
import re
from urllib.parse import urlparse

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Read at import time, like the real collector.
DATA_DIR = os.environ.get("FB_DATA_DIR") or os.path.join(_ROOT, "data", "facebook")
STORAGE_STATE_FILE = os.path.join(DATA_DIR, "fb_auth.json")
POSTS_OUTPUT_FILE = os.path.join(DATA_DIR, "facebook_posts_raw.json")
COMMENTS_OUTPUT_FILE = os.path.join(DATA_DIR, "facebook_comments_raw.json")
COMBINED_OUTPUT_FILE = os.path.join(DATA_DIR, "facebook_raw.json")

NUM_POSTS = 50
MAX_POSTS_LIMIT = 1000
MAX_COMMENTS_PER_POST = 1000
PAGE_COMMENTS_PER_POST = 1000
MAX_PAGE_COMMENTS = 1000
HEADLESS = True

_GROUP = re.compile(r"(?:^|/)groups/([^/?#]+)")


def resolve_page_url(value):
    """A page URL from a name, a path or a full URL."""
    text = str(value or "").strip()
    if urlparse(text).scheme:
        return text
    return f"https://www.facebook.com/{text.strip('/')}"


def is_group_url(url):
    """Is this a Facebook GROUP target (feed or post)?"""
    return bool(_GROUP.search(str(url or "")))


def group_id(url):
    """The group's own id/vanity from any group URL, or None."""
    match = _GROUP.search(str(url or ""))
    return match.group(1) if match else None


def normalize_group_target(value):
    """Accept a group id, a ``groups/<id>`` path or a full URL -> a group URL."""
    text = str(value or "").strip().strip("/")
    if is_group_url(text):
        return resolve_page_url(text)
    return f"https://www.facebook.com/groups/{text}"


def save_output(path, output):
    from ..safe_write import write_json
    write_json(path, output, indent=2, ensure_ascii=False)
