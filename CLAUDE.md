# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

SK_DZ_media is a Flask web application that monitors Algerian news coverage of Slovakia-Algeria relations. It fetches news via SerpAPI (Google News), extracts article content, stores it in MySQL, and provides a dashboard for reviewing and labeling articles.

## Commands

### Development

```bash
# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Run Flask dev server (uses .env for config)
python app.py

# Run via Docker
docker-compose up --build
```

### Tests

```bash
# Run all tests
pytest

# Run a single test file
pytest tests/test_config.py

# Run a single test
pytest tests/test_search_flow.py::TestDomainExtraction::test_extract_domain
```

### Data Pipeline (CLI scripts)

```bash
# Step 1: Search for news articles and save to bundle/
python search_flow_news.py

# Step 2: Extract article content from bundle and ingest into DB
python ingest_to_dz_news_reworked.py

# Step 3: Bulk-extract articles still missing content_text
python extract_bulk.py [--limit N] [--dry-run]
```

### Other scripts

```bash
python search_mfa_gov.py [--when 30d]          # Scrape MFA Algeria press releases (no SerpAPI)
python refetch_article.py --article-id <id>    # Re-fetch and re-extract a single article
python fix_serp_dates.py [--apply]             # Fix relative SerpAPI dates (dry-run by default)
python classify_existing.py [--apply] [--limit N]  # Backfill AI sport classification (dry-run by default)
python source_relink.py [--apply]              # Relink google.com/goto articles to their real domain (dry-run by default)
python scheduler.py                            # Daily pipeline at 15:00 + email notification
python setup_db.py                             # One-time DB init + first admin user
python create_admin.py [--role user|power|admin]
```

## Architecture

### Data Flow

```
SerpAPI (Google News)            MFA Algeria (Next.js JSON)
    ↓ search_flow_news.py            ↓ search_mfa_gov.py
bundle/*.json  (intermediate JSON files)
    ↓ extract_articles_reworked.py
ExtractionResult objects   (fetches URLs, extracts text via trafilatura)
    ↓ ingest_to_dz_news_reworked.py
MySQL database             (articles + sources tables)
    ↓ extract_bulk.py      (fills missing content_text, filters irrelevant)
    ↓ app.py (Flask)
Web dashboard              (browse, label, translate, export)
```

`scheduler.py` (container `dz-scheduler`) runs search → ingest → extract_bulk daily at 15:00 and emails a summary (`SMTP_*`, `NOTIFY_TO`).

### Key Modules

- **config/config.py** — Central `Settings` dataclass loaded from `.env`. Two init modes: `init_context()` for Flask app factory, `init_cli()` for standalone scripts. All other modules import from here.
- **search_flow_news.py** — Builds SerpAPI queries with a multi-query fallback strategy (preferred domains → broad .dz search → fallback). Outputs JSON bundles to `./bundle/`.
- **extract_articles_reworked.py** — Fetches URLs, extracts content via trafilatura, detects language/dates/topics, returns `ExtractionResult` dataclasses. Handles dedup by URL canonicalization.
- **ingest_to_dz_news_reworked.py** — Maps `ExtractionResult` → MySQL. Handles source upserts, content-hash dedup, and title fuzzy matching.
- **extract_bulk.py** — Extracts articles where `content_text IS NULL`. Articles without Slovak context are soft-deleted, commercial content gets `relevance=0`, failures are stored in `fetch_error`.
- **classify_ai.py** — AI check "is this sports news?" via NVIDIA API (build.nvidia.com, OpenAI-compatible, `NVIDIA_API_KEY`, `NVIDIA_MODEL`). Called from `extract_bulk.py` (before translation) and `refetch_article.py`. Sports articles get `ai_label='sport'`, soft delete, `relevance=0`, note `auto: sport (AI) – <reason>`. Each article is classified once (`ai_checked_at`); articles with manual `relevance` are never touched; API failures leave the article unclassified for a later retry. Rows are never hard-deleted so `url_hash` dedup prevents re-ingest. Browse filter `del=ai` shows AI-excluded articles.
- **source_relink.py** — SerpAPI sometimes returns only a `google.com/goto?url=…` redirect, so ingest files the article under `google.com`. After fetch, `relink_source()` (called from `extract_bulk.py` and `refetch_article.py`) moves the article to the domain of `final_url` (on HTTP errors `final_url` is taken from the error response). If another active article already has that URL, the redirected one is soft-deleted with note `auto: duplikát #<id>` (only when `relevance IS NULL`).
- **translate.py** — AR→FR translation with three backends: `local` (MarianMT service at `TRANSLATOR_URL`), `deepl`, `azure`. The backend chosen in the admin UI is stored in `config/translator_backend` (gitignored) and overrides `TRANSLATOR_BACKEND` from `.env`.
- **app.py** — Flask app with ~53 routes. Uses SQLAlchemy `text()` queries directly (no ORM models). Filter state is built as SQL fragments via helper functions. Route groups:
  - browse / stats / export (CSV, Word), errors
  - article detail, label, edit, fetch, translate, set_content, delete/undelete
  - search (SerpAPI, MFA) and process (ingest, extract_bulk as background jobs, `/process/job/<id>`)
  - sources (create, note, avoid/unavoid), users
  - `/admin/queries`, `/admin/search-terms`, `/admin/maintenance` (translator backend, fix-dates, DB backup/restore)
- **Roles** — hierarchy `user < power < admin` (`User.has_role()`), decorators `user_required`, `power_required`, `admin_required`.

