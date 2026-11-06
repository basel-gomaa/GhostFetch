"""Offline stand-in for the Claude analysis (GhostFetch demo build).

When no ANTHROPIC_API_KEY is configured, ``ai/claude.py`` and
``ai/segmentation.py`` answer from here instead of calling the API, so the
demo pipeline produces a complete report with no network and no cost.

It is deliberately simple - keyword topics and a small sentiment lexicon - and
every summary it writes says that it is a demo analysis. With a key set, the
real Claude analysis runs on the same data instead.
"""

from __future__ import annotations

DEMO_PREFIX = "[Demo analysis - generated offline without Claude] "

TOPICS = [
    ("Pricing", "Price changes and value for money",
     ("price", "cents", "cost", "expensive", "أسعار")),
    ("App & Loyalty", "The mobile app, payments and loyalty stamps",
     ("app", "stamps", "loyalty", "charged", "order")),
    ("Service & Wait Times", "Staff, queues and customer service",
     ("wait", "waited", "staff", "service", "minutes", "barista", "dm")),
    ("Products & Flavour", "Blends, drinks and new menu items",
     ("blend", "brew", "jasmine", "oat", "decaf", "beans", "latte", "coffee",
      "pastry", "قهوة", "single-origin")),
    ("Sustainability", "Packaging, cups and sourcing",
     ("cup", "cups", "compostable", "lids", "farm", "sourcing")),
    ("Stores & Events", "Locations, opening hours, workshops and events",
     ("store", "stores", "workshop", "happy hour", "airport", "downtown", "فرع",
      "ship", "seats")),
]
FALLBACK_TOPIC = "Products & Flavour"

POSITIVE = ("best", "love", "great", "fun", "thank", "thanks", "agreed", "real",
            "finally", "رائعة", "أفضل")
NEGATIVE = ("twice", "nobody", "not great", "waited", "why", "aren't", "hire more",
            "outage", "sorry", "ارتفعت", "please")


def topic_of(text: str) -> str:
    low = (text or "").lower()
    best, hits = FALLBACK_TOPIC, 0
    for name, _, words in TOPICS:
        score = sum(1 for w in words if w in low)
        if score > hits:
            best, hits = name, score
    return best


def sentiment_of(text: str) -> tuple[str, float]:
    low = (text or "").lower()
    # A negated praise word is a complaint, not a mixed comment.
    for phrase in ("not great", "not good", "isn't great"):
        if phrase in low:
            low = low.replace(phrase, " nobody ")
    pos = sum(1 for w in POSITIVE if w in low)
    neg = sum(1 for w in NEGATIVE if w in low)
    if pos and neg:
        return "Mixed", 0.0
    if pos:
        return "Positive", min(1.0, 0.4 + 0.2 * pos)
    if neg:
        return "Negative", max(-1.0, -0.4 - 0.2 * neg)
    return "Neutral", 0.0


# --- segmentation -------------------------------------------------------------


def taxonomy() -> list[dict]:
    return [{"name": name, "description": desc} for name, desc, _ in TOPICS]


def label(items: list[dict]) -> dict:
    labels = {}
    for item in items:
        sentiment, score = sentiment_of(item.get("text", ""))
        labels[item["id"]] = {"topic": topic_of(item.get("text", "")),
                              "sentiment": sentiment,
                              "sentiment_score": round(score, 2)}
    return labels


# --- run-level analyses -------------------------------------------------------


def _all_comments(comments_data: dict) -> list[str]:
    texts = []
    for post in (comments_data or {}).get("posts") or []:
        for comment in post.get("comments") or []:
            texts.append(comment.get("comment") or "")
            texts.extend(r.get("comment") or "" for r in comment.get("reply_comments") or [])
    return [t for t in texts if t]


def _rating(score: float) -> str:
    if score > 0.5:
        return "Positive"
    if score > 0.15:
        return "Mostly Positive"
    if score > -0.15:
        return "Mixed"
    if score > -0.5:
        return "Mostly Negative"
    return "Negative"


def comment_analysis_fields(texts: list[str]) -> dict:
    """The CommentAnalysis fields for a list of comment texts."""
    scores: list[float] = []
    by_topic: dict[str, dict[str, int]] = {}
    for text in texts:
        sentiment, score = sentiment_of(text)
        scores.append(score)
        bucket = by_topic.setdefault(topic_of(text), {"Positive": 0, "Negative": 0})
        if sentiment in bucket:
            bucket[sentiment] += 1
    average = sum(scores) / len(scores) if scores else 0.0
    liked = sorted((t for t, b in by_topic.items() if b["Positive"] > b["Negative"]),
                   key=lambda t: -by_topic[t]["Positive"])
    disliked = sorted((t for t, b in by_topic.items() if b["Negative"] >= b["Positive"] and b["Negative"]),
                      key=lambda t: -by_topic[t]["Negative"])
    return {
        "executive_summary": (
            f"{DEMO_PREFIX}{len(texts)} comment(s) were scored with a keyword "
            f"lexicon. Overall tone is {_rating(average).lower()} "
            f"(score {average:+.2f}). Most praise is about "
            f"{', '.join(liked[:2]) or 'the products'}; most complaints are about "
            f"{', '.join(disliked[:2]) or 'nothing in particular'}."),
        "overall_sentiment": _rating(average),
        "sentiment_score": round(average, 2),
        "positive_topics": liked or ["Products & Flavour"],
        "negative_topics": disliked,
        "requested_features": [t for t in texts if t.rstrip().endswith("?")][:5],
        "recommendations": [
            f"Reply publicly to the {d.lower()} complaints and say what will change."
            for d in disliked[:3]] or ["Keep engaging with commenters' questions."],
        "content_reception": "Generated offline for the demo build.",
    }


def page_analysis_fields(page_data: dict) -> dict:
    posts = page_data.get("posts") or []
    stats = page_data.get("statistics") or {}
    top = sorted(posts, key=lambda p: -(p.get("likes") or 0))[:3]
    avg = stats.get("average_likes")
    return {
        "executive_summary": (
            f"{DEMO_PREFIX}{len(posts)} post(s) from {page_data.get('page_name')} "
            f"averaged {avg} likes. The strongest posts are product and community "
            f"stories; operational announcements (prices, outages) draw the most "
            f"critical replies."),
        "performance_rating": "Good",
        "communication_message": "Friendly, community-first coffee brand voice.",
        "content_overview": "Product launches, behind-the-scenes posts and store news.",
        "key_insights": [
            f"{len(posts)} posts analysed; average {avg} likes per post.",
            "Posts that introduce people (staff, farmers) out-perform announcements.",
            "Price and outage posts attract the most negative comments.",
        ],
        "best_performing_content": [
            (p.get("caption") or "")[:120] for p in top],
        "content_recommendations": [
            "Pair every price change with a clear explanation and a loyalty perk.",
            "Post more behind-the-scenes and staff stories.",
            "Answer recurring app questions in a pinned post.",
        ],
    }
