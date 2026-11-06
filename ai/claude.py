"""
ai/claude.py

Responsibility: turn raw comment text into a structured analysis using
the Claude API. This module knows nothing about YouTube, HTTP APIs, or
report formatting -- it receives plain data in, and returns a plain
dataclass out. That's what lets the exact same function analyze
comments collected from LinkedIn or TikTok later with zero changes here.
"""

from __future__ import annotations

import json
import re
from typing import Any, cast
from dataclasses import dataclass, field

import anthropic
from anthropic.types import ToolParam

from config import ANTHROPIC_API_KEY

# Model choice: Sonnet is the right default for this kind of qualitative
# analysis -- strong reasoning over messy, informal text (YouTube comments)
# at a reasonable cost/latency for a prototype. If this becomes a
# high-volume production job, this is the one line you'd swap to a
# cheaper/faster model after evaluating quality.
MODEL = "claude-sonnet-5"

# Demo build: with no API key the analyses fall back to ai/demo_analysis.py,
# an offline keyword analysis, so the demo pipeline runs without network.
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
OFFLINE = client is None

# Output token budget for the structured analyses. The reports are small, but
# 2000 was tight enough that large comment sets truncated the JSON mid-string.
ANALYSIS_MAX_TOKENS = 8000


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------
# Same idea as the Video/Comment dataclasses in collectors/youtube.py:
# a single, explicit definition of "what a report looks like" that
# reports/ and main.py can rely on without caring how it was produced.

@dataclass
class CommentAnalysis:
    executive_summary: str
    overall_sentiment: str  # e.g. "Positive", "Mixed", "Negative"
    sentiment_score: float  # -1.0 (very negative) to 1.0 (very positive)
    positive_topics: list[str] = field(default_factory=list)
    negative_topics: list[str] = field(default_factory=list)
    requested_features: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    # How the page's content is received by commenters (Facebook analyzer
    # fills this; the YouTube analyzer leaves it empty).
    content_reception: str = ""


# ---------------------------------------------------------------------------
# Structured output via forced tool use
# ---------------------------------------------------------------------------
# Asking Claude to "please return JSON" in the prompt works most of the time,
# but a prototype that's meant to feed a report generator needs *reliable*
# structure. Forcing a tool call is the robust pattern: we describe the
# exact shape we want as a tool schema, force Claude to call it, and read
# the arguments straight out of the tool_use block -- no fragile string
# parsing of maybe-JSON.

REPORT_TOOL: dict[str, Any] = {
    "name": "submit_comment_analysis",
    "description": "Submit a structured executive analysis of the provided social media comments.",
    "input_schema": {
        "type": "object",
        "properties": {
            "executive_summary": {
                "type": "string",
                "description": "2-4 sentence summary written for a CEO/executive audience.",
            },
            "overall_sentiment": {
                "type": "string",
                "enum": ["Positive", "Mostly Positive", "Mixed", "Mostly Negative", "Negative"],
            },
            "sentiment_score": {
                "type": "number",
                "description": "-1.0 (very negative) to 1.0 (very positive).",
            },
            "positive_topics": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Themes viewers responded positively to.",
            },
            "negative_topics": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Themes viewers responded negatively to or complained about.",
            },
            "requested_features": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific features, content, or changes commenters asked for.",
            },
            "recommendations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Concrete, actionable recommendations for the business.",
            },
        },
        "required": [
            "executive_summary",
            "overall_sentiment",
            "sentiment_score",
            "positive_topics",
            "negative_topics",
            "requested_features",
            "recommendations",
        ],
    },
}

SYSTEM_PROMPT = (
    "You are a social media analyst preparing a concise, honest brief for "
    "a company executive. Base every claim strictly on the comments "
    "provided. If the comments are too sparse or too vague to support a "
    "field, say so plainly (e.g. 'insufficient data') rather than "
    "inventing detail. Do not soften negative feedback -- executives need "
    "the real signal."
)


def _format_comments_for_prompt(videos) -> str:
    """
    Flatten Video/Comment objects (from collectors/youtube.py) into a
    plain-text block Claude can read. Kept as a private helper so the
    public analyze_youtube_comments() function stays the one thing other modules
    import and call.
    """
    lines = []
    for video in videos:
        lines.append(f"\n=== Video: {video.title} (id={video.video_id}) ===")
        if not video.comments:
            lines.append("(no comments collected)")
            continue
        for c in video.comments:
            lines.append(f"- [{c.like_count} likes] {c.author}: {c.text}")
    return "\n".join(lines)