### Configuration

All config comes from `.env` (loaded via python-dotenv). Key variables:
- `SERPAPI_KEY` — Required for news search
- `DB_HOST/PORT/NAME/USER/PASS` — MySQL connection
- `PREFERRED_DOMAINS` — CSV of prioritized Algerian news domains
- `FLASK_PORT`, `FLASK_SECRET_KEY`
- `SERP_HL/GL/NUM/WHEN/ENGINE` — SerpAPI search defaults
- `TRANSLATOR_BACKEND`, `TRANSLATOR_URL`, `DEEPL_API_KEY`, `AZURE_TRANSLATOR_KEY/REGION` — translation
- `SMTP_HOST/PORT/USER/PASS`, `NOTIFY_TO` — scheduler email notifications
- `NVIDIA_API_KEY`, `NVIDIA_MODEL`, `AI_CLASSIFY_ENABLED`, `AI_CLASSIFY_DELAY` — AI sport classification (disabled without key)
- `EXTRACT_MAX`, `EXTRACT_ONLY_MISSING`, `LOG_FILE/LEVEL/PREFIX`
- There is .env.local file, where are put variables for development PC and server 
- See `.env.example` for the full list.

Static config files in `config/`:
- `sources.json` — Preferred domain list
- `ranking.json` — Per-domain quality scores
- `topics.json` — Keyword lists (French/English/Arabic) for politics, economy, security
- `url_cleanup.json` — Query params stripped from URLs before dedup
- `queries.json`, `search_terms.json` — Search query presets and terms (editable via `/admin/queries`, `/admin/search-terms`)

`deploy.sh` is not tracked in git (server-specific); the template is `deploy.sh.example`. See `DEPLOYMENT.md`.

### Test Setup

`tests/conftest.py` injects a fake `Settings` object into `config._CACHED` **before** other modules are imported, preventing any real filesystem or DB access. Tests use `pytest-mock` for mocking HTTP calls and SerpAPI.

### Database

MySQL with utf8mb4 (MariaDB 11.4). Main tables: `articles`, `sources`, `runs`, `run_articles`, `users`. Schema and migrations live in `migrations/` (`000_init_schema.sql`, `001_add_users.sql`, `002_user_roles.sql`, `003_ai_classification.sql`). The app uses soft-delete (`deleted_at` timestamp) and domain muting (`sources.is_avoided`).

#### Table: `articles`

```sql
CREATE TABLE `articles` (
  `id`                   bigint(20) NOT NULL AUTO_INCREMENT,
  `source_id`            int(11) NOT NULL,                          -- FK → sources.id
  `url`                  text NOT NULL,
  `final_url`            text DEFAULT NULL,
  `final_url_canonical`  varchar(1024) DEFAULT NULL,
  `final_url_hash`       char(64) DEFAULT NULL,
  `url_canonical`        varchar(1024) NOT NULL,
  `url_hash`             char(64) NOT NULL,                         -- UNIQUE, dedup key
  `title`                text DEFAULT NULL,
  `normalized_title`     text DEFAULT NULL,
  `title_hash`           char(64) DEFAULT NULL,
  `published_at_text`    varchar(64) DEFAULT NULL,
  `published_at_real`    datetime DEFAULT NULL,
  `published_conf`       varchar(12) DEFAULT NULL,
  `snippet`              text DEFAULT NULL,
  `language`             varchar(10) DEFAULT NULL,
  `lang_detected`        varchar(10) DEFAULT NULL,
  `extraction_ok`        tinyint(1) NOT NULL DEFAULT 0,
  `source_label`         varchar(255) DEFAULT NULL,
  `first_seen_at`        datetime NOT NULL DEFAULT current_timestamp(),
  `last_seen_at`         datetime NOT NULL DEFAULT current_timestamp(),
  `fetched_at`           datetime DEFAULT NULL,
  `http_status`          int(11) DEFAULT NULL,
  `fetch_error`          text DEFAULT NULL,
  `content_text`         mediumtext DEFAULT NULL,
  `content_hash`         char(64) DEFAULT NULL,
  `ingestion_engine`     varchar(30) DEFAULT NULL,
  `ingestion_query_id`   varchar(10) DEFAULT NULL,
  `ingestion_rank`       int(11) DEFAULT NULL,
  `relevance`            tinyint(4) DEFAULT NULL,                   -- labeling: NULL/0/1/2
  `relevance_note`       varchar(255) DEFAULT NULL,
  `deleted_at`           datetime DEFAULT NULL,                     -- soft delete
  `content_text_fr`      longtext DEFAULT NULL,
  `snippet_fr`           text DEFAULT NULL,
  `ai_label`             varchar(20) DEFAULT NULL,                  -- AI classification: 'sport' | 'other' | NULL
  `ai_checked_at`        datetime DEFAULT NULL                      -- when classified (once per article)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
```

#### Table: `users`

`id`, `username` (UNIQUE), `password_hash`, `role` (`user`/`power`/`admin`, CHECK constraint), `created_at`.

### Docker

`docker-compose.yml` defines two containers from image `eavfeavf/dz-news`, both with `network_mode: host` to reach a Synology-hosted MySQL instance:
- `dz-app` — runs as a non-root user on port 5088 via gunicorn (2 workers, 4 threads)
- `dz-scheduler` — runs `python scheduler.py`

`entrypoint.sh` waits until MariaDB accepts connections before starting the main command.
