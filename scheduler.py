"""
scheduler.py — Spúšťa pipeline každý deň o 15:00 (podľa TZ z prostredia).

Pipeline:
  1. search_flow_news.py
  2. ingest_to_dz_news_reworked.py
  3. extract_bulk.py

Po skončení odošle emailové upozornenie. V maili sú len články, ktoré prešli filtrami
(nie zmazané, nie relevance=0, nie AI šport) — viď _EMAIL_FILTER_SQL. Články bez textu
(extrakcia zlyhala) sú v samostatnej sekcii, a to len ak titulok/snippet spomína Slovensko.
"""
from __future__ import annotations

import base64
import logging
import smtplib
import subprocess
import sys
import time
from datetime import date, datetime
from email.message import EmailMessage

from config.config import init_context

s, paths = init_context()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("scheduler")

PIPELINE = [
    "search_flow_news.py",
    "ingest_to_dz_news_reworked.py",
    "extract_bulk.py",
]

RUN_HOUR = 15
RUN_MINUTE = 0


_AR_RE = __import__("re").compile(r"[\u0600-\u06FF]")


def _is_arabic(text: str | None) -> bool:
    return bool(text and _AR_RE.search(text))


def _detect_lang(text: str | None) -> str:
    """Detekuje jazyk z textu. Vracia kód jazyka alebo '?'."""
    if not text or len(text.strip()) < 10:
        return "?"
    if _is_arabic(text):
        return "ar"
    try:
        from langdetect import detect, LangDetectException
        return detect(text)
    except Exception:
        return "?"


def _translate_missing_for_email(engine, rows) -> dict[int, dict]:
    """Preloží title/snippet pre arabské články bez title_fr. Uloží do DB. Vracia {id: {title_fr, snippet_fr}}."""
    api_key = s.deepl_api_key

    from translate import translate_ar_fr
    from sqlalchemy import text

    # zozbieraj článkov ktoré nemajú title_fr a majú arabský title alebo snippet
    pending: list[tuple[int, str | None, str | None]] = []  # (id, title, snippet)
    for r in rows:
        article_id, title, title_fr, snippet, snippet_fr = r[0], r[1], r[2], r[3], r[4]
        if title_fr:
            continue
        if _is_arabic(title) or _is_arabic(snippet):
            pending.append((article_id, title, snippet))

    if not pending:
        return {}

    result: dict[int, dict] = {}
    for article_id, title, snippet in pending:
        texts, keys = [], []
        if title:
            texts.append(title); keys.append("title_fr")
        if snippet:
            texts.append(snippet); keys.append("snippet_fr")
        if not texts:
            continue
        try:
            translated = translate_ar_fr(texts, api_key=api_key)
            updates = dict(zip(keys, translated))
            updates["id"] = article_id
            set_clause = ", ".join(f"{k} = :{k}" for k in keys)
            with engine.begin() as conn:
                conn.execute(text(f"UPDATE articles SET {set_clause} WHERE id = :id"), updates)
            result[article_id] = updates
            logger.info("Email preklad uložený do DB: id=%s", article_id)
        except Exception as e:
            logger.warning("Preklad pre email zlyhal id=%s: %s", article_id, e)

    return result


# Filtre pre email: skryté (bez SK kontextu, šport, duplikát), irelevantné a AI šport nevypisujeme
_EMAIL_FILTER_SQL = """
    a.deleted_at IS NULL
    AND COALESCE(a.relevance, 1) <> 0
    AND COALESCE(a.ai_label, '') <> 'sport'
"""


def _mentions_slovakia(*texts: str | None) -> bool:
    """Spomína text Slovensko? (termíny zo search_terms.json, rovnako ako filter v extrakcii)"""
    terms = [t.lower() for t in s.search_terms.get("slovakia", [])]
    combined = " ".join(t for t in texts if t).lower()
    return any(term in combined for term in terms)


