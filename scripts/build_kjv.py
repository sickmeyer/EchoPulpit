"""
Rebuild data/kjv.json -- the public-domain KJV text scripture_lookup.py
verifies article quotations against -- from the aruljohn/Bible-kjv dataset
(one JSON file per book).

Refuses to write anything unless the result is complete: all 66 books in
canonical order (matching scripture_lookup.CANONICAL_BOOK_NAMES), 1,189
chapters and 31,102 verses, none empty. A partial dataset would make valid
citations look "unverifiable" and get them stripped from articles.

Usage (from the repo root):
    python scripts/build_kjv.py
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from scripture_lookup import CANONICAL_BOOK_NAMES, KJV_PATH  # noqa: E402

SOURCE = "https://raw.githubusercontent.com/aruljohn/Bible-kjv/master"
EXPECTED_CHAPTERS = 1189
EXPECTED_VERSES = 31102


def fetch_json(url: str):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return json.load(resp)
        except Exception:
            if attempt == 2:
                raise


def main() -> None:
    books = fetch_json(f"{SOURCE}/Books.json")
    if books != CANONICAL_BOOK_NAMES:
        sys.exit("Source book list doesn't match scripture_lookup.CANONICAL_BOOK_NAMES")

    kjv, chapters = {}, 0
    for book in books:
        data = fetch_json(f"{SOURCE}/{book.replace(' ', '')}.json")
        if data.get("book") != book:
            sys.exit(f"{book}: source file names a different book ({data.get('book')!r})")
        for ch in data["chapters"]:
            chapters += 1
            for v in ch["verses"]:
                key = f"{book} {int(ch['chapter'])}:{int(v['verse'])}"
                text = " ".join(v["text"].split())
                if key in kjv or not text:
                    sys.exit(f"{key}: duplicate or empty verse")
                kjv[key] = text

    if chapters != EXPECTED_CHAPTERS or len(kjv) != EXPECTED_VERSES:
        sys.exit(f"Incomplete: {chapters} chapters / {len(kjv)} verses "
                 f"(expected {EXPECTED_CHAPTERS} / {EXPECTED_VERSES})")

    os.makedirs(os.path.dirname(KJV_PATH), exist_ok=True)
    with open(KJV_PATH, "w", encoding="utf-8") as f:
        json.dump(kjv, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {KJV_PATH}: {chapters} chapters, {len(kjv)} verses")


if __name__ == "__main__":
    main()
