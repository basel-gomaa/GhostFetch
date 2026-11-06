"""
The text and PDF report renderer, shared by every platform.

Responsibility: turn the analyses (PageAnalysis + CommentAnalysis from
ai/claude.py) into a combined executive report - plain text for the console and
a polished PDF. Mirrors the split used for YouTube (formatter.py /
pdf_report.py), but combines BOTH reports into one document, as the pipeline
architecture requires.

It is called facebook_report.py here for historical reasons - Facebook was the
first platform - and NOTHING in it is Facebook-specific: the vocabulary swaps
come from a `platform=` argument and reports/common.py. Each Actor vendors it
under its own name (`instagram_report.py`, `tiktok_report.py`, ...), so this
docstring deliberately does not name a file that is true in only one tree.
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    ListFlowable,
    ListItem,
    Table,
    TableStyle,
)

from ai.claude import PageAnalysis, CommentAnalysis
from .pdf_text import plain_text, pdf_text, shape, unicode_font
from .common import (audience_label, cap_note, coverage_note, publishes_views,
                     segmentation_note, share_terms, target_label)

OUTPUT_DIR = Path(__file__).parent / "output"

# ---------------------------------------------------------------------------
# Unicode/Arabic text in PDFs lives in reports/pdf_text.py (font + shaping +
# escaping, shared with pdf_report.py). EVERY string that reaches the page -
# paragraph, bullet, header-table cell, table body - must go through
# _pdf_text()/_plain_text() and be drawn in _unicode_font(); Helvetica has no
# Arabic glyphs and draws black boxes. Names kept for existing callers.
# ---------------------------------------------------------------------------

_unicode_font = unicode_font
_shape = shape
_pdf_text = pdf_text
_plain_text = plain_text


def _para(text, style) -> Paragraph:
    """A Paragraph of arbitrary (possibly Arabic, possibly AI-written) text."""
    return Paragraph(_pdf_text(text), style)


def _meta_rows(rows: list[list]) -> list[list]:
    """Header-table rows with every plain-string VALUE turned into a Paragraph
    in the Unicode font (shaped, escaped, wrapping). The page name is the
    value that is Arabic for Egyptian targets; a bare string cell would be
    drawn in Helvetica, which has no Arabic glyphs, as black boxes. Labels
    (column 0) are fixed English and keep the bold Helvetica; cells that are
    already flowables are left alone."""
    style = ParagraphStyle("MetaValue", fontName=_unicode_font(),
                           fontSize=9.5, leading=12)
    return [[label, _para(value, style) if isinstance(value, str) else value]
            for label, value in rows]


def _bullet_lines(items: list[str]) -> str:
    if isinstance(items, str):
        items = [items]
    if not items:
        return "  (none identified)"
    return "\n".join(f"  - {item}" for item in items)


def _company_details_block(page_data: dict) -> str:
    """A COMPANY OVERVIEW section for platforms that carry an About block
    (LinkedIn company scrapes). Returns "" when absent, so other platforms and
    non-company scrapes are unaffected."""
    details = page_data.get("company_details")
    if not isinstance(details, dict) or not details:
        return ""
    order = [
        ("description", "About"), ("industry", "Industry"),
        ("company_size", "Company size"), ("employee_count", "Employees"),
        ("associated_members", "Associated members"),
        ("affiliated_members", "Affiliated members"),
        ("type", "Type"), ("founded", "Founded"),
        ("headquarters", "Headquarters"), ("website", "Website"),
        ("specialties", "Specialties"), ("phone", "Phone"),
    ]
    lines = []
    for key, label in order:
        value = details.get(key)
        if value in (None, "", []):
            continue
        if isinstance(value, int):
            value = f"{value:,}"
        lines.append(f"  {label}: {value}")
    if not lines:
        return ""
    return "COMPANY OVERVIEW\n" + "\n".join(lines) + "\n\n"


def _segmentation_block(segmentation) -> str:
    """A TOPIC & SENTIMENT SEGMENTATION section, or "" when it was not run.

    Additive by construction: `segmentation` is None for every run that did not
    ask for it (and for every run where it failed), so all existing reports -
    Facebook pages, Instagram, TikTok - render byte-identically.
    """
    if not segmentation or not segmentation.topics:
        return ""
    totals = segmentation.sentiment_totals or {}
    labelled = sum(totals.values())
    lines = [
        "TOPIC & SENTIMENT SEGMENTATION",
        # Topic count first, then what the counts are OF - the note ends in the
        # sample clause when there is one, and "1,500 of 6,078 items into 9
        # topic(s)" would read as the sample being the topics.
        f"  {len(segmentation.topics)} topic(s); {segmentation_note(segmentation)}",
        "",
        "  SENTIMENT OVERALL",
    ]
    for name in ("Positive", "Neutral", "Negative", "Mixed"):
        count = totals.get(name, 0)
        share = f"{count / labelled:.0%}" if labelled else "0%"
        lines.append(f"    {name:<9} {count:>5}  ({share})")
    lines += ["", "  BY TOPIC  (items, then positive/neutral/negative/mixed)"]
    for topic in segmentation.topics:
        lines.append(
            f"    {topic['name']} - {topic['count']} item(s) "
            f"[{topic['posts']} post(s), {topic['comments']} comment(s)]: "
            f"+{topic.get('Positive', 0)} / ~{topic.get('Neutral', 0)} / "
            f"-{topic.get('Negative', 0)} / ?{topic.get('Mixed', 0)}")
        if topic.get("description"):
            lines.append(f"        {topic['description']}")
    return "\n".join(lines) + "\n\n"


def _segmentation_pdf_flowables(segmentation, section_style, body_style):
    """Reportlab flowables for the segmentation section (empty when absent)."""
    if not segmentation or not segmentation.topics:
        return []
    flow = [Paragraph("Topic &amp; Sentiment Segmentation", section_style),
            # The table's totals are the sample's totals when a run was capped,
            # and a table with no note beside it claims to be the whole page.
            Paragraph(_pdf_text(segmentation_note(segmentation), 400), body_style),
            Spacer(1, 6)]
    totals = segmentation.sentiment_totals or {}
    rows = [["Topic", "Items", "Posts", "Comments", "Pos", "Neu", "Neg", "Mixed"]]
    for topic in segmentation.topics:
        rows.append([_plain_text(topic["name"], 40), f"{topic['count']:,}",
                     f"{topic['posts']:,}", f"{topic['comments']:,}",
                     f"{topic.get('Positive', 0):,}", f"{topic.get('Neutral', 0):,}",
                     f"{topic.get('Negative', 0):,}", f"{topic.get('Mixed', 0):,}"])
    rows.append(["TOTAL", "", "", "",
                 f"{totals.get('Positive', 0):,}", f"{totals.get('Neutral', 0):,}",
                 f"{totals.get('Negative', 0):,}", f"{totals.get('Mixed', 0):,}"])
    table = Table(rows, hAlign="LEFT",
                  colWidths=[150, 45, 45, 60, 40, 40, 40, 45])
    table.setStyle(TableStyle([
        # Only the regular Unicode face is registered (see _unicode_font), so
        # there is no "-Bold" variant to name. Header/footer rows use
        # Helvetica-Bold like every other table here; the body rows use the
        # Unicode face because topic names can carry non-Latin text.
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), _unicode_font()),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1877F2")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#EEF3FB")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CBD5E1")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    flow.append(table)
    flow.append(Spacer(1, 14))
    return flow


def _company_pdf_flowables(page_data: dict, styles):
    """Reportlab flowables for the COMPANY OVERVIEW block (empty list when the
    page carries no company_details - additive for other platforms)."""
    details = page_data.get("company_details")
    if not isinstance(details, dict) or not details:
        return []
    order = [
        ("description", "About"), ("industry", "Industry"),
        ("company_size", "Company size"), ("employee_count", "Employees"),
        ("associated_members", "Associated members"),
        ("affiliated_members", "Affiliated members"), ("type", "Type"),
        ("founded", "Founded"), ("headquarters", "Headquarters"),
        ("website", "Website"), ("specialties", "Specialties"), ("phone", "Phone"),
    ]
    rows = []
    for key, label in order:
        value = details.get(key)
        if value in (None, "", []):
            continue
        if isinstance(value, int):
            value = f"{value:,}"
        rows.append([label, _para(value, ParagraphStyle(
            "CompanyValue", parent=styles["BodyText"], fontName=_unicode_font()))])
    if not rows:
        return []
    table = Table(rows, colWidths=[1.7 * inch, 4.3 * inch])
    table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return [Spacer(1, 10), Paragraph("Company Overview", styles["Heading2"]),
            Spacer(1, 4), table]


def format_facebook_text_report(
    page_data: dict,
    page_analysis: PageAnalysis,
    comment_analysis: CommentAnalysis,
    comments_data: dict | None = None,
    platform: str = "Facebook",
    segmentation=None,
) -> str:
    stats = page_data.get("statistics") or {}
    comment_posts = (comments_data or {}).get("posts") or []
    total_scraped_comments = sum(len(p.get("comments") or []) for p in comment_posts)
    comments_block = (
        f"\nALL SCRAPED COMMENTS ({total_scraped_comments}"
        + (f" - {_cov}" if (_cov := coverage_note(page_data, comments_data)) else "")
        + ")\n"
        f"{_comment_lines(comment_posts)}\n" if comment_posts else ""
    )
    return f"""
