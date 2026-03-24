import os
import re
import sys
import io
import csv
from datetime import datetime, timedelta
import subprocess
from pathlib import Path
from urllib.parse import urlparse
import logging
from logging.handlers import RotatingFileHandler

from flask import Flask, render_template, request, redirect, url_for, send_file, abort, flash
from dotenv import load_dotenv
from sqlalchemy import text, bindparam
from docx import Document


from config.config import get_db_engine, require, init_context
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()

settings, paths = init_context()
logger = logging.getLogger("app")

def configure_logging(app, settings):
    log_path = settings.paths.log_dir / settings.log_file

    handler = RotatingFileHandler(
        log_path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )

    level = getattr(logging, settings.log_level.upper(), logging.ERROR)
    handler.setLevel(level)

    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s in %(pathname)s:%(lineno)d"
    ))

    # vyhneš sa duplikáciám handlerov (hlavne pri reloaderi)
    app.logger.handlers.clear()

    app.logger.setLevel(level)
    app.logger.addHandler(handler)

    # (voliteľné) zachytí aj root logy z knižníc
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)


app = Flask(__name__)
app.config["SECRET_KEY"] = require(settings.flask_secret_key, "kduhfhg liughaliug aeliug heliugh seligrus gehlriuaegl iearugfh lskdjhfgsldoughsgôoriughoserghero")
# za reverse proxy (Synology) – aby Flask vedel o pôvodnom HTTPS/hoste
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
configure_logging(app, settings)

SLOVAK_TERMS = [
    "slovaquie", "slovaque", "slovakia", "slovak",
    "république slovaque",
    "سلوفاكيا", "سلوفاكي", "الجمهورية السلوفاكية"
]
TERM_RE = re.compile(r"(" + "|".join(re.escape(t) for t in SLOVAK_TERMS) + r")", re.IGNORECASE)


BUNDLE_GLOB = "news_bundle_*.json"   # presne ako vytvára search_flow_news.py
PYTHON_BIN = sys.executable


def latest_bundle_path() -> str | None:
    p = max(paths.bundle_dir.glob(BUNDLE_GLOB), default=None, key=lambda x: x.stat().st_mtime)
    return str(p) if p else None


def run_script(args: list[str], env: dict | None = None) -> tuple[int, str]:
    """
    Spustí skript a vráti (returncode, combined_output).
    """
    cp = subprocess.run(
        args,
        capture_output=True,
        text=True,
        env=env,
    )
    out = (cp.stdout or "") + ("\n" + cp.stderr if cp.stderr else "")
    return cp.returncode, out


def split_sentences(text_: str):
    # Simple splitter good enough for FR/EN/AR news text
    return re.split(r'(?<=[\.\!\?؟])\s+', text_)


def extract_context_sentences(text_: str, max_sentences: int = 3):
    if not text_:
        return []
    t = text_.replace("\n", " ")
    sentences = [s.strip() for s in split_sentences(t) if s.strip()]
    hits = []
    for s in sentences:
        if TERM_RE.search(s):
            hits.append(s)
        if len(hits) >= max_sentences:
            break
    return hits


def highlight_terms_html(s: str) -> str:
    # Very simple highlighting (safe enough if we escape later in template)
    return TERM_RE.sub(r"<mark>\1</mark>", s)


def build_filters(days: int, only_ok: bool, only_slovak: bool, relevance: str, include_deleted: bool, include_avoided: bool):
    cutoff = datetime.now() - timedelta(days=days)

    where = ["a.last_seen_at >= :cutoff"]
    params = {"cutoff": cutoff}

    if not include_deleted:
        where.append("a.deleted_at IS NULL")

    if not include_avoided:
        where.append("s.is_avoided = 0")

    if only_ok:
        where.append("a.extraction_ok = 1")

    if relevance in ("1", "0"):
        where.append("a.relevance = :rel")
        params["rel"] = int(relevance)
    elif relevance == "null":
        where.append("a.relevance IS NULL")

    # Slovak context filter:
    # If extracted text exists -> search there; else fall back to title/snippet.
    if only_slovak:
        # Use LOWER + LIKE for portability
        like_terms = []
        for i, term in enumerate(SLOVAK_TERMS):
            key = f"t{i}"
            params[key] = f"%{term.lower()}%"
            like_terms.append(f"LOWER(COALESCE(a.content_text, a.title, a.snippet, '')) LIKE :{key}")
        where.append("(" + " OR ".join(like_terms) + ")")

    return " AND ".join(where), params


