import os
import sys

import pytest

import sermon_pipeline as sp
from prompts import LANGUAGE_INSTRUCTIONS
from scripture_lookup import (
    bible_available,
    canonical_reference_string,
    lookup_verse_text,
    normalize_reference,
    verify_and_correct_scripture,
)

RVG = {
    "John 3:16": "Porque de tal manera amó Dios al mundo, que ha dado a su Hijo unigénito.",
    "1 Corinthians 13:4": "La caridad es sufrida, es benigna.",
    "1 Corinthians 13:5": "No hace nada indebido.",
    "Psalms 23:1": "Jehová es mi pastor; nada me faltará.",
    "Matthew 6:9": "Vosotros, pues, oraréis así: Padre nuestro que estás en el cielo, santificado sea tu nombre.",
}


# ---- references ----

@pytest.mark.parametrize("ref,expected", [
    ("Juan 3:16", ("John", 3, 16, 16)),
    ("1 Corintios 13:4-5", ("1 Corinthians", 13, 4, 5)),
    ("Salmos 23:1", ("Psalms", 23, 1, 1)),
    ("Salmo 23:1", ("Psalms", 23, 1, 1)),
    ("Génesis 1:1", ("Genesis", 1, 1, 1)),
    ("Genesis 1:1", ("Genesis", 1, 1, 1)),
    ("Apocalipsis 3:20", ("Revelation", 3, 20, 20)),
    ("Cantares 2:4", ("Song of Solomon", 2, 4, 4)),
    ("Judas 1:24", ("Jude", 1, 24, 24)),
    ("Santiago 1:5", ("James", 1, 5, 5)),
])
def test_spanish_book_names_resolve(ref, expected):
    assert normalize_reference(ref) == expected


def test_canonical_reference_in_spanish():
    assert canonical_reference_string("juan 3:16", "es") == "Juan 3:16"
    assert canonical_reference_string("1 Corinthians 13:4-5", "es") == "1 Corintios 13:4-5"
    assert canonical_reference_string("Juan 3:16") == "John 3:16"  # English default unchanged


def test_lookup_spanish_range():
    assert lookup_verse_text("1 Corintios 13:4-5", RVG) == "La caridad es sufrida, es benigna. No hace nada indebido."


def test_verify_spanish_corrects_and_keeps_spanish_names():
    body = ('Texto.\n\n> "Porque de tal manera amo Dios al mundo" (Juan 3:16)\n\n'
            '> «Jehová es mi pastor» (Salmo 23:1)\n\n> "Inventado" (Juan 99:1)\n\nFin.')
    out, refs, flags = verify_and_correct_scripture(body, kjv=RVG, lang="es")
    assert '> "Porque de tal manera amó Dios al mundo, que ha dado a su Hijo unigénito." (Juan 3:16)' in out
    assert '> "Jehová es mi pastor; nada me faltará." (Salmos 23:1)' in out
    assert "Juan 99:1" not in out
    assert refs == ["Juan 3:16", "Salmos 23:1"]
    assert len(flags) == 1 and "Reina Valera Gómez" in flags[0]


def test_rvg_dataset_bundled():
    assert bible_available("es") and bible_available("en")


# ---- language plumbing ----

def test_detect_language():
    assert sp.detect_language("Servicio en Español") == "es"
    assert sp.detect_language("Por Poco... | Servicio en Español") == "es"
    assert sp.detect_language("Sunday Main Worship") == "en"


CFG = {
    "transcription": {"language": "en", "caption_langs": ["en"]},
    "languages": {"es": {"whisper_language": "es", "caption_langs": ["es", "es-419"],
                         "default_preacher": "Nelson Bonilla"}},
    "church": {"timezone": "America/Chicago"},
}


def test_transcription_cfg_for_spanish():
    t = sp.transcription_cfg_for(CFG, "es")
    assert t["language"] == "es" and t["caption_langs"] == ["es", "es-419"]
    assert sp.transcription_cfg_for(CFG, "en")["language"] == "en"


def test_default_preacher_for_spanish_only():
    es = sp.build_service_info(CFG, "subsplash-x", "Servicio en Español", "2026-09-20T12:00:00Z", 3200, language="es")
    assert es["preacher"] == "Nelson Bonilla"
    en = sp.build_service_info(CFG, "vid", "Weekly Bible Hour", "2026-09-20T12:00:00Z", 3200)
    assert "preacher" not in en


def test_spanish_prompt_block():
    block = LANGUAGE_INSTRUCTIONS["es"]
    assert "Reina Valera Gómez" in block and "English" in block and "Adaptado de un mensaje" in block
    assert LANGUAGE_INSTRUCTIONS["en"] == ""


def test_article_html_lang():
    assert sp.build_article_html({"title": "T", "language": "es", "article_markdown": "x"}).startswith(
        '<!doctype html>\n<html lang="es">')


# ---- notifier / publisher ----

def _lambdas():
    os.environ.setdefault("SES_SENDER_ADDRESS", "a@example.com")
    os.environ.setdefault("NOTIFY_RECIPIENT_ADDRESS", "a@example.com")
    os.environ.setdefault("SERMON_ARTIFACTS_BUCKET", "bkt")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deploy", "lambdas"))
    import notifier_lambda
    import publisher_lambda
    return notifier_lambda, publisher_lambda


def test_spanish_articles_also_go_to_spanish_reviewer(monkeypatch):
    n, _ = _lambdas()
    monkeypatch.setenv("NOTIFY_EXTRA_RECIPIENTS_ES", "nelson@example.org, A@example.com")
    assert n._recipients("es") == ["a@example.com", "nelson@example.org"]
    assert n._recipients("en") == ["a@example.com"]


def test_published_spanish_post_has_lang():
    _, pl = _lambdas()
    article = {"title": "Santificado Sea Tu Nombre", "slug": "santificado-sea-tu-nombre-mateo-6-9",
               "meta_description": "d", "language": "es", "article_markdown": "Cuerpo."}
    job = {"video_id": "subsplash-x", "title": "Servicio en Español"}
    _, text = pl.build_post(article, job, "Nelson Bonilla", "2026-09-20", None)
    assert "lang: es" in text and "service: Servicio en Español" in text
    _, en_text = pl.build_post({**article, "language": "en"}, job, "", "2026-09-20", None)
    assert "lang:" not in en_text
