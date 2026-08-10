from reportlab.lib.pagesizes import LETTER
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib import utils
import re
from html import unescape
from xml.sax.saxutils import escape as xml_escape

def html_to_flowables(html: str):
    """
    Minimal HTML handling for a controlled subset:
    <h2>, <h3>, <p>, <ul>, <li>, <blockquote>
    """
    styles = getSampleStyleSheet()
    story = []

    # Very small/controlled parsing: split on tags we care about.
    # This is not a general HTML parser—kept simple for consistent LLM output.
    html = unescape(html)
    html = re.sub(r"\s+", " ", html).strip()

    # Convert blockquote to italics-ish
    html = html.replace("<blockquote>", "<p><i>").replace("</blockquote>", "</i></p>")

    # Split by block tags
    blocks = re.split(r"(?i)(?=<h2>|<h3>|<p>|<ul>)", html)
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        if b.lower().startswith("<h2>"):
            txt = xml_escape(re.sub(r"(?i)</?h2>", "", b).strip())
            story.append(Paragraph(f"<b>{txt}</b>", styles["Heading2"]))
            story.append(Spacer(1, 0.15 * inch))
        elif b.lower().startswith("<h3>"):
            txt = xml_escape(re.sub(r"(?i)</?h3>", "", b).strip())
            story.append(Paragraph(f"<b>{txt}</b>", styles["Heading3"]))
            story.append(Spacer(1, 0.10 * inch))
        elif b.lower().startswith("<p>"):
            # <p><i>...</i></p> from the blockquote conversion above still
            # needs its inner text escaped without escaping our own <i> tags.
            inner = re.sub(r"(?i)</?p>", "", b).strip()
            txt = _escape_preserving_italic(inner)
            story.append(Paragraph(txt, styles["BodyText"]))
            story.append(Spacer(1, 0.12 * inch))
        elif b.lower().startswith("<ul>"):
            # extract li items
            items = re.findall(r"(?i)<li>(.*?)</li>", b)
            lf = ListFlowable(
                [ListItem(Paragraph(xml_escape(i.strip()), styles["BodyText"])) for i in items],
                bulletType="bullet",
                leftIndent=18,
            )
            story.append(lf)
            story.append(Spacer(1, 0.12 * inch))
        else:
            # fallback: strip tags and add paragraph
            txt = xml_escape(re.sub(r"<[^>]+>", "", b).strip())
            if txt:
                story.append(Paragraph(txt, styles["BodyText"]))
                story.append(Spacer(1, 0.12 * inch))
    return story


def _escape_preserving_italic(text: str) -> str:
    """
    Escape everything except the literal <i>/</i> wrapper this module adds
    itself for blockquote-derived paragraphs, so stray '<'/'&'/'>' from the
    LLM's own text can't be misinterpreted as ReportLab markup.
    """
    if text.startswith("<i>") and text.endswith("</i>"):
        return f"<i>{xml_escape(text[3:-4])}</i>"
    return xml_escape(text)

def render_pdf(pdf_path: str, title: str, meta_description: str, html_body: str, scripture_refs: list[str], takeaways: list[str]):
    doc = SimpleDocTemplate(pdf_path, pagesize=LETTER, rightMargin=54, leftMargin=54, topMargin=54, bottomMargin=54)
    styles = getSampleStyleSheet()

    story = []
    story.append(Paragraph(f"<b>{xml_escape(title)}</b>", styles["Title"]))
    story.append(Spacer(1, 0.15 * inch))

    if meta_description:
        story.append(Paragraph(xml_escape(meta_description), styles["Italic"]))
        story.append(Spacer(1, 0.2 * inch))

    story += html_to_flowables(html_body)

    if takeaways:
        story.append(Spacer(1, 0.2 * inch))
        story.append(Paragraph("<b>Key Takeaways</b>", styles["Heading3"]))
        story.append(Spacer(1, 0.08 * inch))
        lf = ListFlowable([ListItem(Paragraph(xml_escape(t), styles["BodyText"])) for t in takeaways], bulletType="bullet", leftIndent=18)
        story.append(lf)

    if scripture_refs:
        story.append(Spacer(1, 0.2 * inch))
        story.append(Paragraph("<b>Scripture References (KJV)</b>", styles["Heading3"]))
        story.append(Spacer(1, 0.08 * inch))
        lf = ListFlowable([ListItem(Paragraph(xml_escape(r), styles["BodyText"])) for r in scripture_refs], bulletType="bullet", leftIndent=18)
        story.append(lf)

    doc.build(story)