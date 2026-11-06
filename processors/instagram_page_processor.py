"""Instagram profile/media performance processor.

Reads the raw post-scraper output (instagram_posts_raw.json), computes
profile-level and per-post statistics, ranks every post by engagement, and
writes a clean instagram_page_processed.json ready to be sent to the Claude API.

No AI is involved here - this only organizes and calculates data. The output
schema matches the Facebook page processor so the Claude analysis and the
report/dashboard layer work unchanged for both platforms.

Instagram note: the platform has no "share" count - the public equivalent is
REPOSTS (`media_repost_count`), so posts carry `reposts` rather than `shares`.
`views` only exists for video/reel content.
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
INPUT_FILE = os.path.join(DATA_DIR, "instagram_posts_raw.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "instagram_page_processed.json")

KNOWN_CONTENT_TYPES = ("image", "video", "reel", "carousel")


# ---------------------------------------------------------------------------
# Safe helpers
# ---------------------------------------------------------------------------

def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit(f"Input file not found: {path} - run the Instagram post scraper first.")
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
    if not isinstance(data, dict):
        return []
    posts = data.get("posts")
    if isinstance(posts, list):
        return [p for p in posts if isinstance(p, dict)]
    return []


def build_post(raw):
    """Normalize one raw post into the processed shape."""
    likes = as_int(raw.get("likes"))
    comments_count = as_int(raw.get("comments_count"))
    reposts = as_int(raw.get("reposts"))
    content_type = as_text(raw.get("content_type"), default="unknown") or "unknown"
    if content_type not in KNOWN_CONTENT_TYPES:
        content_type = "unknown"
    return {
        "post_url": as_text(raw.get("post_url")),
        "caption": as_text(raw.get("caption")),
        "post_date": as_text(raw.get("post_date")),
        "likes": likes,
        "reposts": reposts,
        "views": as_int(raw.get("views")),
        "comments_count": comments_count,
        "engagement_score": likes + comments_count + reposts,
        "content_type": content_type,
    }


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def average(posts, key):
    if not posts:
        return 0
    return round(sum(post[key] for post in posts) / len(posts), 2)


def build_posting_frequency(posts):
    """How often the profile posts, from the scraped posts' dates."""
    from datetime import datetime

    dates = []
    for post in posts:
        raw = (post.get("post_date") or "")[:10]
        try:
            dates.append(datetime.strptime(raw, "%Y-%m-%d"))
        except ValueError:
            continue
    if not dates:
        return {"first_post": "", "last_post": "", "days_covered": 0,
                "posts_per_week": 0, "posts_per_day": 0}
    first, last = min(dates), max(dates)
    days = max((last - first).days, 1)
    return {
        "first_post": first.strftime("%Y-%m-%d"),
        "last_post": last.strftime("%Y-%m-%d"),
        "days_covered": days,
        "posts_per_week": round(len(dates) / (days / 7), 2),
        "posts_per_day": round(len(dates) / days, 2),
    }


def build_statistics(posts):
    return {
        "total_posts": len(posts),
        "total_likes": sum(post["likes"] for post in posts),
        "total_comments": sum(post["comments_count"] for post in posts),
        "total_reposts": sum(post["reposts"] for post in posts),
        "total_views": sum(post["views"] for post in posts),
        "average_likes": average(posts, "likes"),
        "average_comments": average(posts, "comments_count"),
        "average_reposts": average(posts, "reposts"),
        "average_views": average(posts, "views"),
        "posting_frequency": build_posting_frequency(posts),
    }


def build_content_breakdown(posts):
    breakdown = {content_type: 0 for content_type in KNOWN_CONTENT_TYPES}
    breakdown["unknown"] = 0
    for post in posts:
        breakdown[post["content_type"]] += 1
    return breakdown


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

def process(data):
    page_info = data.get("page_information") if isinstance(data, dict) else None
    if not isinstance(page_info, dict):
        page_info = {}

    posts = [build_post(raw) for raw in get_raw_posts(data)]
    posts.sort(key=lambda post: post["engagement_score"], reverse=True)

    return {
        "page_name": as_text(page_info.get("page_name")),
        "username": as_text(page_info.get("username")),
        "followers_count": as_int(page_info.get("followers_count")),
        "scrape_date": as_text(page_info.get("scrape_date")),
        "statistics": build_statistics(posts),
        "content_breakdown": build_content_breakdown(posts),
        "posts": posts,
    }


def run():
    """Programmatic entry point used by main.py: process, save, and return."""
    data = load_json(INPUT_FILE)
    processed = process(data)
    save_json(OUTPUT_FILE, processed)
    return processed


def main():
    processed = run()
    print(f"Processed {processed['statistics']['total_posts']} post(s) "
          f"from '{processed['page_name']}' -> {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
