import os

import pytest

reportlab = pytest.importorskip("reportlab", reason="reportlab not installed in this environment")

from render_pdf import markdown_to_flowables, render_pdf


def test_markdown_to_flowables_renders_h1_and_sets_saw_h1():
    story, saw_h1 = markdown_to_flowables("# The Title\n\nSome body text.")
    assert saw_h1 is True
    assert len(story) > 0


def test_markdown_to_flowables_no_h1_reports_false():
    story, saw_h1 = markdown_to_flowables("Just a paragraph, no heading.")
    assert saw_h1 is False
    assert len(story) > 0


def test_markdown_to_flowables_handles_nested_blockquote():
    md = '> "Wherefore take unto you the whole armour of God." (Ephesians 6:13)'
    story, _ = markdown_to_flowables(md)
    assert len(story) > 0


def test_markdown_to_flowables_handles_inline_bold_and_italic():
    md = "This has **bold** and *italic* and a stray < bracket & ampersand."
    story, _ = markdown_to_flowables(md)
    assert len(story) > 0


def test_markdown_to_flowables_handles_unordered_list():
    md = "- Step one\n- Step two with **bold**\n- Step three"
    story, _ = markdown_to_flowables(md)
    assert len(story) > 0


def test_markdown_to_flowables_handles_h2_h3_sections():
    md = "## Section One\n\nProse.\n\n### Subsection\n\nMore prose."
    story, _ = markdown_to_flowables(md)
    assert len(story) > 0


def test_render_pdf_end_to_end_with_full_article(tmp_path):
    pdf_path = str(tmp_path / "out.pdf")
    md = """# A Title With <angle> & Ampersand

Opening paragraph with a stray < bracket and & ampersand, plus **bold** and *italic*.

## First Section

> "Some scripture text here." (Ephesians 6:13)

## Practical Application

- Step one & two
- Step two with **bold**

*Adapted from a message preached from Ephesians 6 by Test Pastor.*
"""
    render_pdf(
        pdf_path=pdf_path,
        title="Fallback Title (unused since body has an H1)",
        meta_description="Description with < and & chars",
        article_markdown=md,
        scripture_refs=["Ephesians 6:13"],
        reviewer_notes={
            "flags": ["Cultural commentary section should be reviewed"],
            "corrections": ["Fixed a misattributed reference"],
            "additions": ["Romans 8:28"],
        },
    )
    assert os.path.getsize(pdf_path) > 0


def test_render_pdf_falls_back_to_title_param_when_body_has_no_h1(tmp_path):
    pdf_path = str(tmp_path / "out2.pdf")
    render_pdf(
        pdf_path=pdf_path,
        title="Fallback Title Used",
        meta_description="",
        article_markdown="Just a paragraph, no heading at all.",
        scripture_refs=[],
        reviewer_notes=None,
    )
    assert os.path.getsize(pdf_path) > 0


def test_render_pdf_with_no_reviewer_notes_or_scripture(tmp_path):
    pdf_path = str(tmp_path / "out3.pdf")
    render_pdf(
        pdf_path=pdf_path,
        title="Title",
        meta_description="",
        article_markdown="# Title\n\nBody text.",
        scripture_refs=[],
        reviewer_notes={"flags": [], "corrections": [], "additions": []},
    )
    assert os.path.getsize(pdf_path) > 0
