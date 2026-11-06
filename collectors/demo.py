"""Synthetic data for the GhostFetch demo build.

The public GhostFetch repository does not ship the real scrapers. Every
collector package under ``collectors/`` keeps its original public API, but
answers from this module instead of from a social network: it makes a small,
deterministic dataset for a FICTIONAL brand ("Lumen Coffee Co.") and writes it
in exactly the raw-JSON shape the real collector wrote, so the processors, the
Claude analysis and the report layer run unchanged on top of it.

Nothing here opens a socket. The field layout of each platform comes from the
templates in ``fixtures/demo/<platform>/``; the text, names, numbers and dates
are generated here, so posts and comments always join on ``post_url`` and the
date window is honoured.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import random
from datetime import datetime, timedelta, timezone

from .safe_write import write_json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# In the repository the templates live in fixtures/demo/; an Actor image has
# them vendored beside this file as demo_fixtures/ (see sync_collector.sh).
_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES_DIR = next((d for d in (os.path.join(ROOT, "fixtures", "demo"),
                                 os.path.join(_HERE, "demo_fixtures"))
                     if os.path.isdir(d)), os.path.join(ROOT, "fixtures", "demo"))

DEMO_NOTE = "Synthetic demo data: GhostFetch's real collectors are not published."
BRAND = "Lumen Coffee Co. (demo)"
DEFAULT_POSTS = 12
MAX_POSTS = 200
WINDOW_POSTS_PER_DAY = 0.5   # how dense a dated run with no count is

PLATFORM_NAMES = {
    "facebook": "Facebook", "instagram": "Instagram", "tiktok": "TikTok",
    "linkedin": "LinkedIn", "x": "X", "threads": "Threads",
}

# Platforms whose share metric is called something else in the raw document.
SHARE_KEYS = ("shares", "reposts", "repost_count")
ID_KEYS = ("post_id", "shortcode", "video_id", "tweet_id", "activity_id",
           "media_id", "activity_urn", "social_detail_urn", "feedback_id")

CAPTIONS = [
    "Our autumn blend is back: cinnamon, roasted pecan and a hint of maple.",
    "Behind the scenes at the roastery - every batch is cupped before it ships.",
    "New: oat-milk cold brew in every store from Monday.",
    "We are trialling compostable cups in three cities. Tell us what you think.",
    "Barista tip: grind just before you brew. Flavour fades within minutes.",
    "Happy hour is moving to 3-5pm on weekdays after your feedback.",
    "Meet Sara, who has run our downtown store for six years.",
    "The loyalty app update is live: stamps now carry over between stores.",
    "Single-origin spotlight: a washed Ethiopian with notes of jasmine and peach.",
    "Sorry for yesterday's app outage - orders are back to normal now.",
    "We now publish where every bean comes from, farm by farm.",
    "Weekend workshop: latte art for beginners. Ten seats left.",
    "Price update: some drinks go up by 20 cents next month. Here is why.",
    "Thank you for 100,000 followers! Free pastry with any drink on Friday.",
]

# (text, sentiment hint) - the hint only shapes like counts; the analysis
# itself is done by Claude (or the offline demo fallback) from the text.
COMMENTS = [
    ("This is the best cold brew in town, honestly.", "pos"),
    ("Love the new cups, finally something that doesn't fall apart.", "pos"),
    ("The app charged me twice last week and nobody replied to my email.", "neg"),
    ("Can you bring the autumn blend to the airport store?", "neu"),
    ("20 cents more for the same small cup? Not great.", "neg"),
    ("Sara is the reason I come every morning!", "pos"),
    ("Is the oat milk sweetened?", "neu"),
    ("Waited 25 minutes for a latte on Saturday. Please hire more staff.", "neg"),
    ("The jasmine notes are real. Bought two bags.", "pos"),
    ("Do the stamps carry over from the old app or do I start again?", "neu"),
    ("Loyalty update is great, thank you for listening.", "pos"),
    ("Workshop was fun, would book again.", "pos"),
    ("Why did happy hour move? 5-7 worked better for people with jobs.", "neg"),
    ("Do you ship beans internationally?", "neu"),
    ("Cups are compostable but the lids aren't?", "neg"),
    ("Best customer service I've had from a coffee chain.", "pos"),
    ("Please add a decaf option to the single-origin range.", "neu"),
    ("القهوة رائعة لكن الأسعار ارتفعت كثيراً", "neg"),
    ("أفضل قهوة مثلجة جربتها هذا الصيف", "pos"),
    ("هل يوجد فرع جديد في وسط المدينة؟", "neu"),
]

REPLIES = [
    "Agreed!", "Same experience here.", "They fixed it for me after a DM.",
    "Totally, the downtown store is the best.", "Not my experience, mine was fine.",
    "Thanks for the tip!", "Hi! Please send us a DM so we can sort this out. - Lumen team",
]

FIRST = ["Maya", "Omar", "Lena", "Jonas", "Aisha", "Theo", "Nadia", "Kai", "Rami",
         "Ines", "Sam", "Yara", "Leo", "Hana", "Ezra", "Mira", "Adam", "Noor"]
LAST = ["Rivers", "Haddad", "Novak", "Okafor", "Brandt", "Silva", "Mansour",
        "Lind", "Park", "Costa", "Ibrahim", "Weiss"]


# --- helpers ----------------------------------------------------------------


def _rng(*parts) -> random.Random:
    """A generator seeded from the inputs, so a run is reproducible."""
    seed = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(seed[:16], 16))


def _id(*parts, length=12) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:length]


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load(platform: str, name: str) -> dict:
    path = os.path.join(FIXTURES_DIR, platform, f"{platform}_{name}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _set(record: dict, key: str, value) -> None:
    """Overwrite a field only if the platform's template carries it."""
    if key in record:
        record[key] = value


