import pytest

from sermon_pipeline import parse_article_yaml, normalize_article_schema, _extract_yaml_block


VALID_YAML = """
title_options:
  - "Title One"
  - "Title Two"
chosen_title: "Title One"
meta_title: "Meta Title"
meta_description: "A short description."
slug: "title-one"
primary_keyword: "faith"
secondary_keywords:
  - "grace"
  - "hope"
featured_image_prompt: "a quiet church interior, photorealistic"
outline:
  - "H2 Introduction"
outline_notes: "extra key not in schema, should be ignored by normalize"
article_html: |
  <h2>Introduction</h2>
  <p>Some content.</p>
scripture_references:
  - "John 3:16 KJV"
takeaway_points:
  - "Point one"
  - "Point two"
"""


def test_parse_article_yaml_valid():
    data = parse_article_yaml(VALID_YAML)
    assert data["chosen_title"] == "Title One"
    assert data["primary_keyword"] == "faith"


def test_parse_article_yaml_strips_markdown_fences():
    fenced = "```yaml\n" + VALID_YAML + "\n```"
    data = parse_article_yaml(fenced)
    assert data["chosen_title"] == "Title One"


def test_parse_article_yaml_strips_leading_chatter():
    chatty = "Here is the YAML:\n" + VALID_YAML
    data = parse_article_yaml(chatty)
    assert data["chosen_title"] == "Title One"


def test_parse_article_yaml_rejects_non_dict():
    with pytest.raises(ValueError):
        parse_article_yaml("- just\n- a\n- list\n")


def test_extract_yaml_block_handles_tabs():
    raw = "chosen_title: \"x\"\n\tmeta_title: \"y\""
    extracted = _extract_yaml_block(raw)
    assert "\t" not in extracted


def test_normalize_article_schema_fills_defaults():
    article = normalize_article_schema({"chosen_title": "Only Title"})
    assert article["title_options"] == []
    assert article["takeaway_points"] == []
    assert article["article_html"] == ""
    assert article["chosen_title"] == "Only Title"


def test_normalize_article_schema_truncates_oversized_lists():
    article = normalize_article_schema({
        "title_options": [f"Title {i}" for i in range(10)],
        "takeaway_points": [f"Point {i}" for i in range(10)],
    })
    assert len(article["title_options"]) == 5
    assert len(article["takeaway_points"]) == 5


def test_normalize_article_schema_coerces_non_string_scalars():
    article = normalize_article_schema({"chosen_title": None, "slug": 12345})
    assert article["chosen_title"] == ""
    assert article["slug"] == "12345"


def test_normalize_article_schema_coerces_non_list_to_empty_list():
    article = normalize_article_schema({"outline": "not a list"})
    assert article["outline"] == []