========================================================
 {platform.upper()} INTELLIGENCE REPORT
 {target_label(page_data)}: {page_data.get('page_name')}  |  {audience_label(page_data)}: {page_data.get('followers_count'):,}
 Posts analyzed: {stats.get('total_posts')}
========================================================

{_company_details_block(page_data)}--- PART 1: PAGE PERFORMANCE ---------------------------

RATING: {page_analysis.performance_rating}

ENGAGEMENT METRICS
{_metrics_lines(stats, platform)}

CONTENT TYPES
  {_content_type_line(page_data.get("content_breakdown"))}

EXECUTIVE SUMMARY
{page_analysis.executive_summary}

WHAT IS THE CONTENT OF THE CHANNEL?
{page_analysis.content_overview or "(not analyzed)"}

COMMUNICATION MESSAGE
{page_analysis.communication_message or "(not analyzed)"}

KEY INSIGHTS
{_bullet_lines(page_analysis.key_insights)}

BEST PERFORMING CONTENT
{_bullet_lines(page_analysis.best_performing_content)}

CONTENT RECOMMENDATIONS
{_bullet_lines(page_analysis.content_recommendations)}

ALL SCRAPED POSTS ({len(page_data.get("posts") or [])}, ranked by engagement)
{_post_lines(_by_score(page_data.get('posts') or []), platform)}

