"""
normalize_url_scheme.py — Prepočet uložených dedup kľúčov na schému https.

canonicalize_url() od 11/2026 zjednocuje http:// → https://, aby http a https verzia toho istého článku
mali rovnaký url_hash. Staré články majú url_canonical / final_url_canonical (a ich hashe) ešte
s http:// — bez prepočtu by ingest tú istú http URL nespároval a vložil ju znova.

Mení len url_canonical, url_hash, final_url_canonical, final_url_hash. Stĺpce url a final_url
(skutočné adresy na fetch a zobrazenie) ostávajú nezmenené, takže prepočet sa dá kedykoľvek zopakovať.

Kolízia: url_hash je UNIQUE. Ak https verzia už existuje ako iný článok (http/https duplikát),
url_hash sa nemení — https článok už má správny kľúč, takže budúci ingest oboch variantov trafí jeho.
Aktívne kolízie (oba články nezmazané) sa vypíšu zvlášť na ručné vyradenie.

Použitie (CLI):
    python normalize_url_scheme.py           # dry-run: zobrazí čo by sa zmenilo
    python normalize_url_scheme.py --apply   # skutočne prepočíta
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

from sqlalchemy import text

from config.config import get_db_engine, init_cli
from extract_articles_reworked import sha256_hex

_HTTP = "http://"


def to_https(canon: str | None) -> str | None:
    """http://x → https://x; iné hodnoty (None, https://…) vráti None = netreba meniť."""
    if canon and canon.startswith(_HTTP):
        return "https://" + canon[len(_HTTP):]
    return None


@dataclass
class Plan:
    url_updates: list[dict] = field(default_factory=list)      # {id, canon, hash}
    final_updates: list[dict] = field(default_factory=list)    # {id, canon, hash}
    collisions: list[tuple[int, int, bool]] = field(default_factory=list)  # (id, https_id, oba_aktívne)


def build_plan(rows, existing_by_hash: dict[str, tuple[int, bool]]) -> Plan:
    """rows: {id, url_canonical, final_url_canonical, active}; existing_by_hash: url_hash → (id, active)."""
    plan = Plan()
    for r in rows:
        new_url = to_https(r["url_canonical"])
        if new_url:
            h = sha256_hex(new_url)
            other = existing_by_hash.get(h)
            if other and other[0] != r["id"]:
                plan.collisions.append((r["id"], other[0], bool(r["active"]) and other[1]))
            else:
                plan.url_updates.append({"id": r["id"], "canon": new_url, "hash": h})

        new_final = to_https(r["final_url_canonical"])
        if new_final:
            plan.final_updates.append({"id": r["id"], "canon": new_final, "hash": sha256_hex(new_final)})
    return plan


def main() -> int:
    _, _, logger = init_cli("normalize_url_scheme")
    p = argparse.ArgumentParser(description="Prepočíta url_canonical/url_hash (a final_*) z http:// na https://.")
    p.add_argument("--apply", action="store_true", help="Skutočne zapíše zmeny (inak dry-run)")
    args = p.parse_args()
    prefix = "" if args.apply else "[DRY-RUN] "

    engine = get_db_engine()
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT id, url_canonical, final_url_canonical, deleted_at IS NULL AS active
            FROM articles
            WHERE url_canonical LIKE 'http://%' OR final_url_canonical LIKE 'http://%'
            ORDER BY id
        """)).mappings().fetchall()

        new_hashes = [sha256_hex(c) for c in (to_https(r["url_canonical"]) for r in rows) if c]
        existing: dict[str, tuple[int, bool]] = {}
        for i in range(0, len(new_hashes), 500):
            chunk = new_hashes[i:i + 500]
            params = {f"h{j}": h for j, h in enumerate(chunk)}
            for e in conn.execute(text(
                f"SELECT id, url_hash, deleted_at IS NULL AS active FROM articles "
                f"WHERE url_hash IN ({', '.join(':' + k for k in params)})"
            ), params).mappings():
                existing[e["url_hash"]] = (e["id"], bool(e["active"]))

        plan = build_plan(rows, existing)

        for c_id, other_id, both_active in plan.collisions:
            if both_active:
                logger.warning("%sid=%s: https verzia už existuje ako #%s, oba aktívne — vyraď jeden ručne",
                               prefix, c_id, other_id)
            else:
                logger.info("%sid=%s: https verzia už existuje ako #%s (jeden zmazaný) — url_hash ponechaný",
                            prefix, c_id, other_id)

        if args.apply:
            if plan.url_updates:
                conn.execute(text(
                    "UPDATE articles SET url_canonical = :canon, url_hash = :hash WHERE id = :id"
                ), plan.url_updates)
            if plan.final_updates:
                conn.execute(text(
                    "UPDATE articles SET final_url_canonical = :canon, final_url_hash = :hash WHERE id = :id"
                ), plan.final_updates)

    logger.info("%sHotovo: url prepočítaných=%s, final_url prepočítaných=%s, kolízií=%s (z toho oba aktívne=%s)",
                prefix, len(plan.url_updates), len(plan.final_updates), len(plan.collisions),
                sum(1 for c in plan.collisions if c[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
