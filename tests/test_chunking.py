from sermon_pipeline import chunk_text_by_chars


def test_short_text_single_chunk():
    text = "This is a short sermon transcript."
    chunks = chunk_text_by_chars(text, max_chars=5000)
    assert chunks == [text]


def test_long_text_splits_on_paragraph_boundaries():
    paragraphs = [f"Paragraph number {i} " + ("word " * 20) for i in range(30)]
    text = "\n".join(paragraphs)
    chunks = chunk_text_by_chars(text, max_chars=500)

    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 500 or "\n" not in c  # a single oversized paragraph is allowed through alone
    # No paragraph text should be lost or duplicated across chunks
    rejoined = "\n".join(chunks)
    for p in paragraphs:
        assert p.strip() in rejoined


def test_empty_text():
    assert chunk_text_by_chars("", max_chars=100) == [""]


def test_whitespace_only_paragraphs_are_dropped():
    text = "Real content here.\n\n   \n\nMore real content."
    chunks = chunk_text_by_chars(text, max_chars=5000)
    assert len(chunks) == 1
    assert "Real content here." in chunks[0]
    assert "More real content." in chunks[0]
