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
```

## Architecture

### Data Flow

```
SerpAPI (Google News)
    ↓ search_flow_news.py
bundle/news_bundle_*.json  (intermediate JSON files)
    ↓ extract_articles_reworked.py
ExtractionResult objects   (fetches URLs, extracts text via trafilatura)
    ↓ ingest_to_dz_news_reworked.py
MySQL database             (articles + sources tables)
    ↓ app.py (Flask)
Web dashboard              (browse, label, export)
```

### Key Modules

- **config/config.py** — Central `Settings` dataclass loaded from `.env`. Two init modes: `init_context()` for Flask app factory, `init_cli()` for standalone scripts. All other modules import from here.
- **search_flow_news.py** — Builds SerpAPI queries with a multi-query fallback strategy (preferred domains → broad .dz search → fallback). Outputs JSON bundles to `./bundle/`.
- **extract_articles_reworked.py** — Fetches URLs, extracts content via trafilatura, detects language/dates/topics, returns `ExtractionResult` dataclasses. Handles dedup by URL canonicalization.
- **ingest_to_dz_news_reworked.py** — Maps `ExtractionResult` → MySQL. Handles source upserts, content-hash dedup, and title fuzzy matching.
- **app.py** — Flask app with ~27 routes. Uses SQLAlchemy `text()` queries directly (no ORM models). Filter state is built as SQL fragments via helper functions.

### Configuration

All config comes from `.env` (loaded via python-dotenv). Key variables:
- `SERPAPI_KEY` — Required for news search
- `DB_HOST/PORT/NAME/USER/PASS` — MySQL connection
- `PREFERRED_DOMAINS` — CSV of prioritized Algerian news domains
- `FLASK_PORT`, `FLASK_SECRET_KEY`
- There is .env.local file, where are put variables for development PC and server 

Static config files in `config/`:
- `sources.json` — Preferred domain list
- `ranking.json` — Per-domain quality scores
- `topics.json` — Keyword lists (French/English/Arabic) for politics, economy, security
- `url_cleanup.json` — Query params stripped from URLs before dedup

### Test Setup

`tests/conftest.py` injects a fake `Settings` object into `config._CACHED` **before** other modules are imported, preventing any real filesystem or DB access. Tests use `pytest-mock` for mocking HTTP calls and SerpAPI.

### Database

MySQL with utf8mb4 (MariaDB 11.4). Main tables: `articles`, `sources`, `run_articles`. The app uses soft-delete (`deleted_at` timestamp) and domain muting (`sources.is_avoided`).

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
  `snippet_fr`           text DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
```

### Docker

`docker-compose.yml` uses `network_mode: host` to reach a Synology-hosted MySQL instance. The app runs as a non-root user on port 5088 via gunicorn (2 workers, 4 threads).
