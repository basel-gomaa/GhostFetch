"""
reports/pdf_report.py

Responsibility: turn a CommentAnalysis (from ai/claude.py) into a
polished PDF file. Same input contract as reports/formatter.py's
format_text_report -- this module just renders it differently.
Keeping this separate from formatter.py means "how do we output this"
stays a reports/ concern; main.py just asks for a report and gets one.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    ListFlowable,
    ListItem,
    Table,
    TableStyle,
)

from ai.claude import CommentAnalysis
from .pdf_text import pdf_text, unicode_font

OUTPUT_DIR = Path(__file__).parent / "output"


def _bullets(items: list[str], style) -> ListFlowable:
    # Defensive: a bare string here would otherwise be iterated
    # character-by-character, producing one bullet per letter.
    if isinstance(items, str):
        items = [items]
    if not items:
        return ListFlowable(
            [ListItem(Paragraph("None identified", style))],
            bulletType="bullet",
        )
    return ListFlowable(
        [ListItem(Paragraph(pdf_text(item), style)) for item in items],
        bulletType="bullet",
    )


def generate_pdf_report(
    channel_id: str,
    video_count: int,
    analysis: CommentAnalysis,
    output_path: str | Path | None = None,
) -> Path:
    """
    Render `analysis` as a PDF and write it to disk.

    If output_path is not given, writes to reports/output/ with a
    timestamped filename so repeated runs don't overwrite each other.
    Returns the Path that was written, so main.py can print/log it.
    """
    if output_path is None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        output_path = OUTPUT_DIR / f"report_{channel_id}_{timestamp}.pdf"
    else:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

    styles = getSampleStyleSheet()
    body_style = ParagraphStyle(
        "Body", parent=styles["Normal"], fontName=unicode_font(),
        fontSize=10.5, leading=15
    )
    section_style = ParagraphStyle(
        "Section",
        parent=styles["Heading2"],
        spaceBefore=16,
        spaceAfter=6,
        textColor=colors.HexColor("#1a1a2e"),
    )

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
    )

    story = []

    # --- Header -----------------------------------------------------
    story.append(Paragraph("Social Media Intelligence Report", styles["Title"]))
    story.append(Spacer(1, 4))
    meta_table = Table(
        [
            ["Channel ID", Paragraph(pdf_text(channel_id), ParagraphStyle(
                "MetaValue", fontName=unicode_font(), fontSize=9.5, leading=12))],
            ["Videos analyzed", str(video_count)],
            ["Generated", datetime.now().strftime("%B %d, %Y at %H:%M")],
            ["Overall sentiment", f"{analysis.overall_sentiment} ({analysis.sentiment_score:+.2f})"],
        ],
        colWidths=[1.7 * inch, 4.3 * inch],
    )
    meta_table.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(meta_table)

    # --- Executive summary -------------------------------------------
    story.append(Paragraph("Executive Summary", section_style))
    story.append(Paragraph(pdf_text(analysis.executive_summary), body_style))

    # --- Topics --------------------------------------------------------
    story.append(Paragraph("Positive Topics", section_style))
    story.append(_bullets(analysis.positive_topics, body_style))

    story.append(Paragraph("Negative Topics", section_style))
    story.append(_bullets(analysis.negative_topics, body_style))

    story.append(Paragraph("Frequently Requested Features", section_style))
    story.append(_bullets(analysis.requested_features, body_style))

    story.append(Paragraph("Recommendations", section_style))
    story.append(_bullets(analysis.recommendations, body_style))

    doc.build(story)
    return output_path
