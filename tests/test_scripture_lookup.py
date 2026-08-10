import pytest

from scripture_lookup import (
    normalize_reference,
    lookup_verse_text,
    canonical_reference_string,
    verify_and_correct_scripture,
    kjv_available,
)

FIXTURE_KJV = {
    "Ephesians 6:13": "Wherefore take unto you the whole armour of God, that ye may be able to withstand in the evil day, and having done all, to stand.",
    "Ephesians 6:14": "Stand therefore, having your loins girt about with truth, and having on the breastplate of righteousness;",
    "Ephesians 6:17": "And take the helmet of salvation, and the sword of the Spirit, which is the word of God:",
    "1 Samuel 17:29": "And David said, What have I now done? Is there not a cause?",
    "Psalms 23:1": "The LORD is my shepherd; I shall not want.",
    "John 3:16": "For God so loved the world, that he gave his only begotten Son, that whosoever believeth in him should not perish, but have everlasting life.",
}


class TestNormalizeReference:
    def test_single_verse(self):
        assert normalize_reference("Ephesians 6:13") == ("Ephesians", 6, 13, 13)

    def test_verse_range(self):
        assert normalize_reference("Ephesians 6:13-17") == ("Ephesians", 6, 13, 17)

    def test_numbered_book(self):
        assert normalize_reference("1 Samuel 17:29") == ("1 Samuel", 17, 29, 29)

    def test_psalm_singular_aliased_to_psalms(self):
        assert normalize_reference("Psalm 23:1") == ("Psalms", 23, 1, 1)

    def test_unparseable_returns_none(self):
        assert normalize_reference("somewhere in Ephesians") is None

    def test_empty_returns_none(self):
        assert normalize_reference("") is None

    def test_backwards_range_returns_none(self):
        assert normalize_reference("Ephesians 6:17-13") is None


class TestLookupVerseText:
    def test_single_verse_found(self):
        assert lookup_verse_text("Ephesians 6:13", FIXTURE_KJV) == FIXTURE_KJV["Ephesians 6:13"]

    def test_verse_range_joins_verses(self):
        result = lookup_verse_text("Ephesians 6:13-14", FIXTURE_KJV)
        assert result == FIXTURE_KJV["Ephesians 6:13"] + " " + FIXTURE_KJV["Ephesians 6:14"]

    def test_partial_range_missing_verse_returns_none(self):
        # 6:15-16 aren't in the fixture, so 6:14-17 can't be fully verified.
        assert lookup_verse_text("Ephesians 6:14-17", FIXTURE_KJV) is None

    def test_unknown_verse_returns_none(self):
        assert lookup_verse_text("Ephesians 6:99", FIXTURE_KJV) is None

    def test_unparseable_reference_returns_none(self):
        assert lookup_verse_text("not a reference", FIXTURE_KJV) is None


class TestCanonicalReferenceString:
    def test_single_verse(self):
        assert canonical_reference_string("ephesians 6:13") == "Ephesians 6:13"

    def test_range(self):
        assert canonical_reference_string("Ephesians 6:13-17") == "Ephesians 6:13-17"

    def test_alias_normalized(self):
        assert canonical_reference_string("Psalm 23:1") == "Psalms 23:1"

    def test_unparseable_returns_none(self):
        assert canonical_reference_string("garbage") is None


class TestVerifyAndCorrectScripture:
    def test_valid_reference_gets_authoritative_text(self):
        body = (
            "Here is the point.\n\n"
            '> "something the model misremembered" (Ephesians 6:13)\n\n'
            "And here is more prose."
        )
        corrected, refs, flags = verify_and_correct_scripture(body, FIXTURE_KJV)
        assert FIXTURE_KJV["Ephesians 6:13"] in corrected
        assert "something the model misremembered" not in corrected
        assert refs == ["Ephesians 6:13"]
        assert flags == []

    def test_unresolvable_reference_is_removed_and_flagged(self):
        body = (
            "Here is the point.\n\n"
            '> "a quote that does not exist" (3 Corinthians 4:5)\n\n'
            "And here is more prose."
        )
        corrected, refs, flags = verify_and_correct_scripture(body, FIXTURE_KJV)
        assert "3 Corinthians" not in corrected
        assert "a quote that does not exist" not in corrected
        assert refs == []
        assert len(flags) == 1
        assert "3 Corinthians 4:5" in flags[0]

    def test_multiple_citations_mixed_valid_and_invalid(self):
        body = (
            '> "x" (John 3:16)\n\n'
            "Some prose in between.\n\n"
            '> "y" (Nowhere 1:1)\n\n'
            '> "z" (Psalm 23:1)\n'
        )
        corrected, refs, flags = verify_and_correct_scripture(body, FIXTURE_KJV)
        assert refs == ["John 3:16", "Psalms 23:1"]
        assert len(flags) == 1
        assert FIXTURE_KJV["John 3:16"] in corrected
        assert FIXTURE_KJV["Psalms 23:1"] in corrected

    def test_no_citations_is_a_noop(self):
        body = "Just plain prose with no scripture blockquotes at all."
        corrected, refs, flags = verify_and_correct_scripture(body, FIXTURE_KJV)
        assert corrected == body
        assert refs == []
        assert flags == []

    @pytest.mark.skipif(not kjv_available(), reason="bundled KJV dataset (data/kjv.json) not present")
    def test_default_kjv_arg_loads_bundled_file(self):
        # Sanity check the real bundled dataset is wired up and has at least
        # this one very well-known verse -- doesn't assert full coverage,
        # that's the build script's own job. Skipped rather than failing
        # when the dataset isn't present -- sermon_pipeline.py's own
        # kjv_available() gate is what matters for pipeline correctness,
        # covered separately.
        corrected, refs, flags = verify_and_correct_scripture(
            '> "x" (John 3:16)'
        )
        assert refs == ["John 3:16"]
