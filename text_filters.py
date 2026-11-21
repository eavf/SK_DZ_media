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


# ── jazyk, titulky z PDF, ukážka okolo zmienky o Slovensku ─────────────────────

_AR_CHAR_RE = re.compile(r"[؀-ۿ]")
_LETTER_RE = re.compile(r"[^\W\d_]")


def detect_language(text: str | None, sample_chars: int = 3000) -> str | None:
    """Jazyk textu ('ar', 'fr', 'en'…) alebo None, ak sa nedá určiť.

    Arabčina podľa podielu arabských písmen (francúzsko-arabské PDF majú obe písma), inak langdetect
    na malých písmenách: titulok VEĽKÝMI PÍSMENAMI langdetect zmätie („L’AFRIQUE VEUT…“ → 'de', 11/2026).
    """
    sample = (text or "")[:sample_chars]
    letters = _LETTER_RE.findall(sample)
    if len(letters) < 10:
        return None
    if sum(1 for c in letters if _AR_CHAR_RE.match(c)) / len(letters) > 0.3:
        return "ar"
    try:
        from langdetect import DetectorFactory, detect
        DetectorFactory.seed = 0            # langdetect je inak náhodný — rovnaký text, iný výsledok
        return detect(sample.lower())
    except Exception:
        return None


def is_garbled_title(title: str | None, min_repeat: int = 15) -> bool:
    """Titulok rozsypaný pri čítaní PDF so stĺpcami (Google News): rovnaký úsek textu sa opakuje.

    „L’Afrique mise sur les compétences ’Afrique mise sur les compétences pour accompa pour…“
    Bežný titulok neopakuje úsek ≥ min_repeat znakov.
    """
    t = " ".join((title or "").split()).lower()
    for i in range(len(t) - 2 * min_repeat + 1):
        if t.find(t[i:i + min_repeat], i + min_repeat) != -1:
            return True
    return False


def slovak_excerpt(text: str | None, pattern: re.Pattern, width: int = 180) -> str | None:
    """Úsek textu okolo prvej zmienky o Slovensku (± width znakov), na jeden riadok."""
    if not text:
        return None
    m = pattern.search(text)
    if not m:
        return None
    start, end = max(m.start() - width, 0), min(m.end() + width, len(text))
    snippet = " ".join(text[start:end].split())
    return ("…" if start else "") + snippet + ("…" if end < len(text) else "")