def browse_q_from_request():
    """
    Vytiahne len browse filtre (days/ok/sk/rel/del/av) z requestu.
    Funguje pre GET aj POST, lebo request.values = args + form.
    """
    allowed = {"days", "ok", "sk", "rel", "del", "av"}
    q = request.values.to_dict(flat=True)
    return {k: v for k, v in q.items() if k in allowed}


def get_selected_article_ids():
    """
    Robustne vytiahne vybrané article IDs z formu.
    Podporuje viacero názvov inputov: ids, article_id, article_ids, id, selected...
    """
    candidate_keys = ("ids", "article_id", "article_ids", "id", "selected", "selected_ids")

    raw = []
    for k in candidate_keys:
        raw.extend(request.form.getlist(k))

    # niekedy príde ako jeden CSV string: "1,2,3"
    if len(raw) == 1 and isinstance(raw[0], str) and "," in raw[0]:
        raw = [x.strip() for x in raw[0].split(",") if x.strip()]

    ids = []
    for x in raw:
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue

    # deduplikácia, zachovať poradie
    seen = set()
    out = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def normalize_domain(s: str) -> str:
    s = (s or "").strip().lower()
    if not s:
        return ""
    # ak niekto dá URL, zober netloc
    if "://" in s:
        try:
            s = urlparse(s).netloc.lower()
        except Exception:
            pass
    # odstráň path, query (ak zadal bez schémy)
    s = s.split("/")[0].split("?")[0].split("#")[0]
    if s.startswith("www."):
        s = s[4:]
    return s


@app.get("/")
def dashboard():
    engine = get_db_engine()
    with engine.begin() as conn:
        # posledná aktualizácia = posledný last_seen_at v tabuľke
        last_update = conn.execute(text("SELECT MAX(last_seen_at) FROM articles")).scalar()

        # zdroje (voliteľne – ak máš tabuľku sources)
        sources_count = conn.execute(text("SELECT COUNT(*) FROM sources")).scalar()

        # nájdené za 24h (podľa last_seen_at)
        found_24h = conn.execute(text("""
            SELECT COUNT(*) FROM articles
            WHERE last_seen_at >= (NOW() - INTERVAL 1 DAY)
              AND deleted_at IS NULL
        """)).scalar()

        # čaká na spracovanie (príklad: extraction_ok = 0 a nie je deleted a nie je z avoided source)
        pending = conn.execute(text("""
            SELECT COUNT(*)
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE a.deleted_at IS NULL
              AND s.is_avoided = 0
              AND (a.extraction_ok = 0 OR a.extraction_ok IS NULL)
        """)).scalar()

        # chyby fetch/extract (len orientačne)
        errors = conn.execute(text("""
            SELECT COUNT(*)
            FROM articles
            WHERE deleted_at IS NULL
              AND fetch_error IS NOT NULL
              AND TRIM(fetch_error) <> ''
        """)).scalar()

    # formátovanie času na text (jednoducho, nech to v šablóne nie je None)
    last_update_str = ""
    if last_update:
        try:
            last_update_str = last_update.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            last_update_str = str(last_update)

    return render_template(
        "index.html",              # NOVÁ dashboard šablóna
        last_update=last_update_str or "—",
        sources_count=sources_count or 0,
        found_24h=found_24h or 0,
        pending_count=pending or 0,
        errors_count=errors or 0,
        port=settings.flask_port,
    )


@app.get("/browse")
def browse():
    days = int(request.args.get("days", 7))
    only_ok = request.args.get("ok", "1") == "1"
    only_slovak = request.args.get("sk", "0") == "1"
    relevance = request.args.get("rel", "all")  # all | 1 | 0 | null
    include_deleted = request.args.get("del", "0") == "1"
    include_avoided = request.args.get("av", "0") == "1"

    where_sql, params = build_filters(days, only_ok, only_slovak, relevance, include_deleted, include_avoided)

    sql = f"""
        SELECT
            a.id,
            a.title,
            s.domain,
            COALESCE(DATE_FORMAT(a.published_at_real, '%Y-%m-%d %H:%i:%s'), a.published_at_text) AS published,
            COALESCE(a.final_url, a.url) AS url,
            COALESCE(a.extraction_ok, 0) = 1,
            a.fetch_error,
            LEFT(a.content_text, 1200) AS preview,
            a.relevance,
            a.relevance_note,
            a.deleted_at,
            s.id AS source_id,
            s.is_avoided,
            s.notes AS source_notes
        FROM articles a
        JOIN sources s ON s.id = a.source_id
        WHERE {where_sql}
        ORDER BY a.last_seen_at DESC
        LIMIT 300
    """

    engine = get_db_engine()
    with engine.begin() as conn:
        rows = conn.execute(text(sql), params).fetchall()

    # Precompute context sentences + highlighted snippets
    enriched = []
    for r in rows:
        ctx = extract_context_sentences(r.preview or "", max_sentences=3)
        ctx_hl = [highlight_terms_html(c) for c in ctx]
        enriched.append((r, ctx_hl))

    # Debug filtrovania : Vytlačí do konzoly podmienky
    #print("WHERE:", where_sql)
    #print("PARAMS:", params)

    return render_template(
        "browse.html",
        rows=enriched,
        days=days,
        only_ok=only_ok,
        only_slovak=only_slovak,
        relevance=relevance,
        include_deleted=include_deleted,
        include_avoided=include_avoided,
        total=len(rows),
        port=settings.flask_port,
    )


