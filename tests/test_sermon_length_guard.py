import pytest

from sermon_pipeline import MIN_SERMON_WORDS, check_sermon_text_length


def test_rejects_near_empty_transcript():
    with pytest.raises(RuntimeError, match="only 4 words"):
        check_sermon_text_length("There's no other thing.")


def test_rejects_empty_transcript():
    with pytest.raises(RuntimeError, match="only 0 words"):
        check_sermon_text_length("")


def test_accepts_sermon_length_transcript():
    check_sermon_text_length("word " * MIN_SERMON_WORDS)
