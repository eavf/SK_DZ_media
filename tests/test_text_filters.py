"""
Tests for text_filters.py — komerčné slová ako celé slová, nie podreťazce.
"""
from __future__ import annotations

from text_filters import find_bad_word

WORDS = {"pass", "offre", "عروض", "العروض", "عرض خاص", "forfait"}


class TestFindBadWord:
    def test_whole_word(self):
        assert find_bad_word("Achetez votre pass internet", WORDS) == "pass"

    def test_not_substring(self):
        assert find_bad_word("Le passé récent et le passage", WORDS) is None

    def test_case_insensitive(self):
        assert find_bad_word("Nouveau FORFAIT mobile", WORDS) == "forfait"

    def test_arabic_word(self):
        assert find_bad_word("أقوى عروض الصيف", WORDS) == "عروض"

    def test_arabic_with_article(self):
        assert find_bad_word("أفضل العروض", WORDS) == "العروض"

    def test_arabic_phrase(self):
        assert find_bad_word("عرض خاص على الإنترنت", WORDS) == "عرض خاص"

    def test_arabic_common_word_not_matched(self):
        # "عرض" (predstaviť/prezentácia) samo osebe nie je komerčné
        assert find_bad_word("عرض الرئيس خطة التعاون مع سلوفاكيا", WORDS) is None

    def test_empty(self):
        assert find_bad_word(None, WORDS) is None
        assert find_bad_word("text", set()) is None
