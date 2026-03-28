"""
translate.py — Zdieľaná DeepL prekladová funkcia.

Použitie:
    from translate import translate_ar_fr
    results = translate_ar_fr(api_key, ["text1", "text2"])
"""
from __future__ import annotations

import logging
import requests

logger = logging.getLogger("translate")


def _deepl_url(api_key: str) -> str:
    return (
        "https://api-free.deepl.com/v2/translate"
        if api_key.endswith(":fx")
        else "https://api.deepl.com/v2/translate"
    )


def translate_ar_fr(api_key: str, texts: list[str]) -> list[str]:
    """
    Preloží zoznam textov z AR do FR cez DeepL.
    Vracia zoznam preložených textov v rovnakom poradí.
    Vyhodí výnimku pri chybe siete alebo API.
    """
    if not texts:
        return []
    resp = requests.post(
        _deepl_url(api_key),
        headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
        json={"text": texts, "source_lang": "AR", "target_lang": "FR"},
        timeout=60,
    )
    resp.raise_for_status()
    return [t["text"] for t in resp.json()["translations"]]