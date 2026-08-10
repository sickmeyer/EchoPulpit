from __future__ import annotations

from html.parser import HTMLParser
from typing import List, Optional
from xml.sax.saxutils import escape as xml_escape

import markdown as _markdown
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer

# ReportLab's Paragraph understands a small set of inline markup tags
# natively (b, i, u, ...) -- map the HTML tags markdown produces onto them.
_INLINE_TAG_MAP = {"strong": "b", "b": "b", "em": "i", "i": "i"}
_BLOCK_TAGS = {"h1", "h2", "h3", "p", "blockquote", "ul", "li"}


class _FlowableBuilder(HTMLParser):
    """
    Walks markdown-derived HTML (h1/h2/h3/p/ul/li/blockquote, with inline
    strong/em spans) into a list of ReportLab flowables, using a block-type
    stack rather than splitting on top-level tags -- so nested structure
    (blockquote>p, inline spans inside p/li/blockquote) renders correctly,
    which the previous regex-based block-splitter couldn't handle.

    All text nodes are XML-escaped as they're accumulated; only the inline
    tags this class itself emits (<b>/<i>) are ever left unescaped, so
    stray '<'/'&'/'>' in the source text can't be misinterpreted as
    ReportLab markup (same discipline as the escaping fix this replaced).
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.styles = getSampleStyleSheet()
        self.blockquote_style = ParagraphStyle(
            "ScriptureQuote", parent=self.styles["Italic"], leftIndent=18, spaceAfter=6,
        )
        self.story: List = []
        self.saw_h1 = False
        self._block_stack: List[str] = []
        self._text_parts: List[str] = []
        self._list_items: List[str] = []

    def _flush_paragraph(self, tag: str):
        text = "".join(self._text_parts).strip()
        self._text_parts = []
        if not text:
            return
        if tag == "h1":
            self.saw_h1 = True
            self.story.append(Paragraph(f"<b>{text}</b>", self.styles["Title"]))
            self.story.append(Spacer(1, 0.2 * inch))
        elif tag == "h2":
            self.story.append(Paragraph(f"<b>{text}</b>", self.styles["Heading2"]))
            self.story.append(Spacer(1, 0.15 * inch))
        elif tag == "h3":
            self.story.append(Paragraph(f"<b>{text}</b>", self.styles["Heading3"]))
            self.story.append(Spacer(1, 0.1 * inch))
        elif tag == "p":
            in_blockquote = "blockquote" in self._block_stack
            style = self.blockquote_style if in_blockquote else self.styles["BodyText"]
            self.story.append(Paragraph(text, style))
            self.story.append(Spacer(1, 0.12 * inch))

    def handle_starttag(self, tag, attrs):
        if tag in _INLINE_TAG_MAP:
            self._text_parts.append(f"<{_INLINE_TAG_MAP[tag]}>")
            return
        if tag == "br":
            self._text_parts.append("<br/>")
            return
        if tag in _BLOCK_TAGS:
            self._block_stack.append(tag)

    def handle_endtag(self, tag):
        if tag in _INLINE_TAG_MAP:
            self._text_parts.append(f"</{_INLINE_TAG_MAP[tag]}>")
            return

        if tag in ("h1", "h2", "h3", "p"):
            self._flush_paragraph(tag)
        elif tag == "li":
            text = "".join(self._text_parts).strip()
            self._text_parts = []
            if text:
                self._list_items.append(text)
        elif tag == "ul":
            if self._list_items:
                lf = ListFlowable(
                    [ListItem(Paragraph(item, self.styles["BodyText"])) for item in self._list_items],
                    bulletType="bullet", leftIndent=18,
                )
                self.story.append(lf)
                self.story.append(Spacer(1, 0.12 * inch))
            self._list_items = []

        if self._block_stack and self._block_stack[-1] == tag:
            self._block_stack.pop()

    def handle_data(self, data):
        if data:
            self._text_parts.append(xml_escape(data))


def markdown_to_flowables(md_text: str):
    html = _markdown.markdown(md_text or "")
    parser = _FlowableBuilder()
    parser.feed(html)
    parser.close()
    return parser.story, parser.saw_h1


def render_pdf(
    pdf_path: str,
    title: str,
    meta_description: str,
    article_markdown: str,
    scripture_refs: List[str],
    reviewer_notes: Optional[dict] = None,
):
    doc = SimpleDocTemplate(pdf_path, pagesize=LETTER, rightMargin=54, leftMargin=54, topMargin=54, bottomMargin=54)
    styles = getSampleStyleSheet()

    body_story, saw_h1 = markdown_to_flowables(article_markdown)

    story = []
    # The article body should carry its own '# Title' (H1); only fall back
    # to a separately-rendered title if the model's output didn't include
    # one, so the common case never duplicates the title.
    if not saw_h1:
        story.append(Paragraph(f"<b>{xml_escape(title)}</b>", styles["Title"]))
        story.append(Spacer(1, 0.2 * inch))

    if meta_description:
        story.append(Paragraph(xml_escape(meta_description), styles["Italic"]))
        story.append(Spacer(1, 0.2 * inch))

    story += body_story

    if scripture_refs:
        story.append(Spacer(1, 0.2 * inch))
        story.append(Paragraph("<b>Scripture References (KJV)</b>", styles["Heading3"]))
        story.append(Spacer(1, 0.08 * inch))
        lf = ListFlowable(
            [ListItem(Paragraph(xml_escape(r), styles["BodyText"])) for r in scripture_refs],
            bulletType="bullet", leftIndent=18,
        )
        story.append(lf)

    reviewer_notes = reviewer_notes or {}
    flags = reviewer_notes.get("flags") or []
    corrections = reviewer_notes.get("corrections") or []
    additions = reviewer_notes.get("additions") or []
    if flags or corrections or additions:
        story.append(Spacer(1, 0.2 * inch))
        story.append(Paragraph("<b>Reviewer Notes (before publishing)</b>", styles["Heading3"]))
        story.append(Spacer(1, 0.08 * inch))
        for label, items in (("Flags", flags), ("Corrections made", corrections), ("Verses added", additions)):
            if not items:
                continue
            story.append(Paragraph(f"<b>{label}</b>", styles["BodyText"]))
            lf = ListFlowable(
                [ListItem(Paragraph(xml_escape(i), styles["BodyText"])) for i in items],
                bulletType="bullet", leftIndent=18,
            )
            story.append(lf)
            story.append(Spacer(1, 0.08 * inch))

    doc.build(story)
