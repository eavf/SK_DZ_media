"""
scheduler.py — Spúšťa pipeline každý deň o 15:00 (podľa TZ z prostredia).

Pipeline:
  1. search_flow_news.py
  2. ingest_to_dz_news_reworked.py
  3. extract_bulk.py

Po skončení odošle emailové upozornenie s počtom nových článkov.
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


def count_todays_articles() -> int:
    """Vráti počet článkov pridaných dnes."""
    try:
        from config.config import get_db_engine
        from sqlalchemy import text
        engine = get_db_engine()
        with engine.begin() as conn:
            row = conn.execute(text(
                "SELECT COUNT(*) FROM articles WHERE DATE(first_seen_at) = CURDATE()"
            )).fetchone()
        return row[0] if row else 0
    except Exception as e:
        logger.warning("Nepodarilo sa načítať počet článkov: %s", e)
        return -1


def send_notification(new_count: int, failed_step: str | None) -> None:
    if not s.smtp_host or not s.notify_to:
        logger.info("SMTP nie je nakonfigurované — notifikácia preskočená.")
        return

    subject = (
        f"DZ News – pipeline zlyhala ({failed_step})"
        if failed_step
        else f"DZ News – pipeline hotová ({new_count} nových článkov)"
    )

    if failed_step:
        body = f"Pipeline zlyhala v kroku: {failed_step}\nSkontroluj logy v /app/logs/."
    elif new_count < 0:
        body = "Pipeline prebehla, ale nepodarilo sa zistiť počet nových článkov."
    else:
        body = (
            f"Denná pipeline prebehla úspešne.\n\n"
            f"Nové články dnes: {new_count}\n"
            f"Čas: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )

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


def run_pipeline() -> None:
    logger.info("=== Pipeline start ===")
    failed_step = None

    for script in PIPELINE:
        logger.info("Spúšťam: %s", script)
        result = subprocess.run([sys.executable, script])
        if result.returncode != 0:
            logger.error("ZLYHALO: %s (exit %s)", script, result.returncode)
            failed_step = script
            break

    new_count = count_todays_articles() if not failed_step else -1
    send_notification(new_count, failed_step)

    if not failed_step:
        logger.info("=== Pipeline hotová — nových článkov dnes: %s ===", new_count)


def main() -> None:
    logger.info("Scheduler spustený. Pipeline beží denne o %02d:%02d.", RUN_HOUR, RUN_MINUTE)
    last_run: date | None = None

    while True:
        now = datetime.now()
        if now.hour == RUN_HOUR and now.minute >= RUN_MINUTE and now.date() != last_run:
            last_run = now.date()
            run_pipeline()
        time.sleep(60)


if __name__ == "__main__":
    main()