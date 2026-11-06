"""
reports/formatter.py

Responsibility: turn a CommentAnalysis (from ai/claude.py) into a
human-readable executive report. Today that's plain text for the
console; later this is where HTML/PDF generation would live, without
touching collectors/ or ai/.
"""

from __future__ import annotations

from ai.claude import CommentAnalysis


def format_text_report(channel_id: str, video_count: int, analysis: CommentAnalysis) -> str:
    def bullet_list(items: list[str]) -> str:
        # Defensive: a bare string here would otherwise be iterated
        # character-by-character, producing one bullet per letter.
        if isinstance(items, str):
            items = [items]
        if not items:
            return "  (none identified)"
        return "\n".join(f"  - {item}" for item in items)

    return f"""
========================================================
 SOCIAL MEDIA INTELLIGENCE REPORT
 Channel: {channel_id}  |  Videos analyzed: {video_count}
========================================================

EXECUTIVE SUMMARY
{analysis.executive_summary}

OVERALL SENTIMENT: {analysis.overall_sentiment}  (score: {analysis.sentiment_score:+.2f})

POSITIVE TOPICS
{bullet_list(analysis.positive_topics)}

NEGATIVE TOPICS
{bullet_list(analysis.negative_topics)}

FREQUENTLY REQUESTED FEATURES
{bullet_list(analysis.requested_features)}

RECOMMENDATIONS
{bullet_list(analysis.recommendations)}
========================================================
""".strip()