"""
Word doc styles for research reports.

Registers paragraph and table styles on a python-docx Document so the
renderer can apply them by name. Kept isolated from render logic to keep
word_report.py readable.

Usage:
    from docx import Document
    from research.word_styles import apply_styles
    doc = Document()
    apply_styles(doc)
    doc.add_paragraph("...", style="PullQuote")
"""

from __future__ import annotations

from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Pt, RGBColor, Inches


# Color palette -- muted, readable, institutional
COLOR_BODY = RGBColor(0x1A, 0x1A, 0x1A)           # near-black body
COLOR_HEADING = RGBColor(0x0F, 0x2A, 0x4E)        # deep navy
COLOR_SUBHEADING = RGBColor(0x33, 0x4B, 0x6B)     # softer navy
COLOR_PULLQUOTE = RGBColor(0x55, 0x55, 0x55)      # gray italic
COLOR_ACCENT = RGBColor(0x8C, 0x1D, 0x40)         # muted crimson for PT/direction
COLOR_TABLE_HEADER_BG = "E8EDF5"                   # very light navy
COLOR_TABLE_BORDER = "BDC3C7"
COLOR_WARNING_BG = "FBE9EC"                        # very light crimson for warning callouts


def apply_styles(doc) -> None:
    """Register all custom paragraph styles on the document."""

    def _add(name, size, *, bold=False, italic=False, color=None,
             font_name="Calibri", space_before=0, space_after=6,
             left_indent=None, line_spacing=1.15):
        styles = doc.styles
        existing = [s.name for s in styles]
        if name in existing:
            style = styles[name]
        else:
            style = styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        font = style.font
        font.name = font_name
        font.size = Pt(size)
        font.bold = bold
        font.italic = italic
        if color is not None:
            font.color.rgb = color
        pf = style.paragraph_format
        pf.space_before = Pt(space_before)
        pf.space_after = Pt(space_after)
        pf.line_spacing = line_spacing
        if left_indent is not None:
            pf.left_indent = left_indent
        return style

    # Body / Normal
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10.5)
    normal.font.color.rgb = COLOR_BODY
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.15

    # Title + headings
    _add("ReportTitle", 20, bold=True, color=COLOR_HEADING,
         space_before=0, space_after=2)
    _add("SectionHeading", 14, bold=True, color=COLOR_HEADING,
         space_before=14, space_after=4)
    _add("SubHeading", 11, bold=True, color=COLOR_SUBHEADING,
         space_before=6, space_after=2)

    # Pull-quote: italic gray, indented. This is the stress-test formatting.
    _add("PullQuote", 10.5, italic=True, color=COLOR_PULLQUOTE,
         left_indent=Inches(0.3), space_before=4, space_after=6)

    # Kill criteria one-liner at end
    _add("KillCriteria", 10.5, bold=True, color=COLOR_ACCENT,
         space_before=6, space_after=6)

    # Warning banner — for REASONABILITY / EXTRAORDINARY-VARIANT callouts
    # rendered as a shaded single-cell table at the top of the report.
    # Bold, accent-colored text so the reader can't miss it.
    _add("WarningBanner", 11, bold=True, color=COLOR_ACCENT,
         space_before=4, space_after=4)

    # Bullet body
    _add("ReportBullet", 10.5, color=COLOR_BODY, space_after=3)

    # Table caption
    _add("ReportCaption", 9, italic=True, color=COLOR_PULLQUOTE,
         space_before=2, space_after=2)

    # Small meta line (ticker | date | source)
    _add("SmallMeta", 9, color=COLOR_PULLQUOTE, space_after=4)


def shade_cell(cell, hex_fill: str = COLOR_TABLE_HEADER_BG) -> None:
    """Apply a background fill color to a table cell (XML level)."""
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tc_pr.append(shd)


def set_cell_border(cell, *, top=True, bottom=True, left=True, right=True,
                    color: str = COLOR_TABLE_BORDER, size: int = 4) -> None:
    """Add thin borders to a cell."""
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_borders = tc_pr.find(qn("w:tcBorders"))
    if tc_borders is None:
        tc_borders = OxmlElement("w:tcBorders")
        tc_pr.append(tc_borders)
    edges = []
    if top: edges.append("top")
    if bottom: edges.append("bottom")
    if left: edges.append("left")
    if right: edges.append("right")
    for edge in edges:
        el = tc_borders.find(qn(f"w:{edge}"))
        if el is None:
            el = OxmlElement(f"w:{edge}")
            tc_borders.append(el)
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(size))
        el.set(qn("w:color"), color)


def set_table_borders(table, **kwargs) -> None:
    """Apply borders to every cell in a table."""
    for row in table.rows:
        for cell in row.cells:
            set_cell_border(cell, **kwargs)