def fetch_reseen_articles(run_ids: list[int], run_start: datetime) -> list[dict]:
    """Vráti články z tohto behu, ktoré existovali pred spustením pipeline."""
    try:
        from config.config import get_db_engine
        from sqlalchemy import text
        engine = get_db_engine()
        placeholders = ", ".join(f":r{i}" for i in range(len(run_ids)))
        params = {f"r{i}": v for i, v in enumerate(run_ids)}
        params["run_start"] = run_start.strftime("%Y-%m-%d %H:%M:%S")
        with engine.begin() as conn:
            rows = conn.execute(text(f"""
                SELECT COALESCE(a.title_fr, a.title), a.title, a.url,
                       COALESCE(a.lang_detected, a.language, '?')
                FROM articles a
                JOIN run_articles ra ON ra.article_id = a.id
                WHERE ra.run_id IN ({placeholders})
                  AND a.first_seen_at < :run_start
                  AND a.content_text IS NOT NULL
                  AND {_EMAIL_FILTER_SQL}
            """), params).fetchall()
        result = []
        for r in rows:
            display_title, orig_title, url, lang = r[0], r[1], r[2], r[3]
            if not lang or lang == "?":
                lang = _detect_lang(orig_title)
            result.append({"title": display_title, "url": url, "lang": lang})
        return result
    except Exception as e:
        logger.warning("Nepodarilo sa načítať znovu videné články: %s", e)
        return []


def fetch_todays_articles(run_ids: list[int], run_start: datetime) -> tuple[list[dict], list[dict]]:
    """Nové články z tohto behu, ktoré prešli filtrami. Arabské bez prekladu preloží a uloží do DB.

    Vracia (overené, neoverené): overené majú text a prešli SK/komerčným/AI filtrom; neoverené sú
    články, ktorých extrakcia zlyhala, ale titulok alebo snippet spomína Slovensko.
    """
    try:
        from config.config import get_db_engine
        from sqlalchemy import text
        engine = get_db_engine()
        placeholders = ", ".join(f":r{i}" for i in range(len(run_ids)))
        params = {f"r{i}": v for i, v in enumerate(run_ids)}
        params["run_start"] = run_start.strftime("%Y-%m-%d %H:%M:%S")
        with engine.begin() as conn:
            rows = conn.execute(text(f"""
                SELECT a.id, a.title, a.title_fr, a.snippet, a.snippet_fr, a.url,
                       a.content_text IS NOT NULL AS has_text, COALESCE(a.lang_detected, a.language, '?') AS lang
                FROM articles a
                JOIN run_articles ra ON ra.article_id = a.id
                WHERE ra.run_id IN ({placeholders})
                  AND a.first_seen_at >= :run_start
                  AND {_EMAIL_FILTER_SQL}
                ORDER BY a.first_seen_at DESC
            """), params).fetchall()

        # neoverené (bez textu) len ak titulok/snippet spomína Slovensko — inak by v maili boli aj
        # športové a nesúvisiace články, ktoré filtrami neprešli len preto, že sa nedali stiahnuť
        rows = [r for r in rows if r[6] or _mentions_slovakia(r[1], r[2], r[3], r[4])]

        translations = _translate_missing_for_email(engine, rows)

        verified, unverified = [], []
        for r in rows:
            article_id, title, title_fr, snippet, snippet_fr, url, has_text, lang_detected = r
            tr = translations.get(article_id, {})
            if not lang_detected or lang_detected == "?":
                lang_detected = _detect_lang(title or snippet)
            (verified if has_text else unverified).append({
                "title":   tr.get("title_fr") or title_fr or title,
                "snippet": tr.get("snippet_fr") or snippet_fr or snippet,
                "url":     url,
                "lang":    lang_detected,
            })
        return verified, unverified
    except Exception as e:
        logger.warning("Nepodarilo sa načítať článkov: %s", e)
        return [], []


def send_notification(
    inserted: int,
    updated: int,
    failed_step: str | None,
    articles: list[dict] | None = None,
    reseen: list[dict] | None = None,
    unverified: list[dict] | None = None,
) -> None:
    if not s.smtp_host or not s.notify_to:
        logger.info("SMTP nie je nakonfigurované — notifikácia preskočená.")
        return

    subject = (
        f"DZ News – pipeline zlyhala ({failed_step})"
        if failed_step
        else f"DZ News – {len(articles or [])} relevantných ({inserted} nových / {updated} aktualizovaných)"
    )

    if failed_step:
        body = f"Pipeline zlyhala v kroku: {failed_step}\nSkontroluj logy v /app/logs/."
    else:
        lines = [
            f"Denná pipeline prebehla úspešne.",
            f"",
            f"Nové články: {inserted}, z toho relevantných: {len(articles or [])}",
            f"Znovu videné (aktualizované): {updated}",
            f"Čas: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        ]
        if articles:
            lines.append("")
            for i, a in enumerate(articles, 1):
                lines.append(f"{i}. [jazyk:{a['lang']}] {a['title'] or '(bez názvu)'}")
                if a["snippet"]:
                    lines.append(f"   {a['snippet']}")
                lines.append(f"   {a['url']}")
        if unverified:
            lines.append("")
            lines.append(f"--- Neoverené – text sa nepodarilo stiahnuť, spomínajú Slovensko ({len(unverified)}) ---")
            for i, a in enumerate(unverified, 1):
                lines.append(f"{i}. [jazyk:{a['lang']}] {a['title'] or '(bez názvu)'}")
                lines.append(f"   {a['url']}")
        if reseen:
            lines.append("")
            lines.append(f"--- Znovu videné články ({len(reseen)}) ---")
            for i, a in enumerate(reseen, 1):
                lines.append(f"{i}. [jazyk:{a['lang']}] {a['title'] or '(bez názvu)'}")
                lines.append(f"   {a['url']}")
        body = "\n".join(lines)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s.smtp_user
    msg["To"] = s.notify_to
    msg.set_content(body)

    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port) as srv:
            srv.ehlo()
            srv.starttls()
            srv.ehlo()
            credentials = base64.b64encode(
                f"\x00{s.smtp_user}\x00{s.smtp_pass}".encode("utf-8")
            ).decode("ascii")
            srv.docmd("AUTH PLAIN", credentials)
            srv.send_message(msg)
        logger.info("Notifikácia odoslaná na %s", s.notify_to)
    except Exception as e:
        logger.error("Odoslanie emailu zlyhalo: %s", e)