def _slug(target) -> str:
    text = str(target or "lumen").strip().rstrip("/").split("/")[-1].lstrip("@#")
    return "".join(c for c in text if c.isalnum() or c in "-_.") or "lumen"


def target_url(platform: str, target) -> str:
    return f"https://example.com/demo/{platform}/{_slug(target)}"


def _post_template(platform: str) -> dict:
    posts = _load(platform, "posts_raw").get("posts") or []
    if not posts:  # the Facebook fixture's posts file is empty; use the combined one
        posts = _load(platform, "raw").get("posts") or []
    template = copy.deepcopy(posts[0])
    for key in ("comments", "comments_scraped", "replies_scraped"):
        template.pop(key, None)
    return template


def _comment_templates(platform: str) -> tuple[dict, dict, dict]:
    """(record template, comment template, reply template) for a platform."""
    records = _load(platform, "comments_raw").get("posts") or []
    record = next((r for r in records if r.get("comments")), records[0])
    comment = copy.deepcopy(record["comments"][0])
    replies = comment.get("reply_comments") or []
    reply = copy.deepcopy(replies[0]) if replies else copy.deepcopy(comment)
    record = copy.deepcopy(record)
    record["comments"] = []
    return record, comment, reply


# --- dates ------------------------------------------------------------------


def _span(window, count_hint: int | None) -> tuple[datetime, datetime]:
    """The time range the generated posts are spread across."""
    now = _now()
    start = getattr(window, "start", None) if window else None
    end = getattr(window, "end", None) if window else None
    end = min(end, now) if end else now
    if not start:
        start = end - timedelta(days=30)
    if start > end:
        start = end - timedelta(days=1)
    return start, end


def _post_count(num_posts, window, span) -> int:
    if num_posts is not None:
        return max(0, min(int(num_posts), MAX_POSTS))
    if window:
        days = max(1.0, (span[1] - span[0]).total_seconds() / 86400)
        return max(1, min(MAX_POSTS, round(days * WINDOW_POSTS_PER_DAY)))
    return DEFAULT_POSTS


# --- documents --------------------------------------------------------------


def page_information(platform: str, target, page_type: str | None = None) -> dict:
    template = _load(platform, "posts_raw").get("page_information") or {}
    info = copy.deepcopy(template)
    rng = _rng(platform, target, "page")
    handle = _slug(target)
    info.update({
        "page_name": BRAND,
        "page_url": target_url(platform, target),
        "followers_count": rng.randint(20_000, 250_000),
        "scrape_date": _iso(_now()),
    })
    _set(info, "username", handle)
    _set(info, "description", "Neighbourhood coffee roaster. Fictional brand used by the GhostFetch demo.")
    _set(info, "user_id", _id(platform, handle, length=10))
    _set(info, "avatar", None)
    if "company_details" in info:
        info["company_details"] = {
            "description": "A fictional coffee roaster used to demonstrate GhostFetch.",
            "website": "https://example.com/demo/lumen",
            "industry": "Food & Beverages",
            "company_size": "201-500 employees",
            "specialties": "Coffee roasting, retail, wholesale",
        }
    if page_type:
        info["page_type"] = page_type
    return info