TOP 10 POSTS BY ENGAGEMENT
{_post_lines((page_data.get('posts') or [])[:10], platform)}

--- PART 2: COMMENTS & SENTIMENT ANALYSIS --------------

OVERALL SENTIMENT: {comment_analysis.overall_sentiment}  (score: {comment_analysis.sentiment_score:+.2f})

COMMENTS ANALYSIS (EXECUTIVE SUMMARY)
{comment_analysis.executive_summary}

HOW IS THE CONTENT RECEIVED IN COMMENTS?
{comment_analysis.content_reception or "(not analyzed)"}

POSITIVE TOPICS
{_bullet_lines(comment_analysis.positive_topics)}

NEGATIVE TOPICS
{_bullet_lines(comment_analysis.negative_topics)}

AUDIENCE REQUESTS
{_bullet_lines(comment_analysis.requested_features)}

RECOMMENDATIONS
{_bullet_lines(comment_analysis.recommendations)}

{_segmentation_block(segmentation)}{comments_block}========================================================
""".strip()


def _safe_filename(name: str) -> str:
    return "_".join(
        part for part in
        "".join(c if c.isalnum() else "_" for c in name).split("_")
        if part
    )[:40]


def _comments_source_name(comments_data: dict) -> str:
    """A filename-friendly label for where the comments came from."""
    posts = comments_data.get("posts") or []
    url = str(posts[0].get("post_url") or "") if posts else ""

    # Instagram post URLs carry no username (instagram.com/p/<shortcode>/),
    # so fall back to the shortcode, which still identifies the post.
    if "instagram.com" in url:
        match = re.search(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", url)
        return match.group(1) if match else "instagram_post"

    match = re.search(r"facebook\.com/(?:profile\.php\?id=(\d+)|([A-Za-z0-9.\-_]+))", url)
    slug = (match.group(1) or match.group(2)) if match else ""
    if not slug or slug.lower() in {"share", "watch", "reel", "reels", "posts",
                                    "photo", "photos", "story.php", "permalink.php"}:
        slug = "facebook_post"
    return slug


def _metrics_lines(stats: dict, platform: str = "Facebook") -> str:
    _, total_key, avg_key, label = share_terms(platform)
    freq = stats.get("posting_frequency") or {}
    return (
        f"  Likes:    {stats.get('total_likes') or 0:,} total   (avg {stats.get('average_likes') or 0}/post)\n"
        f"  Comments: {stats.get('total_comments') or 0:,} total   (avg {stats.get('average_comments') or 0}/post)\n"
        f"  {label + ':':9} {stats.get(total_key) or 0:,} total   (avg {stats.get(avg_key) or 0}/post)\n"
        + (f"  Views:    {stats.get('total_views') or 0:,} total   "
           f"(avg {stats.get('average_views') or 0}/post)\n\n"
           if publishes_views(platform)
           else f"  Views:    not published by {platform}\n\n") +
        f"POSTING FREQUENCY\n"
        f"  {freq.get('posts_per_week') or 0} posts/week ({freq.get('posts_per_day') or 0}/day), "
        f"from {freq.get('first_post') or '?'} to {freq.get('last_post') or '?'} "
        f"({freq.get('days_covered') or 0} days)"
    )


def _content_type_line(breakdown: dict | None) -> str:
    parts = [f"{k}: {v}" for k, v in (breakdown or {}).items() if v]
    return ", ".join(parts) or "(unknown)"


def _by_score(posts: list[dict]) -> list[dict]:
    return sorted(posts, key=lambda p: p.get("engagement_score") or 0, reverse=True)


def _post_lines(posts: list[dict], platform: str = "Facebook") -> str:
    share_key, _, _, label = share_terms(platform)
    lines = []
    for i, post in enumerate(posts, start=1):
        caption = (post.get("caption") or "(no caption)").replace("\n", " ")[:80]
        lines.append(
            f"  {i}. [{post.get('content_type')}] {str(post.get('post_date') or '')[:10]} - "
            f"{post.get('likes') or 0} likes, {post.get('comments_count') or 0} comments, "
            f"{post.get(share_key) or 0} {label.lower()} "
            f"(score {post.get('engagement_score') or 0}) - {caption}"
        )
    return "\n".join(lines) or "  (none)"


def _pdf_commenter(c, name: str) -> str:
    """Commenter name as a reportlab <link> to the comment's permalink.

    Clicking it in the PDF opens that exact comment on Facebook - the point
    being that a reader can check a scraped comment is really there. Falls back
    to plain coloured text when there is no URL, so the platforms that share
    this renderer and emit no comment ids are unaffected.
    """
    url = c.get("comment_url")
    if not url:
        return f'<font color="#0f3460">{name}</font>'
    return (f'<link href="{html.escape(str(url), quote=True)}">'
            f'<font color="#1155cc"><u>{name}</u></font></link>')


def _comment_lines(posts: list[dict]) -> str:
    lines = []
    multi = len(posts) > 1
    # Newest post first.
    ordered = sorted(posts, key=lambda p: str(p.get("post_date") or ""), reverse=True)
    n = 0
    for post in ordered:
        if multi:
            caption = (post.get("caption") or "(no caption)").replace("\n", " ")[:70]
            note = cap_note(post)
            suffix = f" - {note}" if note else ""
            lines.append(f"  --- Post: {caption} "
                         f"({len(post.get('comments') or [])} comment(s){suffix}) ---")
        for c in post.get("comments") or []:
            n += 1
            text = (c.get("comment") or "(no text)").replace("\n", " ")
            lines.append(f"  {n}. {c.get('username') or 'unknown'} "
                         f"({c.get('likes') or 0} likes): {text}")
            # The permalink is what makes a scraped comment checkable. Printed
            # on its own line so it never mangles the comment text, and omitted
            # entirely when absent (Instagram/TikTok share this renderer).
            if c.get("comment_url"):
                lines.append(f"       verify: {c['comment_url']}")
            for r in c.get("reply_comments") or []:
                rtext = (r.get("comment") or "(no text)").replace("\n", " ")
                lines.append(f"       reply - {r.get('username') or 'unknown'} "
                             f"({r.get('likes') or 0} likes): {rtext}")
                if r.get("comment_url"):
                    lines.append(f"         verify: {r['comment_url']}")
    return "\n".join(lines) or "  (none)"


def format_page_text_report(page_data: dict, page_analysis: PageAnalysis,
                            platform: str = "Facebook") -> str:
    stats = page_data.get("statistics") or {}
    posts = page_data.get("posts") or []
    by_score = _by_score(posts)
    return f"""