_INGEST_RE = __import__("re").compile(r"inserted=(\d+).*?updated=(\d+).*?run_id=(\d+)")


def _parse_ingest_output(output: str) -> tuple[int, int, list[int]]:
    """Parsuje stdout ingesta. Vracia (inserted, updated, run_ids)."""
    inserted, updated, run_ids = 0, 0, []
    for m in _INGEST_RE.finditer(output):
        inserted += int(m.group(1))
        updated += int(m.group(2))
        run_ids.append(int(m.group(3)))
    return inserted, updated, run_ids


def backfill_missing_language(limit: int = 500) -> int:
    """Doplní pole language pre články kde je NULL. Vracia počet aktualizovaných."""
    try:
        from config.config import get_db_engine
        from sqlalchemy import text
        engine = get_db_engine()
        with engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT id, title, snippet
                FROM articles
                WHERE language IS NULL
                  AND deleted_at IS NULL
                ORDER BY id DESC
                LIMIT :limit
            """), {"limit": limit}).fetchall()

        if not rows:
            return 0

        updated = 0
        with engine.begin() as conn:
            for r in rows:
                lang = _detect_lang(r[1] or r[2])
                if lang and lang != "?":
                    conn.execute(text("UPDATE articles SET language = :lang WHERE id = :id"),
                                 {"lang": lang, "id": r[0]})
                    updated += 1

        logger.info("backfill_missing_language: aktualizovaných %s/%s článkov", updated, len(rows))
        return updated
    except Exception as e:
        logger.warning("backfill_missing_language zlyhal: %s", e)
        return 0


def run_pipeline() -> None:
    logger.info("=== Pipeline start ===")
    run_start = datetime.now()
    failed_step = None
    ingest_output = ""

    for script in PIPELINE:
        logger.info("Spúšťam: %s", script)
        result = subprocess.run(
            [sys.executable, script],
            capture_output=(script == "ingest_to_dz_news_reworked.py"),
            text=True,
        )
        if script == "ingest_to_dz_news_reworked.py":
            ingest_output = result.stdout or ""
            sys.stdout.write(ingest_output)
            sys.stderr.write(result.stderr or "")
        if result.returncode != 0:
            logger.error("ZLYHALO: %s (exit %s)", script, result.returncode)
            failed_step = script
            break

    if not failed_step:
        backfill_missing_language()
        inserted, updated, run_ids = _parse_ingest_output(ingest_output)
        articles, unverified = fetch_todays_articles(run_ids, run_start) if (run_ids and inserted > 0) else ([], [])
        reseen = fetch_reseen_articles(run_ids, run_start) if run_ids else []
        send_notification(inserted, updated, failed_step, articles, reseen, unverified)
        logger.info("=== Pipeline hotová — nových: %s, aktualizovaných: %s ===", inserted, updated)
    else:
        send_notification(0, 0, failed_step, [], [])


def main() -> None:
    from version import version_string
    logger.info("Scheduler spustený (%s). Pipeline beží denne o %02d:%02d.", version_string(), RUN_HOUR, RUN_MINUTE)
    last_run: date | None = None

    while True:
        now = datetime.now()
        if now.hour == RUN_HOUR and now.minute >= RUN_MINUTE and now.date() != last_run:
            last_run = now.date()
            run_pipeline()
        time.sleep(60)


if __name__ == "__main__":
    main()