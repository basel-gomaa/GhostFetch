"""
reports/pdf_text.py

Responsibility: make arbitrary user/AI text safe to draw in a reportlab PDF,
including Arabic. Shared by both PDF systems (facebook_report.py and
pdf_report.py), so it holds the font choice, the Arabic shaping and the
escaping in ONE place.

Why this exists as a module of its own: the built-in Helvetica has no Arabic
glyphs, so any string that reaches the page through Helvetica draws as black
boxes. Registering a Unicode font is only half of it - every route text can
take onto the page (Paragraph, table cell, bullet, header row) has to use that
font AND pass through shape() first (arabic_reshaper joins the letters, bidi
sets the right-to-left order). Having each call site do both by hand is how the
header table and the AI summary paragraphs were missed. Route text through
pdf_text() / plain_text() and give the style `fontName=unicode_font()`.

Deliberately dependency-light: reportlab only for the font registry, and
arabic_reshaper/bidi imported lazily so a missing package degrades to
unshaped text instead of failing the import.
"""

from __future__ import annotations

import html
import re

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

_UNICODE_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
]

_ARABIC_CHARS_RE = re.compile(r"[؀-ۿݐ-ݿ]")


def unicode_font() -> str:
    """Register (once) and return a Unicode-capable font name for body text."""
    if "UnicodeBody" in pdfmetrics.getRegisteredFontNames():
        return "UnicodeBody"
    for path in _UNICODE_FONT_CANDIDATES:
        try:
            pdfmetrics.registerFont(TTFont("UnicodeBody", path))
            return "UnicodeBody"
        except Exception:
            continue
    return "Helvetica"  # fallback: Arabic will not render, but nothing crashes


def has_arabic(text: str) -> bool:
    return bool(_ARABIC_CHARS_RE.search(str(text or "")))


def shape(text: str) -> str:
    """Make Arabic text renderable by reportlab (connected glyphs, RTL order)."""
    text = str(text or "")
    if not _ARABIC_CHARS_RE.search(text):
        return text
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


def _clean(text, limit: int | None) -> str:
    text = str(text or "").replace("\n", " ").strip()
    if limit and len(text) > limit:
        text = text[:limit] + "..."
    return text


def pdf_text(text, limit: int | None = None) -> str:
    """Escape + shape a string for use INSIDE a reportlab Paragraph.

    Paragraph parses its argument as markup, so '&' and '<' must be escaped -
    and escaping happens AFTER shaping, which only touches Arabic letters.
    """
    return html.escape(shape(_clean(text, limit)))


def plain_text(text, limit: int | None = None) -> str:
    """Shape (but do NOT escape) a string for a bare Table cell.

    A plain-string cell is drawn verbatim, so escaping would print a literal
    '&amp;'. The cell's style must still name unicode_font().
    """
    return shape(_clean(text, limit))