@app.post("/bulk_delete")
def bulk_delete():
    mode = request.form.get("mode", "soft")  # soft|hard
    ids_int = get_selected_article_ids()

    if not ids_int:
        logger.warning("bulk_delete called with no articles selected")
        return redirect(url_for("browse", **browse_q_from_request()))

    engine = get_db_engine()
    with engine.begin() as conn:
        if mode == "hard":
            conn.execute(text("""
                DELETE FROM run_articles
                WHERE article_id IN :ids
            """), {"ids": tuple(ids_int)})
            conn.execute(text("""
                DELETE FROM articles
                WHERE id IN :ids
            """), {"ids": tuple(ids_int)})
        else:
            conn.execute(text("""
                UPDATE articles
                SET deleted_at = NOW()
                WHERE id IN :ids
            """), {"ids": tuple(ids_int)})

    logger.info("bulk_delete (%s): %d articles %s", mode, len(ids_int), ids_int)
    return redirect(url_for("browse", **browse_q_from_request()))


@app.post("/undelete/<int:article_id>")
def undelete(article_id: int):
    """Undo soft delete: vráti článok späť (deleted_at=NULL)."""
    engine = get_db_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE articles
            SET deleted_at = NULL
            WHERE id = :id
        """), {"id": article_id})

    logger.info("Article restored from soft delete: id=%s", article_id)
    return redirect(url_for("browse", **browse_q_from_request()))


@app.post("/bulk_exclude_domains")
def bulk_exclude_domains():
    note = (request.form.get("note") or "").strip()[:255]
    ids_int = get_selected_article_ids()

    if not ids_int:
        return redirect(url_for("browse", **browse_q_from_request()))

    engine = get_db_engine()
    with engine.begin() as conn:
        # 1) zisti source_id pre vybrané články (EXPANDING!)
        q = text("""
            SELECT DISTINCT source_id
            FROM articles
            WHERE id IN :ids
        """).bindparams(bindparam("ids", expanding=True))

        source_rows = conn.execute(q, {"ids": ids_int}).fetchall()
        source_ids = [row[0] for row in source_rows if row[0] is not None]

        if not source_ids:
            logger.warning("bulk_exclude_domains: no source_ids found for articles %s", ids_int)
            return redirect(url_for("browse", **request.args))

        # 2) umlčať domény (EXPANDING!)
        u = text("""
            UPDATE sources
            SET is_avoided = 1
            WHERE id IN :sids
        """).bindparams(bindparam("sids", expanding=True))

        conn.execute(u, {"sids": source_ids})

        # 3) voliteľne uložiť dôvod do notes
        if note:
            # nastav iba tam, kde je notes prázdne; nech neprepisujeme ručne napísané dôvody
            u2 = text("""
                UPDATE sources
                SET notes = :note
                WHERE id IN :sids
                  AND (notes IS NULL OR TRIM(notes) = '')
            """).bindparams(bindparam("sids", expanding=True))

            conn.execute(u2, {"note": note, "sids": source_ids})

    logger.info("bulk_exclude_domains: %d sources muted from %d articles", len(source_ids), len(ids_int))
    return redirect(url_for("browse", **browse_q_from_request()))




@app.get("/stats")
def stats():
    """
    Štatistika umlčaných domén:
    - koľko článkov by sa defaultne skrylo (t.j. ušetrilo)
    - koľko z nich je soft-deleted
    - posledný výskyt
    + filter domain priamo v /stats
    + top 10 umlčaných podľa "saved"
    + (optional) články pre vybranú doménu
    """
    domain = (request.args.get("domain") or "").strip().lower()
    days = int(request.args.get("days", 30))
    cutoff = datetime.now() - timedelta(days=days)

    engine = get_db_engine()

    # 1) TOP 10 (vždy, bez domain filtra)
    with engine.begin() as conn:
        top10 = conn.execute(text("""
            SELECT
                s.id,
                s.domain,
                COALESCE(s.notes, '') AS notes,
                COUNT(a.id) AS total_articles,
                SUM(CASE WHEN a.deleted_at IS NULL THEN 1 ELSE 0 END) AS visible_if_unmuted,
                SUM(CASE WHEN a.deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS soft_deleted,
                MAX(a.last_seen_at) AS last_seen
            FROM sources s
            JOIN articles a ON a.source_id = s.id
            WHERE s.is_avoided = 1
              AND a.last_seen_at >= :cutoff
            GROUP BY s.id, s.domain, s.notes
            ORDER BY visible_if_unmuted DESC, total_articles DESC
            LIMIT 10
        """), {"cutoff": cutoff}).mappings().all()

    # 1b) Global sum saved (všetky umlčané domény v okne)
    with engine.begin() as conn:
        total_saved_all = conn.execute(text("""
            SELECT
                COALESCE(SUM(CASE WHEN a.deleted_at IS NULL THEN 1 ELSE 0 END), 0) AS total_saved
            FROM sources s
            JOIN articles a ON a.source_id = s.id
            WHERE s.is_avoided = 1
              AND a.last_seen_at >= :cutoff
        """), {"cutoff": cutoff}).scalar_one()

    # 2) Tabuľka umlčaných domén (s voliteľným filtrom domain)
    where_extra = ""
    params = {"cutoff": cutoff}
    if domain:
        where_extra = " AND LOWER(s.domain) = :domain "
        params["domain"] = domain

    with engine.begin() as conn:
        rows = conn.execute(text(f"""
            SELECT
                s.id,
                s.domain,
                COALESCE(s.notes, '') AS notes,
                COUNT(a.id) AS total_articles,
                SUM(CASE WHEN a.deleted_at IS NULL THEN 1 ELSE 0 END) AS visible_if_unmuted,
                SUM(CASE WHEN a.deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS soft_deleted,
                MAX(a.last_seen_at) AS last_seen
            FROM sources s
            JOIN articles a ON a.source_id = s.id
            WHERE s.is_avoided = 1
              AND a.last_seen_at >= :cutoff
              {where_extra}
            GROUP BY s.id, s.domain, s.notes
            ORDER BY visible_if_unmuted DESC, total_articles DESC
        """), params).mappings().all()

    # “ušetrené” = články z umlčaných domén, ktoré by sa inak zobrazili v UI defaultne
    enriched = []
    total_saved = 0
    for r in rows:
        saved = int(r["visible_if_unmuted"] or 0)
        total_saved += saved
        enriched.append({
            "id": r["id"],
            "domain": r["domain"],
            "notes": r["notes"],
            "total_articles": int(r["total_articles"] or 0),
            "saved": saved,
            "soft_deleted": int(r["soft_deleted"] or 0),
            "last_seen": r["last_seen"],
        })

    # 3) Optional: články pre vybranú doménu (aby klik “domain” malo okamžitý efekt priamo v /stats)
    articles = []
    if domain:
        with engine.begin() as conn:
            articles = conn.execute(text("""
                SELECT
                    a.id,
                    a.title,
                    COALESCE(DATE_FORMAT(a.published_at_real, '%%Y-%%m-%%d %%H:%%i'), a.published_at_text) AS published,
                    COALESCE(a.final_url, a.url) AS url,
                    a.extraction_ok,
                    a.fetch_error,
                    a.deleted_at,
                    a.last_seen_at
                FROM articles a
                JOIN sources s ON s.id = a.source_id
                WHERE LOWER(s.domain) = :domain
                  AND a.last_seen_at >= :cutoff
                ORDER BY a.last_seen_at DESC
                LIMIT 200
            """), {"domain": domain, "cutoff": cutoff}).mappings().all()

    return render_template(
        "stats.html",
        rows=enriched,
        top10=top10,
        articles=articles,
        days=days,
        domain=domain,
        total_saved=total_saved,
        total_saved_all=int(total_saved_all),
    )


@app.post("/label/<int:article_id>")
def label(article_id: int):
    rel = request.form.get("relevance")  # "1" or "0" or "null"
    note = (request.form.get("note") or "").strip()[:255]

    if rel not in ("1", "0", "null"):
        abort(400)

    engine = get_db_engine()
    with engine.begin() as conn:
        if rel == "null":
            conn.execute(text("""
                UPDATE articles
                SET relevance=NULL, relevance_note=:note
                WHERE id=:id
            """), {"id": article_id, "note": note})
        else:
            conn.execute(text("""
                UPDATE articles
                SET relevance=:rel, relevance_note=:note
                WHERE id=:id
            """), {"id": article_id, "rel": int(rel), "note": note})

    logger.info("Article labeled: id=%s, relevance=%s, note=%r", article_id, rel, note)
    return redirect(url_for("browse", **browse_q_from_request()))


@app.post("/delete/<int:article_id>")
def delete(article_id: int):
    mode = request.form.get("mode", "soft")  # soft | hard

    engine = get_db_engine()
    with engine.begin() as conn:
        if mode == "hard":
            # hard delete must remove links first
            conn.execute(text("DELETE FROM run_articles WHERE article_id=:id"), {"id": article_id})
            conn.execute(text("DELETE FROM articles WHERE id=:id"), {"id": article_id})
        else:
            conn.execute(text("UPDATE articles SET deleted_at=NOW() WHERE id=:id"), {"id": article_id})

    logger.info("Article deleted (%s): id=%s", mode, article_id)
    return redirect(url_for("browse", **browse_q_from_request()))


@app.get("/export/csv")
def export_csv():
    days = int(request.args.get("days", 7))
    only_ok = request.args.get("ok", "1") == "1"
    only_slovak = request.args.get("sk", "0") == "1"
    relevance = request.args.get("rel", "all")
    include_deleted = request.args.get("del", "0") == "1"
    include_avoided = request.args.get("av", "0") == "1"
    where_sql, params = build_filters(days, only_ok, only_slovak, relevance, include_deleted, include_avoided)

    sql = f"""
        SELECT
            a.id,
            s.domain AS domain,
            COALESCE(DATE_FORMAT(a.published_at_real, '%Y-%m-%d %H:%i:%s'), a.published_at_text) AS published,
            COALESCE(a.final_url, a.url) AS url,
            a.title,
            a.snippet,
            a.relevance,
            a.relevance_note
        FROM articles a
        JOIN sources s ON s.id = a.source_id
        WHERE {where_sql}
        ORDER BY a.last_seen_at DESC
    """

    engine = get_db_engine()
    with engine.begin() as conn:
        rows = conn.execute(text(sql), params).fetchall()

    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(["id", "domain", "published", "url", "title", "snippet", "relevance", "note"])

    if not rows:
        writer.writerow(["", "", "", "", "Žiadne nové správy", "", "", ""])
    else:
        for r in rows:
            writer.writerow([
                r.id, r.domain, r.published or "", r.url,
                r.title or "", r.snippet or "",
                "" if r.relevance is None else int(r.relevance),
                r.relevance_note or ""
            ])

    logger.info("CSV export: %d rows, %dd filter", len(rows), days)
    mem = io.BytesIO(out.getvalue().encode("utf-8-sig"))  # BOM helps Excel
    filename = f"dz_news_{days}d.csv"
    return send_file(mem, as_attachment=True, download_name=filename, mimetype="text/csv")


@app.get("/export/word")
def export_word():
    days = int(request.args.get("days", 7))
    only_ok = request.args.get("ok", "1") == "1"
    only_slovak = request.args.get("sk", "0") == "1"
    relevance = request.args.get("rel", "all")
    include_deleted = request.args.get("del", "0") == "1"
    include_avoided = request.args.get("av", "0") == "1"

    where_sql, params = build_filters(days, only_ok, only_slovak, relevance, include_deleted, include_avoided)

    sql = f"""
        SELECT
            a.id,
            s.domain AS domain,
            COALESCE(DATE_FORMAT(a.published_at_real, '%Y-%m-%d %H:%i:%s'), a.published_at_text) AS published,
            COALESCE(a.final_url, a.url) AS url,
            a.title,
            a.snippet,
            a.content_text,
            a.relevance,
            a.relevance_note
        FROM articles a
        JOIN sources s ON s.id = a.source_id
        WHERE {where_sql}
        ORDER BY a.last_seen_at DESC
        LIMIT 200
    """

    engine = get_db_engine()
    with engine.begin() as conn:
        rows = conn.execute(text(sql), params).fetchall()

    doc = Document()
    doc.add_heading(f"DZ News – monitoring (posledných {days} dní)", level=1)

    if not rows:
        doc.add_paragraph("Žiadne nové správy.")
    else:
        for r in rows:
            title = r.title or "— bez titulku —"
            doc.add_heading(title, level=2)
            meta = f"Zdroj: {r.domain} | Dátum: {r.published or ''}"
            if r.relevance is not None:
                meta += f" | Relevancia: {int(r.relevance)}"
            doc.add_paragraph(meta)
            doc.add_paragraph(r.url)

            if r.relevance_note:
                doc.add_paragraph(f"Poznámka: {r.relevance_note}")

            if r.snippet:
                doc.add_paragraph(r.snippet)

            # Slovak context sentences (strict, no inference)
            ctx = extract_context_sentences(r.content_text or "", max_sentences=3)
            if ctx:
                doc.add_paragraph("Výskyt SR v texte (explicitne):")
                for s in ctx:
                    doc.add_paragraph(f"- {s}")

    logger.info("Word export: %d rows, %dd filter", len(rows), days)
    mem = io.BytesIO()
    doc.save(mem)
    mem.seek(0)

    filename = f"dz_news_{days}d.docx"
    return send_file(mem, as_attachment=True, download_name=filename,
                     mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.get("/article/<int:article_id>")
def article_detail(article_id: int):
    engine = get_db_engine()
    with engine.begin() as conn:
        r = conn.execute(text("""
            SELECT
                a.id,
                a.source_id,
                s.domain,
                s.is_preferred,
                s.is_avoided,
                s.notes AS source_notes,

                a.url,
                a.final_url,
                a.final_url_canonical,
                a.final_url_hash,

                a.url_canonical,
                a.url_hash,

                a.title,
                a.published_at_text,
                a.published_at_real,
                a.published_conf,

                a.snippet,
                a.language,
                a.lang_detected,

                a.extraction_ok,
                a.source_label,

                a.first_seen_at,
                a.last_seen_at,
                a.fetched_at,
                a.http_status,
                a.fetch_error,

                a.content_text,
                a.content_hash,

                a.ingestion_engine,
                a.ingestion_query_id,
                a.ingestion_rank,

                a.relevance,
                a.relevance_note,
                a.deleted_at
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE a.id = :id
        """), {"id": article_id}).mappings().fetchone()

    if r is None:
        abort(404)

    # Preferuj final_url (ak existuje), inak url
    online_url = r["final_url"] or r["url"]

    published_display = ""
    if r["published_at_real"]:
        published_display = r["published_at_real"].strftime("%Y-%m-%d %H:%M:%S")
    else:
        published_display = r["published_at_text"] or ""

    # Kontextové vety (ak už máš helpery; ak nie, nechaj prázdne)
    ctx_plain = []
    ctx_hl = []

    try:
        ctx_plain = extract_context_sentences(
            r.get("content_text") or "",
            max_sentences=6
        )
        ctx_hl = [highlight_terms_html(s) for s in ctx_plain]
    except Exception:
        ctx_plain = []
        ctx_hl = []

    return render_template(
        "article.html",
        r=r,
        online_url=online_url,
        ctx_plain=ctx_plain,
        ctx_hl=ctx_hl,
        published_display=published_display)


@app.post("/article/<int:article_id>/label")
def article_label(article_id: int):
    relevance_raw = request.form.get("relevance", "null")
    note = (request.form.get("note") or "").strip()[:255]

    if relevance_raw == "null":
        relevance = None
    elif relevance_raw in ("0", "1"):
        relevance = int(relevance_raw)
    else:
        abort(400)

    engine = get_db_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE articles
            SET relevance = :relevance,
                relevance_note = :note
            WHERE id = :id
        """), {"relevance": relevance, "note": note if note else None, "id": article_id})

    logger.info("Article labeled: id=%s, relevance=%s, note=%r", article_id, relevance_raw, note)
    return redirect(url_for("article_detail", article_id=article_id))


