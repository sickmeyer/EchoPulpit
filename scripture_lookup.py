from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Dict, List, Optional, Tuple

KJV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "kjv.json")
# Reina Valera Gómez, for Spanish articles -- same "Book C:V" keys (English
# canonical book names) as kjv.json; see scripts/build_rvg.py.
RVG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "rvg.json")
BIBLES = {"en": (KJV_PATH, "KJV"), "es": (RVG_PATH, "Reina Valera Gómez")}

# The 66 canonical book names as they appear as keys in data/kjv.json
# (straight from the source dataset's own "book" field). Used to
# case-normalize whatever casing the model used ("ephesians" -> "Ephesians")
# without naively title-casing multi-word names, which would wrongly
# capitalize "of" in "Song of Solomon".
CANONICAL_BOOK_NAMES = [
    "Genesis", "Exodus", "Leviticus", "Numbers", "Deuteronomy", "Joshua", "Judges", "Ruth",
    "1 Samuel", "2 Samuel", "1 Kings", "2 Kings", "1 Chronicles", "2 Chronicles", "Ezra",
    "Nehemiah", "Esther", "Job", "Psalms", "Proverbs", "Ecclesiastes", "Song of Solomon",
    "Isaiah", "Jeremiah", "Lamentations", "Ezekiel", "Daniel", "Hosea", "Joel", "Amos",
    "Obadiah", "Jonah", "Micah", "Nahum", "Habakkuk", "Zephaniah", "Haggai", "Zechariah",
    "Malachi", "Matthew", "Mark", "Luke", "John", "Acts", "Romans", "1 Corinthians",
    "2 Corinthians", "Galatians", "Ephesians", "Philippians", "Colossians", "1 Thessalonians",
    "2 Thessalonians", "1 Timothy", "2 Timothy", "Titus", "Philemon", "Hebrews", "James",
    "1 Peter", "2 Peter", "1 John", "2 John", "3 John", "Jude", "Revelation",
]
_CANONICAL_BY_LOWER = {name.lower(): name for name in CANONICAL_BOOK_NAMES}

# Common real-world variants that don't match a canonical name at all (not
# just different casing).
BOOK_ALIASES = {
    "psalm": "Psalms",
    "song of songs": "Song of Solomon",
    "song of songs of solomon": "Song of Solomon",
    "canticles": "Song of Solomon",
    "revelations": "Revelation",
}

# Spanish book names (Reina Valera), aligned with CANONICAL_BOOK_NAMES.
SPANISH_BOOK_NAMES = [
    "Génesis", "Éxodo", "Levítico", "Números", "Deuteronomio", "Josué", "Jueces", "Rut",
    "1 Samuel", "2 Samuel", "1 Reyes", "2 Reyes", "1 Crónicas", "2 Crónicas", "Esdras",
    "Nehemías", "Ester", "Job", "Salmos", "Proverbios", "Eclesiastés", "Cantares",
    "Isaías", "Jeremías", "Lamentaciones", "Ezequiel", "Daniel", "Oseas", "Joel", "Amós",
    "Abdías", "Jonás", "Miqueas", "Nahúm", "Habacuc", "Sofonías", "Hageo", "Zacarías",
    "Malaquías", "Mateo", "Marcos", "Lucas", "Juan", "Hechos", "Romanos", "1 Corintios",
    "2 Corintios", "Gálatas", "Efesios", "Filipenses", "Colosenses", "1 Tesalonicenses",
    "2 Tesalonicenses", "1 Timoteo", "2 Timoteo", "Tito", "Filemón", "Hebreos", "Santiago",
    "1 Pedro", "2 Pedro", "1 Juan", "2 Juan", "3 Juan", "Judas", "Apocalipsis",
]


def _fold(name: str) -> str:
    """Lowercase, accents removed, single spaces -- for matching book names."""
    text = unicodedata.normalize("NFKD", name)
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).lower().split())


_SPANISH_BY_FOLDED = {_fold(es): en for es, en in zip(SPANISH_BOOK_NAMES, CANONICAL_BOOK_NAMES)}
_SPANISH_BY_FOLDED.update({
    "salmo": "Psalms",
    "cantar de los cantares": "Song of Solomon",
    "cantar": "Song of Solomon",
    "hechos de los apostoles": "Acts",
})
_SPANISH_NAME = dict(zip(CANONICAL_BOOK_NAMES, SPANISH_BOOK_NAMES))

_kjv_cache: Optional[Dict[str, str]] = None
_bible_cache: Dict[str, Dict[str, str]] = {}


def kjv_available() -> bool:
    """Whether the bundled KJV dataset exists on disk. Verification should
    be skipped entirely (not run against an empty dict) when this is False
    -- an empty dataset would make every single citation look
    "unverifiable" and strip all of them, which is the opposite of what a
    missing dataset should mean."""
    return os.path.exists(KJV_PATH)