========================================================
 {platform.upper()} PAGE REPORT
 {target_label(page_data)}: {page_data.get('page_name')}  |  {audience_label(page_data)}: {page_data.get('followers_count'):,}
 Posts analyzed: {stats.get('total_posts')}
========================================================

{_company_details_block(page_data)}RATING: {page_analysis.performance_rating}

ENGAGEMENT METRICS
{_metrics_lines(stats, platform)}

CONTENT TYPES
  {_content_type_line(page_data.get("content_breakdown"))}

EXECUTIVE SUMMARY
{page_analysis.executive_summary}

WHAT IS THE CONTENT OF THE CHANNEL?
{page_analysis.content_overview or "(not analyzed)"}

COMMUNICATION MESSAGE
{page_analysis.communication_message or "(not analyzed)"}

KEY INSIGHTS
{_bullet_lines(page_analysis.key_insights)}

BEST PERFORMING CONTENT
{_bullet_lines(page_analysis.best_performing_content)}

CONTENT RECOMMENDATIONS
{_bullet_lines(page_analysis.content_recommendations)}

ALL SCRAPED POSTS ({len(posts)}, ranked by engagement)
{_post_lines(by_score, platform)}

TOP 10 POSTS BY ENGAGEMENT
{_post_lines(posts[:10], platform)}
========================================================
""".strip()


def format_comments_text_report(comments_data: dict, comment_analysis: CommentAnalysis,
                                platform: str = "Facebook") -> str:
    posts = comments_data.get("posts") or []
    caption = (posts[0].get("caption") or "(no caption)")[:120] if posts else "(unknown post)"
    post_url = str(posts[0].get("post_url") or "") if posts else ""
    total = sum(len(p.get("comments") or []) for p in posts)
    return f"""