@app.post("/article/<int:article_id>/exclude_domain")
def article_exclude_domain(article_id: int):
    note = (request.form.get("note") or "").strip()[:255]  # voliteľné: dôvod

    engine = get_db_engine()
    with engine.begin() as conn:
        r = conn.execute(text("""
            SELECT source_id
            FROM articles
            WHERE id = :id
        """), {"id": article_id}).scalar()

        if not r:
            abort(404)

        conn.execute(text("""
            UPDATE sources
            SET is_avoided = 1
            WHERE id = :sid
        """), {"sid": r})

        if note:
            # nastav dôvod len ak prázdny
            conn.execute(text("""
                UPDATE sources
                SET notes = :note
                WHERE id = :sid AND (notes IS NULL OR TRIM(notes) = '')
            """), {"note": note, "sid": r})

    logger.info("Source muted from article: article_id=%s, source_id=%s", article_id, r)
    return redirect(url_for("article_detail", article_id=article_id))


@app.post("/article/<int:article_id>/unexclude_domain")
def article_unexclude_domain(article_id: int):
    engine = get_db_engine()
    with engine.begin() as conn:
        sid = conn.execute(text("""
            SELECT source_id FROM articles WHERE id=:id
        """), {"id": article_id}).scalar()
        if not sid:
            abort(404)

        conn.execute(text("""
            UPDATE sources SET is_avoided = 0 WHERE id = :sid
        """), {"sid": sid})

    logger.info("Source unmuted from article: article_id=%s, source_id=%s", article_id, sid)
    return redirect(url_for("article_detail", article_id=article_id))


