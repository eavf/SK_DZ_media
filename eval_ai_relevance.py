#!/usr/bin/env python3
"""Meranie: vie AI posúdiť relevanciu článku k vzťahom Slovensko–Alžírsko?

Porovná odpoveď LLM (NVIDIA API, OpenAI-kompatibilné) s ručným označením z CSV exportu
(/export/csv, stĺpce id;domain;published;url;title;snippet;fetch_error;relevance;note).
DB sa nepoužíva a nič sa nezapisuje — výsledok je len report + CSV s odpoveďami.

Do merania idú len ručne označené články: relevance 0/1 a poznámka nezačína „auto:“
(automatické vyradenia — komerčné, šport AI, PDF, duplikáty — nie sú tvoje rozhodnutie).
CSV export neobsahuje celý text, AI teda rozhoduje z titulku, snippetu a domény
(rovnako ako pri prvom pohľade v zozname).

Odpovede sa ukladajú priebežne (--out), opakované spustenie pokračuje od posledného článku.

Použitie:
    python eval_ai_relevance.py export.csv                       # všetky ručne označené
    python eval_ai_relevance.py export.csv --limit 100           # vyvážená vzorka 50 + 50
    python eval_ai_relevance.py export.csv --model meta/llama-3.3-70b-instruct
    python eval_ai_relevance.py export.csv --report-only         # len report z uložených odpovedí

Premenné z .env: NVIDIA_API_KEY, NVIDIA_BASE_URL (default https://integrate.api.nvidia.com/v1).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

DEFAULT_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"  # nemotron-3-super-120b NVIDIA zrušila (410)
DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
REQUEST_TIMEOUT = 90
MAX_SNIPPET_CHARS = 1500

SYSTEM_PROMPT = (
    "You help a Slovak diplomat monitor Algerian media. Decide whether a news article is RELEVANT "
    "for monitoring Slovakia and Slovakia–Algeria relations.\n"
    "Answer ONLY with JSON: {\"relevant\": true|false, \"confidence\": 0-100, \"reason\": \"<max 15 words>\"}.\n"
    "relevant=true when Slovakia is a real subject of the article: Slovak–Algerian relations "
    "(visits, meetings, agreements, trade, investments, cooperation, embassy activities, consular matters), "
    "Slovak officials (president, prime minister Fico, foreign minister Blanár, ambassador) acting or speaking, "
    "Slovak companies, products or citizens in Algeria, or Slovak domestic or foreign policy covered on its own.\n"
    "relevant=false when Slovakia (Slovaquie, سلوفاكيا) is only mentioned in passing: in a list of countries, "
    "a sports result, an advertisement, a generic EU or UN article, or when the matching word refers to "
    "something else (a person's name, a different country). Also false for commercial content and job offers.\n"
    "Decide from the main subject, not from single words. The article may be in French, Arabic or English. "
    "Only the title, a short snippet and the source domain are available."
)

_JSON_RE = re.compile(r"\{.*?\}", re.S)


# ── vstup ─────────────────────────────────────────────────────────────────────

def load_labeled(path: Path) -> list[dict]:
    """Ručne označené riadky exportu (relevance 0/1, poznámka bez „auto:“)."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    out = []
    for r in rows:
        rel = (r.get("relevance") or "").strip()
        note = (r.get("note") or "").strip()
        if rel not in ("0", "1") or note.lower().startswith("auto:"):
            continue
        if not (r.get("title") or r.get("snippet")):
            continue
        out.append({**r, "relevance": int(rel)})
    return out


def balanced_sample(rows: list[dict], limit: int | None, seed: int = 42) -> list[dict]:
    """Vzorka s rovnakým počtom relevantných a nerelevantných (koľko sa dá), stabilná pre seed."""
    if not limit or limit >= len(rows):
        return rows
    rnd = random.Random(seed)
    pos = [r for r in rows if r["relevance"] == 1]
    neg = [r for r in rows if r["relevance"] == 0]
    rnd.shuffle(pos)
    rnd.shuffle(neg)
    half = limit // 2
    take_pos = min(len(pos), max(half, limit - len(neg)))
    take_neg = min(len(neg), limit - take_pos)
    return pos[:take_pos] + neg[:take_neg]


# ── AI ────────────────────────────────────────────────────────────────────────

def build_user_msg(row: dict) -> str:
    return (f"Source domain: {row.get('domain') or ''}\n"
            f"Title: {row.get('title') or ''}\n\n"
            f"Snippet:\n{(row.get('snippet') or '')[:MAX_SNIPPET_CHARS]}")


def parse_answer(answer: str) -> tuple[bool, int | None, str]:
    """JSON odpoveď modelu → (relevant, confidence, reason). Pri neplatnej odpovedi ValueError."""
    m = _JSON_RE.search(answer or "")
    if not m:
        raise ValueError(f"AI nevrátilo JSON: {answer[:200]!r}")
    data = json.loads(m.group(0))
    if not isinstance(data.get("relevant"), bool):
        raise ValueError(f"neplatná hodnota 'relevant': {answer[:200]!r}")
    conf = data.get("confidence")
    conf = int(conf) if isinstance(conf, (int, float)) else None
    return data["relevant"], conf, str(data.get("reason") or "").strip()