========================================================
 {platform.upper()} COMMENTS REPORT
 Post: {caption}
 Link: {post_url}
 Comments analyzed: {total}
========================================================

OVERALL SENTIMENT: {comment_analysis.overall_sentiment}  (score: {comment_analysis.sentiment_score:+.2f})

EXECUTIVE SUMMARY
{comment_analysis.executive_summary}

HOW IS THE CONTENT RECEIVED IN COMMENTS?
{comment_analysis.content_reception or "(not analyzed)"}

POSITIVE TOPICS
{_bullet_lines(comment_analysis.positive_topics)}

NEGATIVE TOPICS
{_bullet_lines(comment_analysis.negative_topics)}

AUDIENCE REQUESTS
{_bullet_lines(comment_analysis.requested_features)}

RECOMMENDATIONS
{_bullet_lines(comment_analysis.recommendations)}

ALL SCRAPED COMMENTS ({total})
{_comment_lines(posts)}
========================================================
""".strip()


def _bullets(items: list[str], style) -> ListFlowable:
    if isinstance(items, str):
        items = [items]
    if not items:
        return ListFlowable(
            [ListItem(Paragraph("None identified", style))], bulletType="bullet"
        )
    return ListFlowable(
        [ListItem(_para(item, style)) for item in items], bulletType="bullet"
    )


def generate_facebook_pdf(
    page_data: dict,
    page_analysis: PageAnalysis,
    comments_data: dict,
    comment_analysis: CommentAnalysis,
    output_path: str | Path | None = None,
    platform: str = "Facebook",
    segmentation=None,
) -> Path:
    """Render both analyses as one combined PDF and return the written path."""
    page_name = page_data.get("page_name") or "facebook_page"
    stats = page_data.get("statistics") or {}
    total_comments = sum(
        len(post.get("comments") or []) for post in comments_data.get("posts") or []
    )
    if output_path is None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        safe_name = "_".join(
            part for part in
            "".join(c if c.isalnum() else "_" for c in page_name).split("_")
            if part
        )[:40]
        output_path = OUTPUT_DIR / (
            f"{safe_name}_{stats.get('total_posts') or 0}posts_"
            f"{total_comments}comments_{timestamp}.pdf"
        )
    else:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

    styles = getSampleStyleSheet()
    body_style = ParagraphStyle("Body", parent=styles["Normal"], fontName=_unicode_font(),
                                fontSize=10.5, leading=15)
    section_style = ParagraphStyle(
        "Section", parent=styles["Heading2"],
        spaceBefore=16, spaceAfter=6, textColor=colors.HexColor("#1a1a2e"),
    )
    part_style = ParagraphStyle(
        "Part", parent=styles["Heading1"],
        spaceBefore=22, spaceAfter=8, textColor=colors.HexColor("#0f3460"),
    )

    doc = SimpleDocTemplate(
        str(output_path), pagesize=letter,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
    )
    story = []

    # --- Header ------------------------------------------------------
    story.append(Paragraph(f"{platform} Intelligence Report", styles["Title"]))
    story.append(Spacer(1, 4))
    meta_table = Table(
        _meta_rows([
            [("Profile" if platform == "Instagram" else "Page"), page_name],
            [audience_label(page_data), f"{page_data.get('followers_count') or 0:,}"],
            ["Posts analyzed", str(stats.get("total_posts") or 0)],
            ["Comments analyzed", str(total_comments)],
            ["Generated", datetime.now().strftime("%B %d, %Y at %H:%M")],
            ["Page performance", page_analysis.performance_rating],
            ["Audience sentiment",
             f"{comment_analysis.overall_sentiment} ({comment_analysis.sentiment_score:+.2f})"],
        ]),
        colWidths=[1.7 * inch, 4.3 * inch],
    )
    meta_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(meta_table)
    story.extend(_company_pdf_flowables(page_data, styles))

    # --- Part 1: page performance ------------------------------------
    story.append(Paragraph("Part 1 - Page Performance", part_style))
    story.extend(_metrics_pdf_flowables(page_data, section_style, body_style, platform))
    story.append(Paragraph("Executive Summary", section_style))
    story.append(_para(page_analysis.executive_summary, body_style))
    if page_analysis.content_overview:
        story.append(Paragraph("What Is the Content of the Channel?", section_style))
        story.append(_para(page_analysis.content_overview, body_style))
    if page_analysis.communication_message:
        story.append(Paragraph("Communication Message", section_style))
        story.append(_para(page_analysis.communication_message, body_style))
    story.append(Paragraph("Key Insights", section_style))
    story.append(_bullets(page_analysis.key_insights, body_style))
    story.append(Paragraph("Best Performing Content", section_style))
    story.append(_bullets(page_analysis.best_performing_content, body_style))
    story.append(Paragraph("Content Recommendations", section_style))
    story.append(_bullets(page_analysis.content_recommendations, body_style))

    # Posts are already engagement-ranked (rank_by_engagement); show them by score.
    posts_list = _by_score(page_data.get("posts") or [])
    if posts_list:
        story.append(Paragraph(f"All Scraped Posts ({len(posts_list)}, ranked by engagement)",
                               section_style))
        story.append(_posts_pdf_table(posts_list, platform))
        story.append(Paragraph("Top 10 Posts by Engagement", section_style))
        story.append(_posts_pdf_table(posts_list[:10], platform))

    # --- Part 2: comments & sentiment analysis -----------------------
    story.append(Paragraph("Part 2 - Comments & Sentiment Analysis", part_style))
    story.append(Paragraph("Comments Analysis (Executive Summary)", section_style))
    story.append(_para(comment_analysis.executive_summary, body_style))
    if comment_analysis.content_reception:
        story.append(Paragraph("How Is the Content Received in Comments?", section_style))
        story.append(_para(comment_analysis.content_reception, body_style))
    story.append(Paragraph("Positive Topics", section_style))
    story.append(_bullets(comment_analysis.positive_topics, body_style))
    story.append(Paragraph("Negative Topics", section_style))
    story.append(_bullets(comment_analysis.negative_topics, body_style))
    story.append(Paragraph("Audience Requests", section_style))
    story.append(_bullets(comment_analysis.requested_features, body_style))
    story.append(Paragraph("Recommendations", section_style))
    story.append(_bullets(comment_analysis.recommendations, body_style))

    story.extend(_segmentation_pdf_flowables(segmentation, section_style,
                                             body_style))

    comment_posts = comments_data.get("posts") or []
    if any(p.get("comments") for p in comment_posts):
        story.extend(_comment_pdf_flowables(comment_posts, section_style))

    doc.build(story)
    return output_path


# ---------------------------------------------------------------------------
# Single-mode PDFs (page-only / comments-only) - used by the interactive menu
# where page scraping and comment scraping are independent flows.
# ---------------------------------------------------------------------------

def _metrics_pdf_flowables(page_data: dict, section_style, body_style,
                           platform: str = "Facebook") -> list:
    """Engagement metrics + posting frequency + content types, for PDFs."""
    _, share_total_key, share_avg_key, share_label = share_terms(platform)
    stats = page_data.get("statistics") or {}
    freq = stats.get("posting_frequency") or {}
    rows = [["Metric", "Total", "Average / post"]]
    metrics = [
        ("Likes", "total_likes", "average_likes"),
        ("Comments", "total_comments", "average_comments"),
        (share_label, share_total_key, share_avg_key),
    ]
    if publishes_views(platform):
        metrics.append(("Views", "total_views", "average_views"))
    for label, total_key, avg_key in metrics:
        rows.append([label, f"{stats.get(total_key) or 0:,}",
                     str(stats.get(avg_key) or 0)])
    if not publishes_views(platform):
        rows.append(["Views", f"not published by {platform}", "-"])
    table = Table(rows, colWidths=[1.6 * inch, 1.6 * inch, 1.6 * inch])
    table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f7")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))
    frequency_text = (
        f"{freq.get('posts_per_week') or 0} posts/week "
        f"({freq.get('posts_per_day') or 0}/day), from {freq.get('first_post') or '?'} "
        f"to {freq.get('last_post') or '?'} ({freq.get('days_covered') or 0} days)."
    )
    return [
        Paragraph("Engagement Metrics", section_style),
        table,
        Paragraph("Posting Frequency", section_style),
        Paragraph(frequency_text, body_style),
        Paragraph("Content Types", section_style),
        Paragraph(_content_type_line(page_data.get("content_breakdown")), body_style),
    ]


def _comment_pdf_flowables(comment_posts: list[dict], section_style) -> list:
    """Every scraped comment as PDF paragraphs, grouped by post (newest post
    first), with each comment's replies indented underneath it."""
    total = sum(len(p.get("comments") or []) for p in comment_posts)
    replies = sum(len(c.get("reply_comments") or [])
                  for p in comment_posts for c in p.get("comments") or [])
    comment_style = ParagraphStyle(
        "Comment", fontName=_unicode_font(), fontSize=9, leading=12, spaceAfter=4
    )
    reply_style = ParagraphStyle(
        "Reply", fontName=_unicode_font(), fontSize=8.5, leading=11,
        leftIndent=20, spaceAfter=2, textColor=colors.HexColor("#444444"),
    )
    post_header_style = ParagraphStyle(
        "CommentPost", fontName=_unicode_font(), fontSize=9.5, leading=12,
        spaceBefore=8, spaceAfter=4, textColor=colors.HexColor("#0f3460"),
    )
    flows = [Paragraph(f"All Scraped Comments ({total} comments + {replies} replies)",
                       section_style)]
    multi = len(comment_posts) > 1
    ordered = sorted(comment_posts, key=lambda p: str(p.get("post_date") or ""), reverse=True)
    n = 0
    for post in ordered:
        if multi:
            caption = _pdf_text(post.get("caption") or "(no caption)", 90)
            count = len(post.get("comments") or [])
            note = cap_note(post)
            flows.append(Paragraph(f"Post: {caption} ({count} comment(s)"
                                   f"{' - ' + note if note else ''})",
                                   post_header_style))
        for c in post.get("comments") or []:
            n += 1
            user = _pdf_text(c.get("username") or "unknown", 60)
            text = _pdf_text(c.get("comment") or "(no text)", 600)
            flows.append(Paragraph(
                f'{n}. {_pdf_commenter(c, user)} ({c.get("likes") or 0} likes): {text}',
                comment_style,
            ))
            for r in c.get("reply_comments") or []:
                ruser = _pdf_text(r.get("username") or "unknown", 60)
                rtext = _pdf_text(r.get("comment") or "(no text)", 600)
                flows.append(Paragraph(
                    f'&#8618; {_pdf_commenter(r, ruser)} '
                    f'({r.get("likes") or 0} likes): {rtext}',
                    reply_style,
                ))
    return flows