@app.post("/article/<int:article_id>/fetch")
def fetch_extract_article(article_id: int):
    code, out = run_script([PYTHON_BIN, "refetch_article.py", "--article-id", str(article_id)])
    if code == 0:
        logger.info("Article fetch OK: id=%s", article_id)
        flash("Fetch & extract OK.", "success")
    else:
        logger.error("Article fetch failed: id=%s\n%s", article_id, out)
        flash("Fetch & extract FAIL — pozri log.", "error")

    return redirect(url_for("article_detail", article_id=article_id))


@app.post("/bulk/restore")
def bulk_restore():
    ids_int = get_selected_article_ids()
    if not ids_int:
        logger.warning("bulk_restore called with no articles selected")
        return redirect(url_for("browse", **browse_q_from_request()))

    engine = get_db_engine()
    with engine.begin() as conn:
        q = text("UPDATE articles SET deleted_at = NULL WHERE id IN :ids")
        conn.execute(q, {"ids": tuple(ids_int)})

    logger.info("bulk_restore: %d articles restored", len(ids_int))
    return redirect(url_for("browse", **browse_q_from_request()))


@app.get("/search")
def search_page():
    lb = latest_bundle_path()
    return render_template("search.html", latest_bundle=lb)

@app.post("/search/run")
def search_run():
    code, out = run_script([PYTHON_BIN, "search_flow_news.py"])
    if code == 0:
        lb = latest_bundle_path()
        logger.info("search_run OK: exit_code=%s, bundle=%s", code, lb)
        flash(f"OK: vyhľadávanie dokončené. Latest bundle: {lb}", "ok")
    else:
        logger.error("search_run FAILED: exit_code=%s\n%s", code, out)
        flash("CHYBA: vyhľadávanie zlyhalo (pozri log na /search).", "bad")
    return render_template("search.html", latest_bundle=latest_bundle_path(), last_log=out, last_rc=code)