def make_posts(platform: str, target, num_posts=None, window=None) -> list[dict]:
    """Generated posts, newest first, inside the window."""
    template = _post_template(platform)
    span = _span(window, num_posts)
    count = _post_count(num_posts, window, span)
    rng = _rng(platform, target, "posts")
    base = target_url(platform, target)
    total = (span[1] - span[0]).total_seconds()
    posts = []
    for i in range(count):
        offset = total * (i + rng.random() * 0.6) / max(count, 1)
        moment = span[1] - timedelta(seconds=min(offset, total))
        pid = _id(platform, target, i)
        likes = rng.randint(40, 4000)
        comments_count = rng.randint(3, 120)
        shares = rng.randint(0, 300)
        post = copy.deepcopy(template)
        post.update({
            "post_url": f"{base}/posts/{pid}",
            "post_date": _iso(moment),
            "caption": CAPTIONS[(i + rng.randint(0, 3)) % len(CAPTIONS)],
            "likes": likes,
            "comments_count": comments_count,
        })
        for key in ID_KEYS:
            _set(post, key, pid)
        for key in SHARE_KEYS:
            _set(post, key, shares)
        _set(post, "views", rng.randint(likes * 5, likes * 40) if "views" in post else None)
        _set(post, "media", [])
        _set(post, "author", BRAND)
        _set(post, "content_type", rng.choice(["photo", "video", "text"]))
        _set(post, "engagement_score", likes + 2 * comments_count + 3 * shares)
        if window and not window.contains(post["post_date"]):
            continue
        posts.append(post)
    if any("engagement_score" in p for p in posts):
        ranked = sorted(posts, key=lambda p: -(p.get("engagement_score") or 0))
        for rank, post in enumerate(ranked, 1):
            _set(post, "engagement_rank", rank)
    return posts


