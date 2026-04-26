"""
translate.py — Prekladová funkcia s podporou lokálneho, DeepL a Azure backendu.

Backend sa volí cez env var TRANSLATOR_BACKEND:
  - "local"  (default): lokálny translation service (TRANSLATOR_URL)
  - "deepl":  DeepL API (vyžaduje DEEPL_API_KEY)
  - "azure":  Azure Cognitive Translator (vyžaduje AZURE_TRANSLATOR_KEY + AZURE_TRANSLATOR_REGION)

Použitie:
    from translate import translate_ar_fr
    results = translate_ar_fr(["text1", "text2"])
"""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

import requests

logger = logging.getLogger("translate")

TRANSLATOR_URL = os.getenv("TRANSLATOR_URL", "https://translate.eavf1621.synology.me").rstrip("/")
TRANSLATOR_BACKEND = os.getenv("TRANSLATOR_BACKEND", "local").strip().lower()

_BACKEND_FILE = Path(__file__).parent / "config" / "translator_backend"
_VALID_BACKENDS = ("local", "deepl", "azure")


def get_translator_backend() -> str:
    """Vráti aktívny backend — config/translator_backend má prednosť pred .env."""
    try:
        val = _BACKEND_FILE.read_text(encoding="utf-8").strip().lower()
        if val in _VALID_BACKENDS:
            return val
    except Exception:
        pass
    return TRANSLATOR_BACKEND


def get_translator_backend_with_source() -> tuple[str, str]:
    """Vráti (backend, source) kde source je 'ui' alebo 'env'."""
    try:
        val = _BACKEND_FILE.read_text(encoding="utf-8").strip().lower()
        if val in _VALID_BACKENDS:
            return val, "ui"
    except Exception:
        pass
    return TRANSLATOR_BACKEND, "env"


def set_translator_backend(backend: str) -> None:
    """Uloží backend do config/translator_backend (prepisuje .env za behu)."""
    if backend not in _VALID_BACKENDS:
        raise ValueError(f"Neplatný backend: {backend}")
    _BACKEND_FILE.write_text(backend, encoding="utf-8")

_AZURE_ENDPOINT = "https://api.cognitive.microsofttranslator.com/translate"


# ── Lokálny translator ─────────────────────────────────────────────────────────

def _local_translate(texts: list[str], source_lang: str, target_lang: str) -> list[str]:
    results = []
    for t in texts:
        resp = requests.post(
            f"{TRANSLATOR_URL}/translate",
            json={"text": t, "source_lang": source_lang, "target_lang": target_lang},
            timeout=120,
        )
        resp.raise_for_status()
        results.append(resp.json()["translated_text"])
    return results


# ── DeepL ─────────────────────────────────────────────────────────────────────

def _deepl_url(api_key: str) -> str:
    return (
        "https://api-free.deepl.com/v2/translate"
        if api_key.endswith(":fx")
        else "https://api.deepl.com/v2/translate"
    )


def _deepl_translate(api_key: str, texts: list[str], source_lang: str, target_lang: str) -> list[str]:
    resp = requests.post(
        _deepl_url(api_key),
        headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
        json={"text": texts, "source_lang": source_lang.upper(), "target_lang": target_lang.upper()},
        timeout=60,
    )
    resp.raise_for_status()
    return [t["text"] for t in resp.json()["translations"]]


def get_deepl_usage(api_key: str) -> dict | None:
    """Vráti {'used': int, 'limit': int} alebo None pri chybe."""
    if not api_key:
        return None
    base = _deepl_url(api_key).replace("/translate", "/usage")
    try:
        resp = requests.get(
            base,
            headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return {"used": data["character_count"], "limit": data["character_limit"]}
    except Exception:
        return None


# ── Azure Cognitive Translator ─────────────────────────────────────────────────

def _azure_translate(texts: list[str], source_lang: str, target_lang: str) -> list[str]:
    azure_key = os.getenv("AZURE_TRANSLATOR_KEY", "").strip()
    azure_region = os.getenv("AZURE_TRANSLATOR_REGION", "westeurope").strip()
    if not azure_key:
        raise ValueError("AZURE_TRANSLATOR_KEY nie je nastavený v .env")
    resp = requests.post(
        _AZURE_ENDPOINT,
        params={"api-version": "3.0", "from": source_lang, "to": target_lang},
        headers={
            "Ocp-Apim-Subscription-Key": azure_key,
            "Ocp-Apim-Subscription-Region": azure_region,
            "Content-Type": "application/json",
            "X-ClientTraceId": str(uuid.uuid4()),
        },
        json=[{"Text": t} for t in texts],
        timeout=60,
    )
    resp.raise_for_status()
    return [item["translations"][0]["text"] for item in resp.json()]


# ── Azure usage (nie je dostupné cez API) ─────────────────────────────────────

def get_azure_usage() -> dict | None:
    """Azure Cognitive Translator nemá usage API — vráti None."""
    return None


# ── Local health check ─────────────────────────────────────────────────────────

def get_local_status() -> dict | None:
    """Vráti {'status': 'ok', 'supported_pairs': [...]} alebo None pri chybe."""
    try:
        resp = requests.get(f"{TRANSLATOR_URL}/", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return {
            "status": "ok",
            "supported_pairs": data.get("supported_pairs", []),
            "url": TRANSLATOR_URL,
        }
    except Exception:
        return None


# ── Verejné API ────────────────────────────────────────────────────────────────

def translate_ar_fr(texts: list[str], api_key: str = "") -> list[str]:
    """
    Preloží zoznam AR textov do FR.

    Backend sa volí cez TRANSLATOR_BACKEND (env var):
      - "local" (default): lokálny service na TRANSLATOR_URL
      - "deepl": DeepL API (api_key je povinný)
      - "azure": Azure Cognitive Translator (AZURE_TRANSLATOR_KEY + AZURE_TRANSLATOR_REGION)
    """
    if not texts:
        return []

    backend = get_translator_backend()

    if backend == "deepl":
        if not api_key:
            raise ValueError("TRANSLATOR_BACKEND=deepl ale DEEPL_API_KEY nie je nastavený")
        logger.debug("Prekladám cez DeepL: %d textov", len(texts))
        return _deepl_translate(api_key, texts, "ar", "fr")

    if backend == "azure":
        logger.debug("Prekladám cez Azure: %d textov", len(texts))
        return _azure_translate(texts, "ar", "fr")

    logger.debug("Prekladám cez lokálny service (%s): %d textov", TRANSLATOR_URL, len(texts))
    return _local_translate(texts, "ar", "fr")
