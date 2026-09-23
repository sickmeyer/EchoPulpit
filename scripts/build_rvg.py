"""
Build data/rvg.json -- the Reina Valera Gómez text Spanish articles are
verified against (scripture_lookup.verify_and_correct_scripture with
lang="es") -- from eBible.org's verse-per-line edition (sparvg).

Keys match data/kjv.json ("John 3:16", English canonical book names) so the
same lookup code serves both; scripture_lookup maps Spanish book names to
them. Square brackets marking translator-supplied words (printed in italics
in the Bible) are removed, keeping the words. Refuses to write anything
unless all 66 books / 31,102 verses are present.

Copyright © 2004, 2010, 2023 Dr. Humberto Gómez Caballero (Iglesia Bautista
Libertad de Matamoros). Reproduction for free distribution is permitted;
reproduction for profit is prohibited. See data/RVG-NOTICE.txt.

Usage (repo root):  python scripts/build_rvg.py
"""
import io
import json
import os
import re
import sys
import urllib.request
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from scripture_lookup import CANONICAL_BOOK_NAMES  # noqa: E402

SOURCE = "https://ebible.org/Scriptures/sparvg_vpl.zip"
# USFM edition, only for the Psalm superscriptions ("Salmo de David"), which
# the verse-per-line edition merges into verse 1 (the KJV data keeps them out).
USFM_SOURCE = "https://ebible.org/Scriptures/sparvg_usfm.zip"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "rvg.json")
EXPECTED_VERSES = 31102
LINE = re.compile(r"^([1-3A-Z]{3}) (\d+):(\d+) (.*)$")


def _fetch_zip(url: str, ctx) -> zipfile.ZipFile:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (EchoPulpit build_rvg.py)"})
    with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
        return zipfile.ZipFile(io.BytesIO(resp.read()))


def _usfm_plain(text: str) -> str:
    text = re.sub(r"\\w\s+([^|\\]*)\|[^\\]*\\w\*", r"\1", text)  # \w word|strong="..."\w* -> word
    text = re.sub(r"\\\+?[a-z]+\d*\*?", " ", text)               # any other \marker or \marker*
    return " ".join(text.replace("[", "").replace("]", "").split())


def psalm_headings(ctx) -> dict:
    """{psalm number: superscription text} from the USFM edition."""
    z = _fetch_zip(USFM_SOURCE, ctx)
    name = next(n for n in z.namelist() if "PSA" in n.upper())
    headings, chapter = {}, None
    for line in z.read(name).decode("utf-8-sig").splitlines():
        m = re.match(r"\\c\s+(\d+)", line)
        if m:
            chapter = int(m.group(1))
        elif line.startswith("\\d ") and chapter:
            headings[chapter] = _usfm_plain(line[3:])
    return headings


def main() -> None:
    try:
        import certifi, ssl  # noqa: E401
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = None
    text = _fetch_zip(SOURCE, ctx).read("sparvg_vpl.txt").decode("utf-8-sig")
    headings = psalm_headings(ctx)

    # eBible's VPL uses its own 3-letter book codes (JOH, MAR, SOL, 1JO...),
    # listed in canonical order -- so map by order of first appearance.
    codes = []
    for line in text.splitlines():
        m = LINE.match(line.strip())
        if m and m.group(1) not in codes:
            codes.append(m.group(1))
    if len(codes) != 66:
        sys.exit(f"Expected 66 books, found {len(codes)}: {codes}")
    book_of = dict(zip(codes, CANONICAL_BOOK_NAMES))

    bible, stripped = {}, 0
    for line in text.splitlines():
        m = LINE.match(line.strip())
        if not m:
            continue
        code, ch, vs, words = m.groups()
        words = " ".join(words.replace("[", "").replace("]", "").split())
        key = f"{book_of[code]} {int(ch)}:{int(vs)}"
        heading = headings.get(int(ch)) if book_of[code] == "Psalms" and int(vs) == 1 else None
        if heading and words.startswith(heading):
            words = words[len(heading):].strip()
            stripped += 1
        if not words or key in bible:
            sys.exit(f"{key}: empty or duplicate verse")
        bible[key] = words
    if len(bible) != EXPECTED_VERSES:
        sys.exit(f"Incomplete: {len(bible)} verses (expected {EXPECTED_VERSES})")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(bible, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {OUT}: {len(bible)} verses ({stripped} of {len(headings)} Psalm headings removed from verse 1)")


if __name__ == "__main__":
    main()