def _date_window(window, posts) -> dict | None:
    if not window:
        return None
    dates = sorted(p["post_date"] for p in posts if p.get("post_date"))
    return {
        "start": window.describe_start() if window.start else None,
        "end": window.describe_end() if window.end else None,
        "scanned_pages": max(1, len(posts) // 10 + 1),
        "oldest_seen": dates[0] if dates else None,
        "newest_seen": dates[-1] if dates else None,
        "reach_oldest": dates[0] if dates else None,
        "window_reached": True,
        "complete": True,
        "stop_reason": "window_cleared",
    }


def posts_document(platform: str, target, num_posts=None, window=None,
                   page_type: str | None = None) -> dict:
    posts = make_posts(platform, target, num_posts, window)
    doc = {
        "platform": PLATFORM_NAMES[platform],
        "scraper": "posts",
        "status": "success" if posts else "no_posts_found",
        "demo": True,
        "demo_note": DEMO_NOTE,
        "page_information": page_information(platform, target, page_type),
        "posts": posts,
    }
    block = _date_window(window, posts)
    if block:
        doc["date_window"] = block
    return doc


def _comment(platform, template, post, index, rng, parent=None) -> dict:
    text, hint = COMMENTS[rng.randrange(len(COMMENTS))] if parent is None else (
        REPLIES[rng.randrange(len(REPLIES))], "neu")
    handle = f"{rng.choice(FIRST).lower()}.{rng.choice(LAST).lower()}{rng.randint(1, 99)}"
    posted = datetime.fromisoformat(post["post_date"]) if post.get("post_date") else _now()
    moment = min(_now(), posted + timedelta(minutes=rng.randint(5, 60 * 48)))
    cid = _id(post.get("post_url"), parent, index)
    likes = rng.randint(0, 25) + (rng.randint(10, 300) if hint == "pos" else 0)
    comment = copy.deepcopy(template)
    comment.update({
        "username": handle,
        "profile_url": f"https://example.com/demo/{platform}/u/{handle}",
        "comment": text,
        "likes": likes,
        "replies": 0,
        "comment_date": _iso(moment),
        "reply_comments": [],
    })
    _set(comment, "comment_id", cid)
    _set(comment, "comment_url", f"{post['post_url']}?comment_id={cid}")
    _set(comment, "parent_comment_id", parent)
    _set(comment, "post_id", post.get("post_id") or _id(post.get("post_url")))
    _set(comment, "display_name", handle.split(".")[0].title())
    return comment


def _post_url(item) -> str:
    return item if isinstance(item, str) else (item or {}).get("post_url") or ""


def comment_record(platform: str, post, max_comments=20, max_replies=2) -> dict:
    """One post's comments, in the platform's raw comment-record shape."""
    record_t, comment_t, reply_t = _comment_templates(platform)
    if isinstance(post, str):
        post = {"post_url": post, "post_date": _iso(_now() - timedelta(days=3)),
                "caption": CAPTIONS[0], "likes": 120, "comments_count": 8}
    url = post["post_url"]
    rng = _rng(platform, url, "comments")
    limit = 20 if max_comments is None else max(0, int(max_comments))
    count = min(limit, rng.randint(4, 9))
    comments = []
    replies_total = 0
    for i in range(count):
        comment = _comment(platform, comment_t, post, i, rng)
        n_replies = min(max_replies, rng.choice([0, 0, 0, 1, 2]))
        for j in range(n_replies):
            reply = _comment(platform, reply_t, post, j, rng, parent=comment.get("comment_id") or str(i))
            reply.pop("reply_comments", None)
            comment["reply_comments"].append(reply)
        comment["replies"] = n_replies
        replies_total += n_replies
        comments.append(comment)

    record = copy.deepcopy(record_t)
    record.update({
        "post_url": url,
        "status": "success",
        "comments": comments,
        "comments_scraped": len(comments),
    })
    for key in ("post_date", "caption", "likes", "comments_count", "content_type", "views"):
        if key in post:
            _set(record, key, post[key])
    for key in ID_KEYS:
        _set(record, key, post.get(key) or _id(url))
    _set(record, "resolved_url", url)
    _set(record, "replies_scraped", replies_total)
    _set(record, "requested_comments", max_comments)
    _set(record, "reported_comment_count", post.get("comments_count", len(comments)))
    _set(record, "unique_comments", len(comments))
    _set(record, "collection_complete", True)
    _set(record, "stop_reason", "requested_met" if len(comments) >= limit else "source_exhausted")
    _set(record, "login_wall", False)
    _set(record, "authwall", False)
    return record


def comments_document(platform: str, posts, max_comments=20, max_replies=2) -> dict:
    """Comments for each post. ``posts`` may be post dicts or post URLs."""
    records = [comment_record(platform, p, max_comments, max_replies)
               for p in posts if _post_url(p)]
    return {
        "platform": PLATFORM_NAMES[platform],
        "scraper": "comments",
        "scrape_date": _iso(_now()),
        "demo": True,
        "demo_note": DEMO_NOTE,
        "posts": records,
    }


def combined_document(posts_doc: dict, comments_doc: dict) -> dict:
    """Page info + every post with its comments nested (the ``<p>_raw.json``)."""
    by_url = {r["post_url"]: r for r in comments_doc.get("posts") or []}
    posts = []
    for post in posts_doc.get("posts") or []:
        merged = copy.deepcopy(post)
        record = by_url.get(post.get("post_url")) or {}
        merged["comments"] = record.get("comments") or []
        merged["comments_scraped"] = record.get("comments_scraped", 0)
        merged["replies_scraped"] = record.get("replies_scraped", 0)
        posts.append(merged)
    return {
        "platform": posts_doc.get("platform"),
        "scraper": "combined",
        "scrape_date": comments_doc.get("scrape_date") or _iso(_now()),
        "demo": True,
        "page_information": posts_doc.get("page_information"),
        "posts": posts,
    }


def read_posts(path: str) -> dict | None:
    """The posts document a previous demo run wrote, if any."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def posts_for_urls(posts_path: str, urls) -> list:
    """Match URLs back to the posts written earlier, so comments carry dates."""
    doc = read_posts(posts_path) or {}
    known = {p.get("post_url"): p for p in doc.get("posts") or []}
    return [known.get(u, u) for u in urls]


def save(doc: dict, path: str) -> None:
    write_json(path, doc, indent=2, ensure_ascii=False)