# Observed failure mode: the model occasionally wraps a field's JSON
# array in what looks like an internal tool-parameter tag instead of
# returning a plain array, e.g. '\n<parameter name="items">["a", "b"]'.
# When that happens the real list is still there, just wrapped -- strip
# the tag and parse the JSON behind it before falling back to treating
# the whole string as one opaque item.
_PARAM_TAG_RE = re.compile(r'^\s*<parameter\b[^>]*>\s*')


def _as_list(value) -> list[str]:
    """
    Guarantee a list[str] no matter what came back from the model.

    Forcing tool use makes malformed output rare, but not impossible --
    if a "list" field ever comes back as a bare string, iterating over it
    later (in reports/) would silently split it into one bullet per
    character instead of one bullet per item. Normalizing here means
    that failure mode can never reach the report.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        unwrapped = _PARAM_TAG_RE.sub("", value, count=1)
        try:
            parsed = json.loads(unwrapped)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            return [str(v) for v in parsed]
    # A single string came back where a list was expected -- treat it
    # as one item rather than iterating its characters.
    return [str(value)]


_LIST_FIELDS = [
    "positive_topics",
    "negative_topics",
    "requested_features",
    "recommendations",
]

# Second observed failure mode: instead of filling all four array fields,
# the model sometimes jams every field's content into just the first one
# as pseudo-XML -- <item>...</item> entries separated by boundary tags
# named after the other fields (e.g. "</positive_topics><negative_topics>")
# -- and omits the other three fields from the tool call entirely. The
# data isn't lost, just misplaced, so it's recovered by re-splitting that
# blob on the boundary tags rather than discarded as a single bullet.
_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.DOTALL)
_SECTION_TAG_RE = re.compile(
    r'<\s*/?\s*(' + "|".join(_LIST_FIELDS) + r')\b[^>]*>'
)


def _recover_jammed_sections(blob: str) -> dict[str, list[str]]:
    """
    Split a single blob containing all four sections' <item> entries
    back into per-field lists, using the nearest preceding boundary tag
    to decide which field each item belongs to (items before the first
    boundary belong to positive_topics, the implicit first section).
    """
    boundaries = [(m.start(), m.group(1)) for m in _SECTION_TAG_RE.finditer(blob)]
    recovered: dict[str, list[str]] = {name: [] for name in _LIST_FIELDS}
    for item_match in _ITEM_RE.finditer(blob):
        pos = item_match.start()
        current_field = _LIST_FIELDS[0]
        for b_pos, b_name in boundaries:
            if b_pos >= pos:
                break
            current_field = b_name
        text = item_match.group(1).strip()
        if text:
            recovered[current_field].append(text)
    return recovered


def _extract_list_fields(data: dict) -> dict[str, list[str]]:
    """
    Pull the four array fields out of the tool call, recovering from
    either malformed shape seen from the model: a field individually
    wrapped in an internal tool-parameter tag (handled by _as_list), or
    all four fields jammed into one as tagged text (handled here).
    """
    blob = next(
        (
            data[name]
            for name in _LIST_FIELDS
            if isinstance(data.get(name), str) and "<item>" in data[name]
        ),
        None,
    )
    if blob is not None and _SECTION_TAG_RE.search(blob):
        return _recover_jammed_sections(blob)
    return {name: _as_list(data.get(name)) for name in _LIST_FIELDS}


# ---------------------------------------------------------------------------
# Facebook: page-performance analysis
# ---------------------------------------------------------------------------
# The Facebook analyzers use structured outputs (output_config.format) instead
# of forced tool use: the API guarantees the response text is JSON valid
# against the schema, so none of the malformed-tool-call recovery machinery
# above is needed on this path.

@dataclass
class PageAnalysis:
    executive_summary: str
    performance_rating: str  # "Excellent" ... "Poor"
    communication_message: str = ""   # the page's voice/tone and core message
    content_overview: str = ""        # what the channel's content is about
    key_insights: list[str] = field(default_factory=list)
    best_performing_content: list[str] = field(default_factory=list)
    content_recommendations: list[str] = field(default_factory=list)


_PAGE_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "executive_summary": {
            "type": "string",
            "description": "2-4 sentence summary of the page's performance for a CEO/executive audience.",
        },
        "performance_rating": {
            "type": "string",
            "enum": ["Excellent", "Good", "Average", "Below Average", "Poor"],
        },
        "communication_message": {
            "type": "string",
            "description": "The page's communication style and core message: "
                           "tone of voice, framing, and what it is trying to "
                           "convey to its audience (2-3 sentences).",
        },
        "content_overview": {
            "type": "string",
            "description": "What the channel's content is about: main themes, "
                           "topics, and formats (2-3 sentences).",
        },
        "key_insights": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The most important patterns in the page's performance data.",
        },
        "best_performing_content": {
            "type": "array",
            "items": {"type": "string"},
            "description": "What the top posts have in common: themes, content types, tone.",
        },
        "content_recommendations": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete, actionable recommendations to improve page performance.",
        },
    },
    "required": [
        "executive_summary",
        "performance_rating",
        "communication_message",
        "content_overview",
        "key_insights",
        "best_performing_content",
        "content_recommendations",
    ],
    "additionalProperties": False,
}

_COMMENT_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        **REPORT_TOOL["input_schema"]["properties"],
        "content_reception": {
            "type": "string",
            "description": "How the page's content is received by the "
                           "commenters: do they engage with the actual "
                           "content, praise it, mock it, ignore it? "
                           "(2-3 sentences).",
        },
    },
    "required": REPORT_TOOL["input_schema"]["required"] + ["content_reception"],
    "additionalProperties": False,
}


def _parse_structured_json(response) -> dict:
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to analyze this content.")
    text = next(block.text for block in response.content if block.type == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if response.stop_reason == "max_tokens":
            raise RuntimeError(
                "Claude's analysis was cut off (hit the output token limit). "
                "This can happen with very large comment sets - the report "
                "prompt is already capped, so if you see this, raise "
                "ANALYSIS_MAX_TOKENS in ai/claude.py."
            )
        raise


def _format_page_for_prompt(page_data: dict) -> str:
    stats = page_data.get("statistics") or {}
    # Instagram has no "shares" - its share-equivalent metric is REPOSTS, and
    # the Instagram processor emits it under its own keys. Describe whichever
    # the data actually carries so the model never reports a missing metric.
    if "total_reposts" in stats or any("reposts" in p for p in page_data.get("posts") or []):
        share_key, share_avg_key, share_word = "reposts", "average_reposts", "reposts"
    else:
        share_key, share_avg_key, share_word = "shares", "average_shares", "shares"
    breakdown = page_data.get("content_breakdown") or {}
    lines = [
        f"Page: {page_data.get('page_name')}",
        f"Followers: {page_data.get('followers_count')}",
        f"Scrape date: {page_data.get('scrape_date')}",
        f"Posts analyzed: {stats.get('total_posts')}",
        (
            f"Averages per post: {stats.get('average_likes')} likes, "
            f"{stats.get('average_comments')} comments, "
            f"{stats.get(share_avg_key)} {share_word}, {stats.get('average_views')} views"
        ),
        "Content mix: " + (", ".join(f"{k}={v}" for k, v in breakdown.items() if v) or "unknown"),
        "",
        f"Posts, ranked by engagement (likes + comments + {share_word}):",
    ]
    for i, post in enumerate(page_data.get("posts") or [], start=1):
        caption = (post.get("caption") or "(no caption)").replace("\n", " ")[:200]
        date = str(post.get("post_date") or "")[:10]
        lines.append(
            f"{i}. [{post.get('content_type')}] {date} - "
            f"{post.get('likes')} likes, {post.get('comments_count')} comments, "
            f"{post.get(share_key)} {share_word}, {post.get('views')} views - {caption}"
        )
    return "\n".join(lines)



def _with_focus(instruction: str, extra: str) -> str:
    """Append the caller's own analysis instructions to a built-in one.

    WHY APPENDED RATHER THAN SUBSTITUTED: the response is constrained by a
    json_schema, and the report/PDF/dashboard layer renders those exact fields.
    Replacing the instruction would still have to produce that shape, so the
    only thing a free-form prompt could change is the CONTENT of fields it did
    not know it had to fill -- which reads as an ignored prompt. Appending gives
    the user real influence over emphasis while the report always renders.

    DELIMITED AND LABELLED as coming from the user. The data blocks below this
    are scraped captions and comments written by strangers, and the model has to
    be able to tell the operator's instructions from a comment that says "ignore
    your instructions". This does not make injection impossible -- nothing in a
    single prompt does -- but an unlabelled concatenation makes it easy.
    """
    extra = (extra or "").strip()
    if not extra:
        return instruction
    return (
        instruction
        + "\n\nThe person requesting this report added the following "
        "instructions. Follow them where they affect emphasis, focus or tone, "
        "but keep every field of the required output populated:\n"
        "<user_instructions>\n" + extra + "\n</user_instructions>"
    )


def analyze_page(page_data: dict, extra_instructions: str = "") -> PageAnalysis:
    """
    Analyze page/media performance. `page_data` is the dict produced by
    processors/facebook_page_processor.py (facebook_page_processed.json).

    `extra_instructions` is the caller's own analysis brief, appended to the
    built-in one. Defaults to "", which produces the identical request this made
    before the parameter existed.
    """
    if not (page_data.get("posts") or []):
        raise ValueError("No post data to analyze -- did the post scrape succeed?")
    if client is None:
        from . import demo_analysis
        return PageAnalysis(**demo_analysis.page_analysis_fields(page_data))

    response = client.messages.create(
        model=MODEL,
        max_tokens=ANALYSIS_MAX_TOKENS,
        system=SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": _PAGE_ANALYSIS_SCHEMA}},
        messages=[
            {
                "role": "user",
                "content": (
                    _with_focus(
                        "Analyze this Facebook page's performance data and "
                        "produce an executive report.",
                        extra_instructions,
                    )
                    + "\n\n" + _format_page_for_prompt(page_data)
                ),
            }
        ],
    )
    data = _parse_structured_json(response)
    return PageAnalysis(
        executive_summary=data["executive_summary"],
        performance_rating=data["performance_rating"],
        communication_message=data.get("communication_message", ""),
        content_overview=data.get("content_overview", ""),
        key_insights=[str(v) for v in data["key_insights"]],
        best_performing_content=[str(v) for v in data["best_performing_content"]],
        content_recommendations=[str(v) for v in data["content_recommendations"]],
    )


# ---------------------------------------------------------------------------
# Facebook: comment sentiment / audience analysis
# ---------------------------------------------------------------------------

# A page run can scrape tens of thousands of comments; sending them all to
# Claude would blow the context window, cost a fortune, and push the model to
# over-generate (which truncated the JSON). Cap the sample that goes INTO the
# prompt - all comments still appear in the report/raw data. Highest-liked
# comments per post are kept first, since they carry the strongest signal.
MAX_COMMENTS_IN_PROMPT = 1500


def _format_facebook_comments_for_prompt(comments_data: dict) -> str:
    posts = comments_data.get("posts") or []
    total = sum(len(p.get("comments") or []) for p in posts)
    # Per-post budget so no single viral post crowds out the others.
    per_post_cap = max(1, MAX_COMMENTS_IN_PROMPT // max(len(posts), 1))

    lines = []
    if total > MAX_COMMENTS_IN_PROMPT:
        lines.append(f"(Analyzing a sample of the {total} scraped comments - "
                     f"up to {per_post_cap} highest-liked per post.)")
    for post in posts:
        caption = (post.get("caption") or "(no caption)").replace("\n", " ")[:300]
        comments = post.get("comments") or []
        lines.append(f"\n=== Post: {caption} "
                     f"({post.get('comments_count')} total comments) ===")
        if not comments:
            lines.append("(no comments collected)")
            continue
        sample = sorted(comments, key=lambda c: c.get("likes") or 0,
                        reverse=True)[:per_post_cap]
        for c in sample:
            text = (c.get("comment") or "").replace("\n", " ")
            lines.append(f"- [{c.get('likes') or 0} likes] {c.get('username')}: {text}")
            for r in c.get("reply_comments") or []:
                rtext = (r.get("comment") or "").replace("\n", " ")
                lines.append(f"    > reply [{r.get('likes') or 0} likes] "
                             f"{r.get('username')}: {rtext}")
    return "\n".join(lines)


def analyze_facebook_comments(
    comments_data: dict, extra_instructions: str = ""
) -> CommentAnalysis:
    """
    Analyze Facebook comments for sentiment and audience signals.
    `comments_data` is the dict produced by
    processors/facebook_comments_processor.py (facebook_comments_processed.json).
    Returns the same CommentAnalysis shape as the YouTube analyzer, so the
    report layer works identically for both platforms.
    """
    comment_block = _format_facebook_comments_for_prompt(comments_data)
    if not comment_block.strip():
        raise ValueError("No comment data to analyze -- did the comment scrape succeed?")
    if client is None:
        from . import demo_analysis
        return CommentAnalysis(**demo_analysis.comment_analysis_fields(
            demo_analysis._all_comments(comments_data)))

    response = client.messages.create(
        model=MODEL,
        max_tokens=ANALYSIS_MAX_TOKENS,
        system=SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": _COMMENT_ANALYSIS_SCHEMA}},
        messages=[
            {
                "role": "user",
                "content": (
                    _with_focus(
                        "Analyze the following Facebook comments and produce an "
                        "executive audience/sentiment report. Comments may be in "
                        "Arabic or English; analyze them in whatever language "
                        "they are written, but write the report in English.",
                        extra_instructions,
                    )
                    + "\n"
                    + comment_block
                ),
            }
        ],
    )
    data = _parse_structured_json(response)
    return CommentAnalysis(
        executive_summary=data["executive_summary"],
        overall_sentiment=data["overall_sentiment"],
        sentiment_score=float(data["sentiment_score"]),
        positive_topics=[str(v) for v in data["positive_topics"]],
        negative_topics=[str(v) for v in data["negative_topics"]],
        requested_features=[str(v) for v in data["requested_features"]],
        recommendations=[str(v) for v in data["recommendations"]],
        content_reception=data.get("content_reception", ""),
    )


def analyze_youtube_comments(videos) -> CommentAnalysis:
    """
    Send collected videos+comments to Claude and return a structured
    CommentAnalysis. `videos` is a list[Video] as produced by
    collectors.youtube.collect_channel_data().
    """
    comment_block = _format_comments_for_prompt(videos)

    if not comment_block.strip():
        raise ValueError("No comment data to analyze -- did collection succeed?")
    if client is None:
        from . import demo_analysis
        fields = demo_analysis.comment_analysis_fields(
            [c.text for v in videos for c in v.comments])
        fields["content_reception"] = ""
        return CommentAnalysis(**fields)

    message = client.messages.create(
        model=MODEL,
        max_tokens=ANALYSIS_MAX_TOKENS,
        system=SYSTEM_PROMPT,
        tools=[cast(ToolParam, REPORT_TOOL)],
        tool_choice={"type": "tool", "name": "submit_comment_analysis"},
        messages=[
            {
                "role": "user",
                "content": (
                    "Analyze the following YouTube comments and submit your "
                    "findings using the submit_comment_analysis tool.\n"
                    f"{comment_block}"
                ),
            }
        ],
    )

    tool_use_block = next(
        block for block in message.content if block.type == "tool_use"
    )
    # The tool's input_schema makes this a JSON object; the SDK types it as object.
    data = cast(dict[str, Any], tool_use_block.input)
    list_fields = _extract_list_fields(data)

    return CommentAnalysis(
        executive_summary=data["executive_summary"],
        overall_sentiment=data["overall_sentiment"],
        sentiment_score=float(data["sentiment_score"]),
        positive_topics=list_fields["positive_topics"],
        negative_topics=list_fields["negative_topics"],
        requested_features=list_fields["requested_features"],
        recommendations=list_fields["recommendations"],
    )

# The name this function should have had. It has never been Facebook-specific:
# every platform's comments processor emits one shape (see the docstrings in
# processors/*_comments_processor.py), and this reads that shape. The original
# name is kept because main.py and the Facebook Actor import it, and renaming a
# public entry point to tidy it would be a breaking change for no behavioural
# gain -- but a new caller should not have to name one platform to analyse
# another's comments.
#
# (The YouTube analyser above used to be called analyze_comments too, and this
# line silently replaced it, so `main.py youtube` sent a list of videos to the
# Facebook formatter and crashed at step 5 from 2026-09-07 until the type
# checker caught it. It is analyze_youtube_comments now.)
analyze_comments = analyze_facebook_comments
