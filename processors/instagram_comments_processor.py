"""Instagram comments processor.

Reads the raw comment-scraper output (instagram_comments_raw.json) and reshapes
it into instagram_comments_processed.json, ready for AI sentiment and audience
analysis via the Claude API.

No AI is involved here - this only organizes the data. The output schema
matches the Facebook comments processor so the Claude analysis and the
report/dashboard layer work unchanged for both platforms.
"""

import json
import os
import sys

try:
    from collectors.safe_write import write_json
except ImportError:  # run as a script (python3 processors/<name>.py): add the repo root
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from collectors.safe_write import write_json

# All data files live in <project root>/data/instagram/, regardless of which
# directory the processor is launched from.
DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "instagram"
)
INPUT_FILE = os.path.join(DATA_DIR, "instagram_comments_raw.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "instagram_comments_processed.json")


# ---------------------------------------------------------------------------
# Safe helpers
# ---------------------------------------------------------------------------

def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit(f"Input file not found: {path} - run the Instagram comment scraper first.")
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


def build_post(raw):
    raw_comments = raw.get("comments")
    if not isinstance(raw_comments, list):
        raw_comments = []
    comments = [build_comment(c) for c in raw_comments if isinstance(c, dict)]
    total_replies = sum(len(c.get("reply_comments") or []) for c in comments)
    return {
        "post_url": as_text(raw.get("post_url")),
        "caption": as_text(raw.get("caption")),
        # Preserved so the report/dashboard can order the comment section by
        # post time (newest first).
        "post_date": as_text(raw.get("post_date")),
        "comments_count": as_int(raw.get("comments_count"), default=len(comments)),
        "top_level_comments": len(comments),
        "replies_scraped": total_replies,
        "comments": comments,
    }


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

def process(data):
    return {"posts": [build_post(raw) for raw in get_raw_posts(data)]}


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
