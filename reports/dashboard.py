"""
reports/dashboard.py

Responsibility: render the Facebook analyses as a self-contained HTML
dashboard (no server, no external assets - just open the file in a
browser). Overwrites the same file on each run so there is one stable
path to bookmark; the PDF is the timestamped archival artifact.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from ai.claude import PageAnalysis, CommentAnalysis
from .common import (audience_label, cap_note, coverage_note, publishes_views,
                     segmentation_note, share_terms, target_label)

OUTPUT_DIR = Path(__file__).parent / "output"


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _commenter_html(c):
    """The commenter's name, linked to the comment's permalink when we have one.

    Degrades to plain bold text when `comment_url` is absent, which is what
    keeps Instagram and TikTok - who share this renderer and emit no comment
    ids - byte-identical.
    """
    name = f"<b>{_esc(c.get('username') or 'unknown')}</b>"
    url = c.get("comment_url")
    if not url:
        return name
    return (f"<a href='{_esc(url)}' target='_blank' rel='noopener noreferrer'>"
            f"{name}</a>")


def _bullet_list(items: list[str]) -> str:
    if isinstance(items, str):
        items = [items]
    if not items:
        return "<p class='muted'>None identified</p>"
    return "<ul>" + "".join(f"<li>{_esc(item)}</li>" for item in items) + "</ul>"


def _stat_card(label: str, value, note: str | None = None) -> str:
    """One metric card. `note` explains a non-numeric value (e.g. a metric the
    platform does not publish), so an "n/a" never looks like a failed scrape."""
    sub = (f"<div class='card-note'>{_esc(note)}</div>") if note else ""
    return (
        f"<div class='card'><div class='card-value'>{_esc(value)}</div>"
        f"<div class='card-label'>{_esc(label)}</div>{sub}</div>"
    )


def _company_details_html(page_data: dict) -> str:
    """A COMPANY OVERVIEW panel for LinkedIn company scrapes (additive; returns
    "" when the page carries no company_details block)."""
    details = page_data.get("company_details")
    if not isinstance(details, dict) or not details:
        return ""
    order = [
        ("industry", "Industry"), ("company_size", "Company size"),
        ("employee_count", "Employees"), ("associated_members", "Associated members"),
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
        if key == "website" and isinstance(value, str) and value.startswith("http"):
            value = f"<a href='{_esc(value)}'>{_esc(value)}</a>"
        else:
            value = _esc(value)
        rows.append(f"<tr><th>{_esc(label)}</th><td>{value}</td></tr>")
    desc = details.get("description")
    desc_html = f"<p>{_esc(desc)}</p>" if desc else ""
    return (
        "<div class='panel'><h2>Company Overview</h2>"
        f"{desc_html}<table class='kv'>{''.join(rows)}</table></div>"
    )


def _segmentation_html(segmentation) -> str:
    """A TOPIC & SENTIMENT panel: overall split, then a topic x sentiment table.

    Additive - `segmentation` is None for every run that did not request it (or
    where it failed), so existing reports render byte-identically.
    """
    if not segmentation or not segmentation.topics:
        return ""
    totals = segmentation.sentiment_totals or {}
    labelled = sum(totals.values()) or 1
    order = ("Positive", "Neutral", "Negative", "Mixed")
    tone = {"Positive": "#16A34A", "Neutral": "#64748B",
            "Negative": "#DC2626", "Mixed": "#D97706"}

    bars = []
    for name in order:
        count = totals.get(name, 0)
        if not count:
            continue
        pct = round(100 * count / labelled)
        bars.append(
            f"<div class='bar-row'><span class='bar-label'>{_esc(name)}</span>"
            f"<span class='bar-track'><span class='bar-fill' "
            f"style='width:{pct}%;background:{tone[name]}'></span></span>"
            f"<span class='bar-count'>{count} ({pct}%)</span></div>")

    rows = []
    for topic in segmentation.topics:
        cells = "".join(
            f"<td style='color:{tone[name]}'>{topic.get(name, 0):,}</td>"
            for name in order)
        rows.append(
            f"<tr><td><b>{_esc(topic['name'])}</b>"
            + (f"<div class='muted'>{_esc(topic.get('description'))}</div>"
               if topic.get("description") else "")
            + f"</td><td>{topic['count']:,}</td><td>{topic['posts']:,}</td>"
            f"<td>{topic['comments']:,}</td>{cells}</tr>")

    note = segmentation_note(segmentation)
    return (
        "<div class='panel'><h2>Topic &amp; Sentiment Segmentation</h2>"
        f"<p class='muted'>{_esc(note)}</p>"
        f"{''.join(bars)}"
        "<div style='overflow-x:auto'><table><thead><tr>"
        "<th>Topic</th><th>Items</th><th>Posts</th><th>Comments</th>"
        "<th>Positive</th><th>Neutral</th><th>Negative</th><th>Mixed</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div></div>"
    )


def _bar_rows(breakdown: dict) -> str:
    total = sum(breakdown.values()) or 1
    rows = []
    for name, count in sorted(breakdown.items(), key=lambda kv: -kv[1]):
        if not count:
            continue
        pct = round(100 * count / total)
        rows.append(
            f"<div class='bar-row'><span class='bar-label'>{_esc(name)}</span>"
            f"<span class='bar-track'><span class='bar-fill' style='width:{pct}%'></span></span>"
            f"<span class='bar-count'>{count} ({pct}%)</span></div>"
        )
    return "".join(rows) or "<p class='muted'>No content data</p>"


def _posts_table(posts: list[dict], limit: int | None = None,
                 platform: str = "Facebook") -> str:
    share_key, _, _, share_label = share_terms(platform)
    rows = []
    for i, post in enumerate(posts[:limit] if limit else posts, start=1):
        caption = (post.get("caption") or "").replace("\n", " ")
        caption = caption[:120] + ("..." if len(caption) > 120 else "")
        url = post.get("post_url") or "#"
        rows.append(
            "<tr>"
            f"<td>{i}</td>"
            f"<td class='caption'><a href='{_esc(url)}' target='_blank'>{_esc(caption) or '(no caption)'}</a></td>"
            f"<td>{_esc(post.get('content_type'))}</td>"
            f"<td>{_esc(str(post.get('post_date') or '')[:10])}</td>"
            f"<td>{post.get('likes') or 0}</td>"
            f"<td>{post.get('comments_count') or 0}</td>"
            f"<td>{post.get(share_key) or 0}</td>"
            f"<td><b>{post.get('engagement_score') or 0}</b></td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>#</th><th>Caption</th><th>Type</th><th>Date</th>"
        f"<th>Likes</th><th>Comments</th><th>{share_label}</th><th>Score</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def generate_dashboard(
    page_data: dict,
    page_analysis: PageAnalysis,
    comments_data: dict | None = None,
    comment_analysis: CommentAnalysis | None = None,
    output_path: str | Path | None = None,
    platform: str = "Facebook",
    segmentation=None,
) -> Path:
    """Comments/audience sections are optional - page-only scraping passes
    just (page_data, page_analysis)."""
    stats = page_data.get("statistics") or {}
    posts = page_data.get("posts") or []
    total_comments = sum(
        len(p.get("comments") or []) for p in (comments_data or {}).get("posts") or []
    )

    if output_path is None:
        page_name = page_data.get("page_name") or "facebook_page"
        safe_name = "_".join(
            part for part in
            "".join(c if c.isalnum() else "_" for c in page_name).split("_")
            if part
        )[:40]
        comments_part = f"{total_comments}comments_" if comment_analysis else ""
        output_path = OUTPUT_DIR / (
            f"{safe_name}_{stats.get('total_posts') or 0}posts_"
            f"{comments_part}dashboard.html"
        )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # sentiment_score is -1..1 -> map to 0..100% for the meter
    sentiment_pct = round((comment_analysis.sentiment_score + 1) / 2 * 100) \
        if comment_analysis else 50

    freq = stats.get("posting_frequency") or {}
    _, share_total_key, share_avg_key, share_label = share_terms(platform)

    def _panel(title: str, text: str) -> str:
        if not text:
            return ""
        return f"<div class='panel'><h2>{_esc(title)}</h2><p>{_esc(text)}</p></div>"

    ai_text_panels = (
        _panel("What Is the Content of the Channel?",
               getattr(page_analysis, "content_overview", ""))
        + _panel("Communication Message",
                 getattr(page_analysis, "communication_message", ""))
        + (_panel("How Is the Content Received in Comments?",
                  getattr(comment_analysis, "content_reception", ""))
           if comment_analysis else "")
    )

    comments_list_html = ""
    if comments_data and (comments_data.get("posts") or []):
        total_replies = sum(
            len(c.get("reply_comments") or [])
            for post in comments_data.get("posts") or []
            for c in post.get("comments") or []
        )
        items = []
        n = 0
        # Newest post first.
        ordered_posts = sorted(comments_data.get("posts") or [],
                               key=lambda p: str(p.get("post_date") or ""), reverse=True)
        for post in ordered_posts:
            post_comments = post.get("comments") or []
            caption = (post.get("caption") or "(no caption)").replace("\n", " ")[:90]
            note = cap_note(post)
            suffix = f" &mdash; {_esc(note)}" if note else ""
            items.append(f"<p class='comment-post'>{_esc(caption)} "
                         f"({len(post_comments)} comment(s){suffix})</p>")
            # A post that returned nothing is still shown: silently dropping it
            # makes the report look like it covered fewer posts than it did.
            if not post_comments:
                reported = post.get("comments_count")
                note = ("no comments on this post" if not reported
                        else f"no comments retrieved (post reports {reported})")
                items.append(f"<p class='comment muted'>{_esc(note)}</p>")
                continue
            for c in post_comments:
                n += 1
                items.append(
                    f"<p class='comment'>{_commenter_html(c)} "
                    f"<span class='muted'>({c.get('likes') or 0} likes)</span> "
                    f"{_esc(c.get('comment') or '(no text)')}</p>"
                )
                for r in c.get("reply_comments") or []:
                    items.append(
                        f"<p class='reply'>&#8618; {_commenter_html(r)} "
                        f"<span class='muted'>({r.get('likes') or 0} likes)</span> "
                        f"{_esc(r.get('comment') or '(no text)')}</p>"
                    )
        comments_list_html = (
            f"<div class='panel'><details><summary>All Scraped Comments "
            f"({total_comments} comments + {total_replies} replies "
            f"across {len(ordered_posts)} post(s))</summary>"
            f"{''.join(items)}</details></div>"
        )

    if comment_analysis:
        comments_card = _stat_card("Comments analyzed", total_comments,
                                   note=coverage_note(page_data, comments_data))
        audience_panel = f"""<div class="panel">
      <h2>Audience Sentiment <span class="badge">{_esc(comment_analysis.overall_sentiment)}</span></h2>
      <div class="meter"><div class="meter-fill"></div></div>
      <p class="muted">score {comment_analysis.sentiment_score:+.2f} (from -1 negative to +1 positive)</p>
      <p>{_esc(comment_analysis.executive_summary)}</p>
    </div>"""
        audience_topic_panels = (
            f"<div class='panel'><h2>Positive Topics</h2>{_bullet_list(comment_analysis.positive_topics)}</div>"
            f"<div class='panel'><h2>Negative Topics</h2>{_bullet_list(comment_analysis.negative_topics)}</div>"
        )
        audience_recs_panel = (
            f"<div class='panel'><h2>Audience Recommendations</h2>"
            f"{_bullet_list(comment_analysis.recommendations)}</div>"
        )
    else:
        comments_card = ""
        audience_panel = (
            "<div class='panel'><h2>Audience Sentiment</h2>"
            "<p class='muted'>Run comment scraping on one of this page's posts "
            "to add audience &amp; sentiment analysis.</p></div>"
        )
        audience_topic_panels = ""
        audience_recs_panel = ""

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{platform} Dashboard - {_esc(page_data.get('page_name'))}</title>
<style>
  :root {{ --ink:#1a1a2e; --accent:#0f3460; --muted:#6b7280; --line:#e5e7eb; --bg:#f6f7fb; }}
  * {{ box-sizing:border-box; margin:0; }}
  body {{ font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif; background:var(--bg);
         color:var(--ink); padding:32px 16px; line-height:1.55; }}
  .wrap {{ max-width:1000px; margin:0 auto; }}
  h1 {{ font-size:1.6rem; }} h2 {{ font-size:1.15rem; margin-bottom:12px; color:var(--accent); }}
  .sub {{ color:var(--muted); margin:4px 0 24px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:24px; }}
  .card {{ background:#fff; border:1px solid var(--line); border-radius:10px; padding:16px; text-align:center; }}
  .card-value {{ font-size:1.45rem; font-weight:700; }}
  .card-label {{ color:var(--muted); font-size:.82rem; margin-top:2px; }}
  .card-note {{ color:var(--muted); font-size:.68rem; margin-top:3px; opacity:.8; }}
  .panel {{ background:#fff; border:1px solid var(--line); border-radius:10px; padding:20px 22px; margin-bottom:20px; }}
  .cols {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; }}
  @media (max-width:760px) {{ .cols {{ grid-template-columns:1fr; }} }}
  ul {{ padding-left:20px; }} li {{ margin:5px 0; }}
  .muted {{ color:var(--muted); }}
  .bar-row {{ display:flex; align-items:center; gap:10px; margin:7px 0; }}
  .bar-label {{ width:100px; font-size:.85rem; color:var(--muted); text-transform:capitalize; }}
  .bar-track {{ flex:1; background:var(--bg); border-radius:6px; height:14px; overflow:hidden; display:block; }}
  .bar-fill {{ background:var(--accent); height:100%; display:block; border-radius:6px; }}
  .bar-count {{ width:80px; font-size:.82rem; color:var(--muted); text-align:right; }}
  .meter {{ background:var(--bg); border-radius:8px; height:18px; overflow:hidden; margin:8px 0 4px; }}
  .meter-fill {{ height:100%; border-radius:8px; background:var(--accent); width:{sentiment_pct}%; }}
  .badge {{ display:inline-block; padding:3px 12px; border-radius:999px; background:var(--accent);
           color:#fff; font-size:.85rem; font-weight:600; }}
  table {{ width:100%; border-collapse:collapse; font-size:.88rem; }}
  th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); }}
  table.kv th {{ width:190px; white-space:nowrap; vertical-align:top; }}
  th {{ color:var(--muted); font-size:.8rem; text-transform:uppercase; letter-spacing:.03em; }}
  td.caption {{ max-width:340px; }} td.caption a {{ color:var(--ink); text-decoration:none; }}
  td.caption a:hover {{ text-decoration:underline; }}
  .scroll {{ overflow-x:auto; }}
  details summary {{ cursor:pointer; font-weight:600; color:var(--accent); font-size:1.05rem; }}
  .comment {{ margin:6px 0; padding-left:10px; border-left:3px solid var(--line); font-size:.9rem; }}
  .reply {{ margin:3px 0 3px 26px; padding-left:10px; border-left:2px solid var(--accent);
           font-size:.85rem; color:#444; }}
  .comment-post {{ margin:14px 0 4px; font-weight:600; color:var(--accent); font-size:.92rem; }}
  footer {{ color:var(--muted); font-size:.8rem; text-align:center; margin-top:28px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>{platform} Intelligence Dashboard</h1>
  <p class="sub">{_esc(page_data.get('page_name'))} &middot; scraped {_esc(str(page_data.get('scrape_date') or '')[:19])}
     &middot; generated {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>

  <div class="cards">
    {_stat_card(audience_label(page_data), f"{page_data.get('followers_count') or 0:,}")}
    {_stat_card("Posts analyzed", stats.get("total_posts") or 0)}
    {_stat_card("Posts / week", freq.get("posts_per_week") or 0)}
    {comments_card}
  </div>
  <div class="cards">
    {_stat_card("Total likes", f"{stats.get('total_likes') or 0:,}")}
    {_stat_card("Total comments", f"{stats.get('total_comments') or 0:,}")}
    {_stat_card(f"Total {share_label.lower()}", f"{stats.get(share_total_key) or 0:,}")}
    {_stat_card("Total views", f"{stats.get('total_views') or 0:,}")
      if publishes_views(platform)
      else _stat_card("Total views", "n/a", note=f"not published by {platform}")}
  </div>
  <div class="cards">
    {_stat_card("Avg likes / post", stats.get("average_likes") or 0)}
    {_stat_card("Avg comments / post", stats.get("average_comments") or 0)}
    {_stat_card(f"Avg {share_label.lower()} / post", stats.get(share_avg_key) or 0)}
    {_stat_card("Avg views / post", stats.get("average_views") or 0)
      if publishes_views(platform)
      else _stat_card("Avg views / post", "n/a", note=f"not published by {platform}")}
  </div>

  {_company_details_html(page_data)}
  {_segmentation_html(segmentation)}

  <div class="cols">
    <div class="panel">
      <h2>Page Performance <span class="badge">{_esc(page_analysis.performance_rating)}</span></h2>
      <p>{_esc(page_analysis.executive_summary)}</p>
    </div>
    {audience_panel}
  </div>

  <div class="cols">
    {ai_text_panels}
  </div>

  <div class="panel">
    <h2>Content Mix</h2>
    {_bar_rows(page_data.get("content_breakdown") or {})}
  </div>

  <div class="panel">
    <h2>All Posts ({len(posts)}, ranked by engagement)</h2>
    <div class="scroll">{_posts_table(sorted(posts, key=lambda p: p.get('engagement_score') or 0, reverse=True), platform=platform)}</div>
  </div>

  <div class="cols">
    <div class="panel"><h2>Key Insights</h2>{_bullet_list(page_analysis.key_insights)}</div>
    <div class="panel"><h2>Best Performing Content</h2>{_bullet_list(page_analysis.best_performing_content)}</div>
    {audience_topic_panels}
    <div class="panel"><h2>Content Recommendations</h2>{_bullet_list(page_analysis.content_recommendations)}</div>
    {audience_recs_panel}
  </div>

  {comments_list_html}

  <footer>GhostFetch &middot; {platform} module</footer>
</div>
</body>
</html>"""

    output_path.write_text(page_html, encoding="utf-8")
    return output_path
