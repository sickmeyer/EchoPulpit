import pytest

from sermon_pipeline import parse_article_markdown, normalize_article_frontmatter


VALID_DOC = """---
title: "The Man Who Stood Alone"
slug: "the-man-who-stood-alone"
meta_description: "A message on 2 Samuel 23 about standing firm when others quit."
focus_keyword: "standing firm in faith"
keywords: ["faithfulness", "2 Samuel 23"]
primary_passage: "2 Samuel 23:9-10"
scripture_references: ["2 Samuel 23:9-10", "Ephesians 6:13"]
preacher: "Test Pastor"
preached_on: "2026-08-09"
word_count: 1850
needs_review: true
alternate_titles:
  - "Title Two"
  - "Title Three"
reviewer_notes:
  corrections:
    - "Fixed a misattributed reference."
  additions:
    - "Added Ephesians 6:13 as supporting scripture."
  flags:
    - "Section on cultural commentary should be reviewed."
---

# The Man Who Stood Alone

This is the opening paragraph.

## A King Chooses the Cause

Some prose here.

> "Wherefore take unto you the whole armour of God." (Ephesians 6:13)

*Adapted from a message preached from 2 Samuel 23 by Test Pastor.*
"""


def test_parse_article_markdown_valid_document():
    frontmatter, body = parse_article_markdown(VALID_DOC)
    assert frontmatter["title"] == "The Man Who Stood Alone"
    assert frontmatter["primary_passage"] == "2 Samuel 23:9-10"
    assert body.startswith("# The Man Who Stood Alone")
    assert "A King Chooses the Cause" in body


def test_parse_article_markdown_strips_markdown_fences():
    fenced = "```\n" + VALID_DOC + "\n```"
    frontmatter, body = parse_article_markdown(fenced)
    assert frontmatter["title"] == "The Man Who Stood Alone"


def test_parse_article_markdown_ignores_later_horizontal_rules():
    # A '---' used as a markdown horizontal rule later in the body must not
    # be mistaken for a second frontmatter block.
    doc_with_hr = VALID_DOC.replace(
        "## A King Chooses the Cause",
        "---\n\n## A King Chooses the Cause",
    )
    frontmatter, body = parse_article_markdown(doc_with_hr)
    assert frontmatter["title"] == "The Man Who Stood Alone"
    assert "## A King Chooses the Cause" in body


def test_parse_article_markdown_missing_frontmatter_raises():
    with pytest.raises(ValueError):
        parse_article_markdown("# Just a markdown document\n\nNo frontmatter at all.")


def test_parse_article_markdown_unclosed_frontmatter_raises():
    with pytest.raises(ValueError):
        parse_article_markdown('---\ntitle: "x"\n\n# No closing delimiter\n')


def test_parse_article_markdown_empty_body_raises():
    with pytest.raises(ValueError):
        parse_article_markdown('---\ntitle: "x"\n---\n\n')


def test_parse_article_markdown_non_dict_frontmatter_raises():
    with pytest.raises(ValueError):
        parse_article_markdown("---\n- just\n- a\n- list\n---\n\nSome body text.\n")


def test_normalize_article_frontmatter_fills_defaults():
    fm = normalize_article_frontmatter({"title": "Only Title"})
    assert fm["title"] == "Only Title"
    assert fm["keywords"] == []
    assert fm["scripture_references"] == []
    assert fm["alternate_titles"] == []
    assert fm["needs_review"] is True
    assert fm["word_count"] == 0
    assert fm["reviewer_notes"] == {"corrections": [], "additions": [], "flags": []}


def test_normalize_article_frontmatter_truncates_oversized_alternate_titles():
    fm = normalize_article_frontmatter({"alternate_titles": [f"Title {i}" for i in range(10)]})
    assert len(fm["alternate_titles"]) == 7


def test_normalize_article_frontmatter_coerces_non_string_scalars():
    fm = normalize_article_frontmatter({"title": None, "slug": 12345})
    assert fm["title"] == ""
    assert fm["slug"] == "12345"


def test_normalize_article_frontmatter_coerces_non_list_to_empty_list():
    fm = normalize_article_frontmatter({"keywords": "not a list"})
    assert fm["keywords"] == []


def test_normalize_article_frontmatter_coerces_bad_word_count():
    fm = normalize_article_frontmatter({"word_count": "not a number"})
    assert fm["word_count"] == 0


def test_normalize_article_frontmatter_reviewer_notes_partial_fills_missing_keys():
    fm = normalize_article_frontmatter({"reviewer_notes": {"flags": ["a flag"]}})
    assert fm["reviewer_notes"]["flags"] == ["a flag"]
    assert fm["reviewer_notes"]["corrections"] == []
    assert fm["reviewer_notes"]["additions"] == []


def test_normalize_article_frontmatter_reviewer_notes_non_dict_becomes_empty():
    fm = normalize_article_frontmatter({"reviewer_notes": "not a dict"})
    assert fm["reviewer_notes"] == {"corrections": [], "additions": [], "flags": []}