def bible_available(lang: str = "en") -> bool:
    """Whether the bundled Bible for this language exists on disk (see kjv_available)."""
    return lang in BIBLES and os.path.exists(BIBLES[lang][0])


def load_bible(lang: str = "en") -> Dict[str, str]:
    if lang == "en":
        return load_kjv()
    if lang not in _bible_cache:
        with open(BIBLES[lang][0], "r", encoding="utf-8") as f:
            _bible_cache[lang] = json.load(f)
    return _bible_cache[lang]


def load_kjv() -> Dict[str, str]:
    global _kjv_cache
    if _kjv_cache is None:
        with open(KJV_PATH, "r", encoding="utf-8") as f:
            _kjv_cache = json.load(f)
    return _kjv_cache


_REF_RE = re.compile(r"^\s*(?P<book>.+?)\s+(?P<chapter>\d+):(?P<start>\d+)(?:-(?P<end>\d+))?\s*$")


def normalize_reference(ref: str) -> Optional[Tuple[str, int, int, int]]:
    """Parse 'Book Chapter:Verse' or 'Book Chapter:Verse-Verse' into
    (canonical_book, chapter, start_verse, end_verse). Returns None if the
    string doesn't parse as a reference at all."""
    m = _REF_RE.match(ref or "")
    if not m:
        return None
    book = m.group("book").strip()
    book_lower = book.lower()
    book = (BOOK_ALIASES.get(book_lower) or _CANONICAL_BY_LOWER.get(book_lower)
            or _SPANISH_BY_FOLDED.get(_fold(book)) or book)
    chapter = int(m.group("chapter"))
    start = int(m.group("start"))
    end = int(m.group("end")) if m.group("end") else start
    if end < start:
        return None
    return book, chapter, start, end


def lookup_verse_text(ref: str, kjv: Dict[str, str]) -> Optional[str]:
    """Exact KJV text for a reference, joining verse ranges with a space.
    Returns None if the reference doesn't parse or any verse in the range
    isn't found (a partial range is not considered a match -- if we can't
    verify the whole thing, we don't claim to)."""
    parsed = normalize_reference(ref)
    if not parsed:
        return None
    book, chapter, start, end = parsed
    verses = []
    for v in range(start, end + 1):
        key = f"{book} {chapter}:{v}"
        text = kjv.get(key)
        if text is None:
            return None
        verses.append(text)
    return " ".join(verses)


def canonical_reference_string(ref: str, lang: str = "en") -> Optional[str]:
    """Render a parsed reference back out in canonical 'Book C:V' /
    'Book C:V-V' form, using the exact book-name spelling from data/kjv.json
    rather than whatever variant appeared in the model's output."""
    parsed = normalize_reference(ref)
    if not parsed:
        return None
    book, chapter, start, end = parsed
    if lang == "es":
        book = _SPANISH_NAME.get(book, book)
    if start == end:
        return f"{book} {chapter}:{start}"
    return f"{book} {chapter}:{start}-{end}"


# Straight quotes, or the curly/angle quotes Spanish text often uses.
_BLOCKQUOTE_RE = re.compile(
    r'^>\s*(?:"(?P<q1>[^"]*)"|“(?P<q2>[^”]*)”|«(?P<q3>[^»]*)»)\s*\((?P<ref>[^)]+)\)\s*$', re.MULTILINE)


def verify_and_correct_scripture(
    markdown_body: str, kjv: Optional[Dict[str, str]] = None, lang: str = "en"
) -> Tuple[str, List[str], List[str]]:
    """
    Parse every '> "..." (Reference)' scripture blockquote out of the
    article body, replace the quoted text with the authoritative KJV
    wording when the reference resolves, and remove the line entirely when
    it doesn't (never present an unverified reference as if it were
    checked). Returns (corrected_body, surviving_references, flags) --
    surviving_references is the ground truth for frontmatter's
    scripture_references, and flags describes every removal for
    reviewer_notes.
    """
    if kjv is None:
        kjv = load_bible(lang)
    bible_name = BIBLES.get(lang, BIBLES["en"])[1]

    surviving: List[str] = []
    flags: List[str] = []

    def _replace(m: re.Match) -> str:
        ref = m.group("ref").strip()
        text = lookup_verse_text(ref, kjv)
        if text is None:
            flags.append(
                f"Removed unverifiable scripture citation \"{ref}\" -- "
                f"reference did not resolve against the bundled {bible_name} text."
            )
            return ""
        canonical = canonical_reference_string(ref, lang) or ref
        surviving.append(canonical)
        return f'> "{text}" ({canonical})'

    corrected = _BLOCKQUOTE_RE.sub(_replace, markdown_body)
    # Collapse any blank line left behind by a removed blockquote so the
    # article doesn't end up with a visible gap.
    corrected = re.sub(r"\n{3,}", "\n\n", corrected)

    return corrected, surviving, flags