def _posts_pdf_table(posts: list[dict], platform: str = "Facebook") -> Table:
    share_key, _, _, share_label = share_terms(platform)
    """Posts table with (Arabic-capable) caption column."""
    caption_style = ParagraphStyle(
        "Cap", fontName=_unicode_font(), fontSize=8, leading=10
    )
    rows = [["#", "Date", "Type", "Caption", "Likes", "Cmts", share_label, "Score"]]
    for i, post in enumerate(posts, start=1):
        rows.append([
            str(i),
            str(post.get("post_date") or "")[:10],
            str(post.get("content_type") or "-"),
            Paragraph(_pdf_text(post.get("caption") or "(no caption)", 90), caption_style),
            str(post.get("likes") or 0),
            str(post.get("comments_count") or 0),
            str(post.get(share_key) or 0),
            str(post.get("engagement_score") or 0),
        ])
    table = Table(
        rows,
        colWidths=[0.3 * inch, 0.75 * inch, 0.55 * inch, 3.05 * inch,
                   0.5 * inch, 0.45 * inch, 0.5 * inch, 0.5 * inch],
        repeatRows=1,
    )
    table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f7")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _doc_styles(output_path: Path):
    styles = getSampleStyleSheet()
    body = ParagraphStyle("Body", parent=styles["Normal"], fontName=_unicode_font(),
                          fontSize=10.5, leading=15)
    section = ParagraphStyle(
        "Section", parent=styles["Heading2"],
        spaceBefore=16, spaceAfter=6, textColor=colors.HexColor("#1a1a2e"),
    )
    doc = SimpleDocTemplate(
        str(output_path), pagesize=letter,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
    )
    return doc, styles, body, section


