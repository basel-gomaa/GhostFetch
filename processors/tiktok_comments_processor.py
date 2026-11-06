"""TikTok comments processor.

Reads the raw comment-scraper output (tiktok_comments_raw.json) and reshapes it
into tiktok_comments_processed.json, ready for AI sentiment and audience analysis
via the Claude API.

No AI is involved here - this only organizes the data. The output schema matches
the Facebook/Instagram comments processors so the Claude analysis and the
report/dashboard layer work unchanged for all platforms.
"""

import json
import os
import sys

try:
    from collectors.safe_write import write_json
except ImportError:  # run as a script (python3 processors/<name>.py): add the repo root
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from collectors.safe_write import write_json

# All data files live in <project root>/data/tiktok/, regardless of which
# directory the processor is launched from.
DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tiktok"
)
INPUT_FILE = os.path.join(DATA_DIR, "tiktok_comments_raw.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "tiktok_comments_processed.json")
# The comment scraper drives the comment panel, which doesn't expose the video's
# caption/publish time, so those come out null. The posts scrape does capture
# them, so we backfill from its output (processed first, raw as a fallback),
# keyed by video id, so the report/dashboard can show captions and order the
# comment section by post date.
POSTS_PROCESSED_FILE = os.path.join(DATA_DIR, "tiktok_page_processed.json")
POSTS_RAW_FILE = os.path.join(DATA_DIR, "tiktok_posts_raw.json")


# ---------------------------------------------------------------------------
# Safe helpers
# ---------------------------------------------------------------------------

def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit(f"Input file not found: {path} - run the TikTok comment scraper first.")
    except json.JSONDecodeError as exc:
        sys.exit(f"Input file {path} is not valid JSON: {exc}")


def save_json(path, data):
    # 0644 and atomic, through the one shared writer (collectors/safe_write.py),
    # not the 0666 it used to leave so another OS user could overwrite it.
    write_json(path, data, ensure_ascii=False, indent=2)


def as_int(value, default=0):
    """Missing/None/malformed counts become a safe integer."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.replace(",", "")))
        except ValueError:
            return default
    return default


def as_text(value, default=""):
    return value if isinstance(value, str) else default


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def video_id_from_url(url):
    """The trailing /video/<id> segment uniquely identifies a TikTok post."""
    if not isinstance(url, str):
        return ""
    return url.rstrip("/").split("/")[-1].split("?")[0]


def load_json_optional(path):
    """Like load_json but returns None instead of exiting when absent/invalid -
    the caption/date backfill is best-effort and must never fail the pipeline."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def build_post_meta_lookup():
    """Map video id -> {caption, post_date} from the posts scrape, so comment
    posts (which lack both) can be enriched. Prefers the processed posts file
    and falls back to the raw one; returns {} if neither is available."""
    lookup = {}
    for path in (POSTS_PROCESSED_FILE, POSTS_RAW_FILE):
        data = load_json_optional(path)
        posts = data.get("posts") if isinstance(data, dict) else None
        if not isinstance(posts, list):
            continue
        for post in posts:
            if not isinstance(post, dict):
                continue
            vid = as_text(post.get("video_id")) or video_id_from_url(post.get("post_url"))
            if not vid or vid in lookup:
                continue
            lookup[vid] = {
                "caption": as_text(post.get("caption")),
                "post_date": as_text(post.get("post_date")),
            }
    return lookup


def get_raw_posts(data):
    """Return the raw post list, tolerating both scraper output shapes."""
    if not isinstance(data, dict):
        return []
    posts = data.get("posts")
    if isinstance(posts, list):
        return [p for p in posts if isinstance(p, dict)]
    single = data.get("post")
    if isinstance(single, dict):
        return [single]
    return []


def build_comment(raw, with_replies=True):
    comment = {
        "username": as_text(raw.get("username")),
        "comment": as_text(raw.get("comment")),
        "likes": as_int(raw.get("likes")),
        "replies": as_int(raw.get("replies")),
    }
    if with_replies:
        raw_replies = raw.get("reply_comments")
        if not isinstance(raw_replies, list):
            raw_replies = []
        # Replies are one level deep - never recurse into their own replies.
        comment["reply_comments"] = [
            build_comment(r, with_replies=False) for r in raw_replies if isinstance(r, dict)
        ]
    return comment


def build_post(raw, meta_lookup=None):
    raw_comments = raw.get("comments")
    if not isinstance(raw_comments, list):
        raw_comments = []
    comments = [build_comment(c) for c in raw_comments if isinstance(c, dict)]
    total_replies = sum(len(c.get("reply_comments") or []) for c in comments)
    post_url = as_text(raw.get("post_url"))
    # The comment scraper leaves caption/post_date null; backfill from the posts
    # scrape (keyed by video id) so captions render and the comment section can
    # be ordered by post date.
    meta = (meta_lookup or {}).get(video_id_from_url(post_url), {})
    caption = as_text(raw.get("caption")) or meta.get("caption", "")
    post_date = as_text(raw.get("post_date")) or meta.get("post_date", "")
    return {
        "post_url": post_url,
        "caption": caption,
        # Preserved so the report/dashboard can order the comment section by
        # post time (newest first).
        "post_date": post_date,
        "comments_count": as_int(raw.get("comments_count"), default=len(comments)),
        "top_level_comments": len(comments),
        "replies_scraped": total_replies,
        "comments": comments,
    }


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

def process(data):
    meta_lookup = build_post_meta_lookup()
    return {"posts": [build_post(raw, meta_lookup) for raw in get_raw_posts(data)]}


def run():
    """Programmatic entry point used by main.py: process, save, and return."""
    data = load_json(INPUT_FILE)
    processed = process(data)
    save_json(OUTPUT_FILE, processed)
    return processed


def main():
    processed = run()
    total_comments = sum(len(post["comments"]) for post in processed["posts"])
    total_replies = sum(post["replies_scraped"] for post in processed["posts"])
    print(f"Processed {len(processed['posts'])} post(s) with {total_comments} "
          f"comment(s) + {total_replies} repl(y/ies) -> {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
