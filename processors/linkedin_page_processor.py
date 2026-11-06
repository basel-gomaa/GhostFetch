"""LinkedIn company/profile/hashtag performance processor.

Reads the raw post-scraper output (linkedin_posts_raw.json), computes page-level
and per-post statistics, ranks every post by engagement, and writes a clean
linkedin_page_processed.json ready to be sent to the Claude API.

No AI is involved here - this only organizes and calculates data. The output
schema matches the Facebook/Instagram/TikTok page processors so the Claude
analysis and the report/dashboard layer work unchanged for all platforms.

LinkedIn note: posts carry a real "share" count (numShares), so they use
`shares` (like Facebook/TikTok) rather than `reposts`. Content types are text,
image, video, article, document, or shared_post; `views` is only present on
video posts (0 otherwise).
"""

import json
import os
import sys

try:
    from collectors.safe_write import write_json
except ImportError:  # run as a script (python3 processors/<name>.py): add the repo root
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from collectors.safe_write import write_json

# All data files live in <project root>/data/linkedin/, regardless of which
# directory the processor is launched from.
DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "linkedin"
)
INPUT_FILE = os.path.join(DATA_DIR, "linkedin_posts_raw.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "linkedin_page_processed.json")

KNOWN_CONTENT_TYPES = ("text", "image", "video", "article", "document", "shared_post")


# ---------------------------------------------------------------------------
# Safe helpers
# ---------------------------------------------------------------------------

def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit(f"Input file not found: {path} - run the LinkedIn post scraper first.")
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
    shares = as_int(raw.get("shares"))
    content_type = as_text(raw.get("content_type"), default="unknown") or "unknown"
    if content_type not in KNOWN_CONTENT_TYPES:
        content_type = "unknown"
    return {
        "post_url": as_text(raw.get("post_url")),
        "caption": as_text(raw.get("caption")),
        "post_date": as_text(raw.get("post_date")),
        "likes": likes,
        "shares": shares,
        "views": as_int(raw.get("views")),
        "comments_count": comments_count,
        "engagement_score": likes + comments_count + shares,
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
    """How often the page posts, from the scraped posts' dates."""
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
        "total_shares": sum(post["shares"] for post in posts),
        "total_views": sum(post["views"] for post in posts),
        "average_likes": average(posts, "likes"),
        "average_comments": average(posts, "comments_count"),
        "average_shares": average(posts, "shares"),
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

    processed = {
        "page_name": as_text(page_info.get("page_name")),
        "username": as_text(page_info.get("username")),
        "followers_count": as_int(page_info.get("followers_count")),
        "scrape_date": as_text(page_info.get("scrape_date")),
        "statistics": build_statistics(posts),
        "content_breakdown": build_content_breakdown(posts),
        "posts": posts,
    }
    # LinkedIn company scrapes carry an About block (description, industry, size,
    # HQ, specialties, member counts). Pass it through when present so the report
    # / dashboard can surface it; absent for other platforms (additive).
    company_details = page_info.get("company_details")
    if isinstance(company_details, dict) and company_details:
        processed["company_details"] = company_details
    return processed


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