def _meta_table(rows: list[list]) -> Table:
    table = Table(_meta_rows(rows), colWidths=[1.7 * inch, 4.3 * inch])
    table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def generate_page_pdf(
    page_data: dict,
    page_analysis: PageAnalysis,
    output_path: str | Path | None = None,
    platform: str = "Facebook",
) -> Path:
    """Page-performance report only (public page scraping flow)."""
    page_name = page_data.get("page_name") or "facebook_page"
    stats = page_data.get("statistics") or {}
    if output_path is None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        output_path = OUTPUT_DIR / (
            f"{_safe_filename(page_name)}_{stats.get('total_posts') or 0}posts_"
            f"page_report_{timestamp}.pdf"
        )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    doc, styles, body, section = _doc_styles(output_path)
    story = [
        Paragraph(f"{platform} Page Report", styles["Title"]),
        Spacer(1, 4),
        _meta_table([
            [("Profile" if platform == "Instagram" else "Page"), page_name],
            [audience_label(page_data), f"{page_data.get('followers_count') or 0:,}"],
            ["Posts analyzed", str(stats.get("total_posts") or 0)],
            ["Generated", datetime.now().strftime("%B %d, %Y at %H:%M")],
            ["Performance", page_analysis.performance_rating],
        ]),
    ]
    story.extend(_company_pdf_flowables(page_data, styles))
    story.extend(_metrics_pdf_flowables(page_data, section, body, platform))
    story.extend([
        Paragraph("Executive Summary", section),
        _para(page_analysis.executive_summary, body),
    ])
    if page_analysis.content_overview:
        story.append(Paragraph("What Is the Content of the Channel?", section))
        story.append(_para(page_analysis.content_overview, body))
    if page_analysis.communication_message:
        story.append(Paragraph("Communication Message", section))
        story.append(_para(page_analysis.communication_message, body))
    story.extend([
        Paragraph("Key Insights", section),
        _bullets(page_analysis.key_insights, body),
        Paragraph("Best Performing Content", section),
        _bullets(page_analysis.best_performing_content, body),
        Paragraph("Content Recommendations", section),
        _bullets(page_analysis.content_recommendations, body),
    ])

    posts = _by_score(page_data.get("posts") or [])
    if posts:
        story.append(Paragraph(f"All Scraped Posts ({len(posts)}, ranked by engagement)",
                               section))
        story.append(_posts_pdf_table(posts, platform))
        story.append(Paragraph("Top 10 Posts by Engagement", section))
        story.append(_posts_pdf_table(posts[:10], platform))

    doc.build(story)
    return output_path


