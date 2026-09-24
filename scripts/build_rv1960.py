"""
Build data/rv1960.json -- the Reina-Valera 1960 text Spanish articles are
verified against (scripture_lookup.verify_and_correct_scripture with
lang="es").

The RVR1960 is copyrighted (© Sociedades Bíblicas en América Latina, 1960;
renewed 1988, Sociedades Bíblicas Unidas). Articles may quote it under the
Bible Societies' standard permission (up to 500 verses, under 25% of the
work, with their notice), but the complete text must NOT be redistributed:
data/rv1960.json is git-ignored and only ever uploaded to the private
artifacts bucket for the worker. Never commit it or publish it.

Source: the per-book files of github.com/alejandroch1202/biblia-api. Keys
match data/kjv.json ("John 3:16", English canonical book names). Refuses to
write anything unless all 66 books are present, the verse count is within a
few verses of 31,102 (Spanish versification differs slightly in places),
and a set of known RVR1960 readings match.

Usage (repo root):  python scripts/build_rv1960.py
"""
import ast
import json
import os
import ssl
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from scripture_lookup import CANONICAL_BOOK_NAMES, SPANISH_BOOK_NAMES, _fold  # noqa: E402

SOURCE = "https://raw.githubusercontent.com/alejandroch1202/biblia-api/HEAD/dist/db/{}.js"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "rv1960.json")
EXPECTED_VERSES = 31102
# Readings that tell the RVR1960 apart from the 1909 and the Gómez.
KNOWN = {
    "Romans 5:8": "Mas Dios muestra su amor para con nosotros",
    "2 Timothy 1:7": "espíritu de cobardía",
    "Hebrews 4:16": "Acerquémonos, pues, confiadamente",
    "Isaiah 40:31": "levantarán alas como las águilas",
    "John 3:16": "Porque de tal manera amó Dios al mundo",
}


def _context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _file_name(spanish: str) -> str:
    return _fold(spanish).replace(" ", "")


def fetch_book(spanish: str, ctx) -> list:
    """Chapters (lists of verse strings) of one book."""
    req = urllib.request.Request(SOURCE.format(_file_name(spanish)),
                                 headers={"User-Agent": "EchoPulpit build_rv1960.py"})
    with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
        js = resp.read().decode("utf-8")
    body = js[js.index("exports.default =") + len("exports.default ="):].strip().rstrip(";")
    data = ast.literal_eval(body)
    # The first element is a one-string book introduction, not a chapter.
    return data[1:] if len(data[0]) == 1 and len(data[0][0]) > 200 else data


def fix_source_errors(bible: dict) -> None:
    """Two numbering errors in the source, found by comparing every verse
    with the Reina Valera Gómez (a revision of the 1960). Each fix checks
    the source still has the error, so a corrected upstream isn't broken."""
    # Genesis 33:12 is missing, so 33:13-20 sit at 12-19.
    if bible["Genesis 33:12"].startswith("Y Jacob le dijo: Mi señor sabe"):
        for v in range(20, 12, -1):
            bible[f"Genesis 33:{v}"] = bible[f"Genesis 33:{v - 1}"]
        bible["Genesis 33:12"] = "Y Esaú dijo: Anda, vamos; y yo iré delante de ti."
    # Psalm 47:9 is split into 9 and 10.
    if "Psalms 47:10" in bible and bible["Psalms 47:10"].startswith("Porque de Dios son los escudos"):
        bible["Psalms 47:9"] += " " + bible.pop("Psalms 47:10")


def main() -> None:
    ctx = _context()
    bible = {}
    for english, spanish in zip(CANONICAL_BOOK_NAMES, SPANISH_BOOK_NAMES):
        chapters = fetch_book(spanish, ctx)
        for c, verses in enumerate(chapters, 1):
            for v, text in enumerate(verses, 1):
                text = " ".join(str(text).split())
                if text:
                    bible[f"{english} {c}:{v}"] = text
        print(f"{spanish}: {len(chapters)} chapters")

    fix_source_errors(bible)
    books = {k.rsplit(" ", 1)[0] for k in bible}
    missing = [b for b in CANONICAL_BOOK_NAMES if b not in books]
    if missing:
        sys.exit(f"Missing books: {missing}")
    if abs(len(bible) - EXPECTED_VERSES) > 30:
        sys.exit(f"{len(bible)} verses, expected about {EXPECTED_VERSES}; not writing")
    wrong = {ref: bible.get(ref) for ref, want in KNOWN.items() if want not in bible.get(ref, "")}
    if wrong:
        sys.exit(f"Not the RVR1960 (known readings differ): {wrong}")

    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(bible, f, ensure_ascii=False, indent=0, sort_keys=True)
    print(f"Wrote {len(bible)} verses to {os.path.normpath(OUT)} (git-ignored; do not commit)")


if __name__ == "__main__":
    main()
