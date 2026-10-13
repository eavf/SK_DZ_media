"""
text_filters.py — Hľadanie "komerčných" slov (title_bad_words z config/url_cleanup.json) v texte.

Hľadajú sa celé slová/frázy, nie podreťazce — inak "pass" zachytí "passé", "passage"
a bežné arabské slová v iných tvaroch označia aj správy (napr. o dodávkach plynu).
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable


@lru_cache(maxsize=16)
def _pattern(words: frozenset[str]) -> re.Pattern | None:
    cleaned = sorted({w.strip().lower() for w in words if w and w.strip()}, key=len, reverse=True)
    if not cleaned:
        return None
    # (?<!\w) / (?!\w) namiesto \b — funguje aj pre frázy a arabské písmo (\w je Unicode)
    return re.compile(r"(?<!\w)(" + "|".join(map(re.escape, cleaned)) + r")(?!\w)", re.IGNORECASE)


def find_bad_word(text: str | None, words: Iterable[str] | None) -> str | None:
    """Vráti prvé nájdené slovo zo `words` (ako celé slovo) alebo None."""
    if not text or not words:
        return None
    pattern = _pattern(frozenset(words))
    if pattern is None:
        return None
    m = pattern.search(text)
    return m.group(1).lower() if m else None