def ask_ai(row: dict, model: str, api_key: str, base_url: str, thinking: bool) -> tuple[bool, int | None, str]:
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": build_user_msg(row)}],
        "temperature": 0,
        # aj s vypnutým premýšľaním niektoré modely (Mistral Large 4) premýšľajú — 150 tokenov nestačí
        "max_tokens": 4000 if thinking else 1500,
    }
    if not thinking:
        # reasoning modely (Nemotron, Qwen3…) by inak minuli tokeny na premýšľanie
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    for attempt in range(5):
        resp = requests.post(f"{base_url}/chat/completions",
                             headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                             json=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = 30 * (attempt + 1)
            print(f"   HTTP {resp.status_code}, čakám {wait} s…", file=sys.stderr)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return parse_answer(resp.json()["choices"][0]["message"]["content"] or "")
    raise RuntimeError("API opakovane vracia 429/5xx")


# ── výsledky ──────────────────────────────────────────────────────────────────

OUT_FIELDS = ["id", "domain", "title", "manual", "ai", "confidence", "reason", "model"]


def load_done(out: Path, model: str) -> dict[str, dict]:
    if not out.exists():
        return {}
    with out.open(encoding="utf-8", newline="") as f:
        return {r["id"]: r for r in csv.DictReader(f) if r.get("model") == model}


def report(results: list[dict]) -> str:
    """Matica zhody + presnosť/úplnosť pre „relevantné“ + zoznam nezhôd."""
    tp = sum(1 for r in results if r["manual"] == 1 and r["ai"] == 1)
    tn = sum(1 for r in results if r["manual"] == 0 and r["ai"] == 0)
    fp = sum(1 for r in results if r["manual"] == 0 and r["ai"] == 1)
    fn = sum(1 for r in results if r["manual"] == 1 and r["ai"] == 0)
    n = tp + tn + fp + fn
    if not n:
        return "Žiadne vyhodnotené články."
    pct = lambda a, b: f"{100 * a / b:.0f} %" if b else "—"
    lines = [
        f"Vyhodnotené: {n} (ručne relevantné {tp + fn}, nerelevantné {tn + fp})",
        f"Zhoda celkom: {pct(tp + tn, n)}",
        "",
        "                 AI: relevantné   AI: nerelevantné",
        f"ty: relevantné      {tp:6d}            {fn:6d}   ← {fn} relevantných by AI prehliadla",
        f"ty: nerelevantné    {fp:6d}            {tn:6d}   ← {fp} zbytočne navrhnutých",
        "",
        f"Z relevantných AI zachytila (recall):           {pct(tp, tp + fn)}",
        f"Keď AI povie „relevantné“, má pravdu (precision): {pct(tp, tp + fp)}",
        f"Keď AI povie „nerelevantné“, má pravdu:          {pct(tn, tn + fn)}",
    ]
    misses = [r for r in results if r["manual"] != r["ai"]]
    if misses:
        lines += ["", "Nezhody (ty → AI):"]
        for r in sorted(misses, key=lambda r: (r["manual"], -(r["confidence"] or 0))):
            lines.append(f"  #{r['id']} [{r['domain']}] {r['manual']}→{r['ai']} ({r['confidence']}) "
                         f"{(r['title'] or '')[:90]}\n      AI: {r['reason']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", type=Path, help="CSV export z /export/csv")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--limit", type=int, help="vyvážená vzorka N článkov (polovica relevantných)")
    ap.add_argument("--delay", type=float, default=2.0, help="pauza medzi volaniami (s), free limit ~40/min")
    ap.add_argument("--thinking", action="store_true", help="nechať modelu premýšľanie (pomalšie)")
    ap.add_argument("--out", type=Path, help="CSV s odpoveďami (default <csv>.ai_<model>.csv)")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    load_dotenv()
    api_key = os.getenv("NVIDIA_API_KEY", "").strip()
    base_url = (os.getenv("NVIDIA_BASE_URL", "").strip() or DEFAULT_BASE_URL).rstrip("/")
    if not api_key and not args.report_only:
        print("Chýba NVIDIA_API_KEY v .env", file=sys.stderr)
        return 2

    rows = balanced_sample(load_labeled(args.csv), args.limit)
    out = args.out or args.csv.with_name(f"{args.csv.stem}.ai_{re.sub(r'[^A-Za-z0-9.-]+', '_', args.model)}.csv")
    done = load_done(out, args.model)
    print(f"Ručne označených v CSV: {len(rows)} (vo vzorke), už vyhodnotených: "
          f"{sum(1 for r in rows if r['id'] in done)} → {out}", file=sys.stderr)

    if not args.report_only:
        new_file = not out.exists()
        with out.open("a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
            if new_file:
                w.writeheader()
            todo = [r for r in rows if r["id"] not in done]
            for i, r in enumerate(todo, 1):
                try:
                    rel, conf, reason = ask_ai(r, args.model, api_key, base_url, args.thinking)
                except Exception as e:  # jeden zlý článok nezastaví meranie
                    print(f"   #{r['id']}: chyba {e}", file=sys.stderr)
                    continue
                rec = {"id": r["id"], "domain": r.get("domain", ""), "title": r.get("title", ""),
                       "manual": r["relevance"], "ai": int(rel), "confidence": conf, "reason": reason,
                       "model": args.model}
                w.writerow(rec)
                f.flush()
                done[r["id"]] = rec
                mark = "✓" if int(rel) == r["relevance"] else "✗"
                print(f"{i}/{len(todo)} {mark} #{r['id']} {r['relevance']}→{int(rel)} {reason}", file=sys.stderr)
                time.sleep(args.delay)

    results = []
    for r in rows:
        d = done.get(r["id"])
        if d:
            results.append({**d, "manual": int(d["manual"]), "ai": int(d["ai"]),
                            "confidence": int(d["confidence"]) if str(d.get("confidence") or "").isdigit() else None})
    print(f"\nModel: {args.model}\n" + report(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
