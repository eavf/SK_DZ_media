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

Static config files in `config/`:
- `sources.json` — Preferred domain list
- `ranking.json` — Per-domain quality scores
- `topics.json` — Keyword lists (French/English/Arabic) for politics, economy, security
- `url_cleanup.json` — Query params stripped from URLs before dedup

### Test Setup

`tests/conftest.py` injects a fake `Settings` object into `config._CACHED` **before** other modules are imported, preventing any real filesystem or DB access. Tests use `pytest-mock` for mocking HTTP calls and SerpAPI.

### Database

MySQL with utf8mb4. Schema inferred from code — no migration files. Main tables: `articles`, `sources`, `run_articles`. The app uses soft-delete (`deleted_at` timestamp) and domain muting (`sources.is_avoided`).

### Docker

`docker-compose.yml` uses `network_mode: host` to reach a Synology-hosted MySQL instance. The app runs as a non-root user on port 5088 via gunicorn (2 workers, 4 threads).