def generate_comments_pdf(
    comments_data: dict,
    comment_analysis: CommentAnalysis,
    output_path: str | Path | None = None,
    platform: str = "Facebook",
) -> Path:
    """Comments/audience report only (single-post comment scraping flow)."""
    total_comments = sum(
        len(post.get("comments") or []) for post in comments_data.get("posts") or []
    )
    if output_path is None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        output_path = OUTPUT_DIR / (
            f"{_safe_filename(_comments_source_name(comments_data))}_"
            f"{total_comments}comments_report_{timestamp}.pdf"
        )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    posts = comments_data.get("posts") or []
    total_reported = sum(int(post.get("comments_count") or 0) for post in posts)
    post_url = str(posts[0].get("post_url") or "") if posts else ""

    doc, styles, body, section = _doc_styles(output_path)
    link_style = ParagraphStyle(
        "Link", fontName=_unicode_font(), fontSize=8.5, leading=11,
        textColor=colors.HexColor("#0f3460"),
    )
    caption_style = ParagraphStyle(
        "MetaCaption", fontName=_unicode_font(), fontSize=9, leading=12
    )
    caption = posts[0].get("caption") if posts else None

    story = [
        Paragraph(f"{platform} Comments Report", styles["Title"]),
        Spacer(1, 4),
        _meta_table([
            ["Post link",
             Paragraph(f'<a href="{html.escape(post_url)}">{html.escape(post_url)}</a>',
                       link_style) if post_url else "-"],
            ["Post caption", Paragraph(_pdf_text(caption or "(no caption)", 200),
                                       caption_style)],
            ["Comments analyzed", str(total_comments)],
            ["Post total (incl. replies)", str(total_reported)],
            ["Generated", datetime.now().strftime("%B %d, %Y at %H:%M")],
            ["Sentiment",
             f"{comment_analysis.overall_sentiment} ({comment_analysis.sentiment_score:+.2f})"],
        ]),
        Paragraph("Executive Summary", section),
        _para(comment_analysis.executive_summary, body),
        *([Paragraph("How Is the Content Received in Comments?", section),
           _para(comment_analysis.content_reception, body)]
          if comment_analysis.content_reception else []),
        Paragraph("Positive Topics", section),
        _bullets(comment_analysis.positive_topics, body),
        Paragraph("Negative Topics", section),
        _bullets(comment_analysis.negative_topics, body),
        Paragraph("Audience Requests", section),
        _bullets(comment_analysis.requested_features, body),
        Paragraph("Recommendations", section),
        _bullets(comment_analysis.recommendations, body),
    ]

    # Every scraped comment, grouped by post when there are several.
    story.extend(_comment_pdf_flowables(posts, section))

    doc.build(story)
    return output_path


# ---------------------------------------------------------------------------
# Platform-neutral names for the two combined-report entry points.
#
# Exactly the same functions. A caller rendering Instagram should not have to
# name Facebook to do it, and inside an Actor - which vendors this module as
# `<platform>_report.py` - `from reports.instagram_report import
# format_facebook_text_report` reads as two platforms in one line.
#
# The originals are KEPT rather than renamed: main.py imports them at twenty
# sites and they are the documented public surface, so this follows the
# treatment ai/claude.py already gives `analyze_comments`
# (= `analyze_facebook_comments`) - add the neutral name, break nothing.
# ---------------------------------------------------------------------------

format_combined_text_report = format_facebook_text_report
generate_combined_pdf = generate_facebook_pdf