@app.get("/process")
def process_page():
    lb = latest_bundle_path()
    return render_template("process.html", latest_bundle=lb)

@app.post("/process/ingest_latest")
def process_ingest_latest():
    lb = latest_bundle_path()
    if not lb:
        logger.warning("process_ingest_latest: no bundle file found")
        flash("Nenašiel som žiadny news_bundle_*.json.", "bad")
        return redirect(url_for("process_page"))

    env = os.environ.copy()
    env["BUNDLE_PATH"] = lb

    logger.info("process_ingest_latest started: bundle=%s", lb)
    code, out = run_script([PYTHON_BIN, "ingest_to_dz_news_reworked.py"], env=env)
    if code == 0:
        logger.info("process_ingest_latest OK: exit_code=%s, bundle=%s", code, lb)
        flash(f"OK: ingest hotový ({lb})", "ok")
    else:
        logger.error("process_ingest_latest FAILED: exit_code=%s\n%s", code, out)
        flash("CHYBA: ingest zlyhal (pozri log na /process).", "bad")

    return render_template("process.html", latest_bundle=lb, last_log=out, last_rc=code)


@app.get("/errors")
def errors_page():
    days = int(request.args.get("days", 30))
    engine = get_db_engine()
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT a.id, a.title, s.domain,
                   COALESCE(DATE_FORMAT(a.published_at_real, '%%Y-%%m-%%d %%H:%%i'), a.published_at_text) AS published,
                   a.fetch_error, a.http_status, a.last_seen_at
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE a.deleted_at IS NULL
              AND a.last_seen_at >= (NOW() - INTERVAL :days DAY)
              AND (a.fetch_error IS NOT NULL AND TRIM(a.fetch_error) <> '')
            ORDER BY a.last_seen_at DESC
            LIMIT 300
        """), {"days": days}).mappings().all()

    return render_template("errors.html", rows=rows, days=days)



@app.get("/sources")
def sources_page():
    q = (request.args.get("q") or "").strip().lower()
    days = int(request.args.get("days", 30))
    only_avoided = (request.args.get("only_avoided", "0") == "1")
    cutoff = datetime.now() - timedelta(days=days)

    where = ["1=1"]
    params = {"cutoff": cutoff}

    if only_avoided:
        where.append("s.is_avoided = 1")

    if q:
        where.append("LOWER(s.domain) LIKE :q")
        params["q"] = f"%{q}%"

    sql = f"""
        SELECT
            s.id,
            s.domain,
            COALESCE(s.notes, '') AS notes,
            COALESCE(s.is_avoided, 0) AS is_avoided,

            COUNT(a.id) AS total_articles,
            SUM(CASE WHEN a.deleted_at IS NULL THEN 1 ELSE 0 END) AS visible_if_unmuted,
            SUM(CASE WHEN a.deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS soft_deleted,
            MAX(a.last_seen_at) AS last_seen
        FROM sources s
        LEFT JOIN articles a
               ON a.source_id = s.id
              AND a.last_seen_at >= :cutoff
        WHERE {" AND ".join(where)}
        GROUP BY s.id, s.domain, s.notes, s.is_avoided
        ORDER BY s.is_avoided DESC, visible_if_unmuted DESC, total_articles DESC, s.domain ASC
    """

    engine = get_db_engine()
    with engine.begin() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    enriched = []
    for r in rows:
        saved = int(r["visible_if_unmuted"] or 0) if int(r["is_avoided"] or 0) == 1 else 0
        # Pozn.: "ušetrené" má význam najmä pri mute doménach; pri nemute dáme 0 (aby to nemiatlo).
        enriched.append({
            "id": r["id"],
            "domain": r["domain"],
            "notes": r["notes"],
            "is_avoided": int(r["is_avoided"] or 0),
            "total_articles": int(r["total_articles"] or 0),
            "soft_deleted": int(r["soft_deleted"] or 0),
            "saved": saved,
            "last_seen": r["last_seen"],
        })

    return render_template(
        "sources.html",
        rows=enriched,
        q=q,
        days=days,
        only_avoided=only_avoided,
    )


@app.post("/source/create")
def source_create():
    domain_in = request.form.get("domain", "")
    note = (request.form.get("note") or "").strip()

    domain = normalize_domain(domain_in)
    if not domain:
        flash("Chýba doména.", "warn")
        return redirect("/sources")

    engine = get_db_engine()
    with engine.begin() as conn:
        # upsert-like správanie: ak existuje, len update note (ak zadané)
        existing = conn.execute(
            text("SELECT id FROM sources WHERE LOWER(domain) = :d"),
            {"d": domain}
        ).scalar()

        if existing:
            if note:
                conn.execute(
                    text("UPDATE sources SET notes = :n WHERE id = :id"),
                    {"n": note, "id": existing}
                )
            logger.warning("source_create: domain already exists: %s", domain)
            flash(f"Doména už existuje: {domain}", "warn")
        else:
            conn.execute(
                text("""
                    INSERT INTO sources (domain, notes, is_avoided)
                    VALUES (:d, :n, 0)
                """),
                {"d": domain, "n": note}
            )
            logger.info("source_create: new source added: %s", domain)
            flash(f"Pridané: {domain}", "ok")

    return redirect("/sources")


@app.post("/source/note/<int:source_id>")
def source_note(source_id: int):
    note = (request.form.get("note") or "").strip()

    engine = get_db_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE sources SET notes = :n WHERE id = :id"),
            {"n": note if note else None, "id": source_id}
        )

    logger.info("Source note updated: id=%s, note=%r", source_id, note)
    flash("Poznámka uložená.", "ok")
    # vráť sa tam, kde bol user (ak posielal zo /sources alebo /stats)
    return redirect(request.referrer or "/sources")


@app.post("/source/avoid/<int:source_id>")
def source_avoid(source_id: int):
    engine = get_db_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE sources SET is_avoided = 1 WHERE id = :id"),
            {"id": source_id}
        )
    logger.info("Source muted: id=%s", source_id)
    flash("Doména umlčaná (mute).", "ok")
    return redirect(request.referrer or "/sources")


@app.post("/source/unavoid/<int:source_id>")
def source_unavoid(source_id: int):
    engine = get_db_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE sources SET is_avoided = 0 WHERE id = :id"),
            {"id": source_id}
        )
    logger.info("Source unmuted: id=%s", source_id)
    flash("Doména od-umlčaná (unmute).", "ok")
    return redirect(request.referrer or "/sources")


if __name__ == "__main__":
    port = settings.flask_port
    app.run(host="127.0.0.1", port=port, debug=True)
