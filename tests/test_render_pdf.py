import pytest

reportlab = pytest.importorskip("reportlab", reason="reportlab not installed in this environment")

from render_pdf import html_to_flowables, render_pdf


def test_html_to_flowables_handles_stray_angle_bracket():
    # An LLM could plausibly emit "if x < y" inside a <p>; this must not be
    # interpreted as the start of an (invalid) ReportLab markup tag.
    html = "<h2>Point One</h2><p>If x < y and a & b, we grow.</p>"
    story = html_to_flowables(html)
    assert len(story) > 0


def test_html_to_flowables_handles_unexpected_tag_content():
    html = "<p>Some text with a <script>alert(1)</script> stray tag inside.</p>"
    story = html_to_flowables(html)
    assert len(story) > 0


def test_html_to_flowables_handles_blockquote():
    html = "<blockquote>A quoted thought with & an ampersand.</blockquote>"
    story = html_to_flowables(html)
    assert len(story) > 0


def test_html_to_flowables_handles_list_items_with_special_chars():
    html = "<ul><li>Step one & two</li><li>x < y</li></ul>"
    story = html_to_flowables(html)
    assert len(story) > 0


def test_render_pdf_end_to_end_with_hostile_content(tmp_path):
    pdf_path = str(tmp_path / "out.pdf")
    # Should not raise even though the "LLM output" contains characters that
    # would break ReportLab's mini-XML parser if left unescaped.
    render_pdf(
        pdf_path=pdf_path,
        title="A Title with <angle> & ampersand",
        meta_description="Description with < and & chars",
        html_body="<h2>Heading & <stuff></h2><p>Body with < and & and \"quotes\".</p>",
        scripture_refs=["John 3:16 KJV", "Romans 8:28 <note> KJV"],
        takeaways=["Point with & ampersand", "Point with < less-than"],
    )
    import os
    assert os.path.getsize(pdf_path) > 0
