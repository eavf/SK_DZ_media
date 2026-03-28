# DZ News Monitor

> **Language / Jazyk / Langue:** [English](#english) · [Slovenčina](#slovenčina) · [Français](#français)

![Docker Image](https://img.shields.io/docker/v/eavfeavf/dz-news?label=Docker%20Hub&logo=docker)
![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python)
![Flask](https://img.shields.io/badge/Flask-3.x-lightgrey?logo=flask)
![MariaDB](https://img.shields.io/badge/MariaDB-11.4-blue?logo=mariadb)
![License](https://img.shields.io/badge/license-MIT-green)

---

## English

### About

**DZ News Monitor** is a Flask web application that tracks Algerian media coverage of Slovakia–Algeria relations. It automatically searches for relevant news articles, extracts their content, and presents them in a review dashboard where articles can be labeled for relevance and exported for analysis.

![Dashboard](docs/dashboard.png)

### Features

- Automated daily news search via **SerpAPI** (Google News)
- Full-text extraction via **trafilatura**
- Arabic → French translation via **DeepL API**
- Relevance labeling and soft-delete workflow
- Export to **Word** and **CSV**
- Scheduled pipeline with **email notifications**
- Dockerized deployment

### Architecture

```
SerpAPI (Google News)
    ↓  search_flow_news.py       — search & save JSON bundles
bundle/news_bundle_*.json
    ↓  ingest_to_dz_news_reworked.py  — parse & insert into DB
MySQL / MariaDB
    ↓  extract_bulk.py           — fetch URLs, extract text, translate AR→FR
    ↓  app.py (Flask)
Web dashboard                    — browse, label, export
```

A `scheduler.py` daemon runs the full pipeline daily and sends an email summary.

### Requirements

- Python 3.12+
- MySQL / MariaDB
- [SerpAPI](https://serpapi.com/) key
- [DeepL API](https://www.deepl.com/pro-api) key (optional, for Arabic translation)
- Docker (for containerized deployment)

### Installation

```bash
git clone https://github.com/eavf/SK_DZ_media.git
cd SK_DZ_media

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

### Configuration

Key `.env` variables:

| Variable | Description |
|---|---|
| `SERPAPI_KEY` | SerpAPI key for Google News search |
| `DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASS` | MySQL connection |
| `DEEPL_API_KEY` | DeepL API key (optional) |
| `FLASK_SECRET_KEY` | Flask session secret |
| `FLASK_PORT` | Web server port (default: `5088`) |
| `SMTP_HOST / SMTP_USER / SMTP_PASS / NOTIFY_TO` | Email notifications (optional) |

Static config files in `config/`:

| File | Purpose |
|---|---|
| `sources.json` | Preferred Algerian news domains |
| `ranking.json` | Per-domain quality scores |
| `topics.json` | Keyword lists (FR / EN / AR) for topic detection |
| `search_terms.json` | SerpAPI query terms |
| `url_cleanup.json` | Query params stripped from URLs before dedup |

### Usage

**Run the data pipeline manually:**

```bash
# Step 1 – search for articles
python search_flow_news.py

# Step 2 – ingest into database
python ingest_to_dz_news_reworked.py

# Step 3 – extract full text (+ translate Arabic)
python extract_bulk.py
```

**Start the web dashboard:**

```bash
python app.py
# → http://localhost:5088
```

**Run the scheduler (daily automation):**

```bash
python scheduler.py
```

### Docker

```bash
docker compose up --build
```

The image is also available on Docker Hub:

```bash
docker pull eavfeavf/dz-news:latest
```

---

## Slovenčina

### O projekte

**DZ News Monitor** je Flask webová aplikácia na sledovanie alžírskych mediálnych správ o vzťahoch Slovensko–Alžírsko. Automaticky vyhľadáva relevantné články, extrahuje ich obsah a zobrazuje ich v prehľadovom dashboarde, kde ich možno označiť podľa relevancie a exportovať na analýzu.

![Dashboard](docs/dashboard.png)

### Funkcie

- Automatické denné vyhľadávanie cez **SerpAPI** (Google News)
- Extrakcia plného textu cez **trafilatura**
- Preklad arabčiny do francúzštiny cez **DeepL API**
- Označovanie relevancie a soft-delete workflow
- Export do **Word** a **CSV**
- Automatická pipeline s **emailovými notifikáciami**
- Dockerizované nasadenie

### Architektúra

```
SerpAPI (Google News)
    ↓  search_flow_news.py       — vyhľadávanie a uloženie JSON súborov
bundle/news_bundle_*.json
    ↓  ingest_to_dz_news_reworked.py  — spracovanie a vloženie do DB
MySQL / MariaDB
    ↓  extract_bulk.py           — stiahnutie URL, extrakcia textu, preklad AR→FR
    ↓  app.py (Flask)
Webový dashboard                 — prehľad, označovanie, export
```

`scheduler.py` spúšťa celú pipeline denne a posiela emailový súhrn.

### Požiadavky

- Python 3.12+
- MySQL / MariaDB
- Kľúč [SerpAPI](https://serpapi.com/)
- Kľúč [DeepL API](https://www.deepl.com/pro-api) (voliteľné, pre preklad arabčiny)
- Docker (pre kontajnerizované nasadenie)

### Inštalácia

```bash
git clone https://github.com/eavf/SK_DZ_media.git
cd SK_DZ_media

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Skopíruj `.env.example` do `.env` a vyplň prístupové údaje:

```bash
cp .env.example .env
```

### Konfigurácia

Kľúčové premenné v `.env`:

| Premenná | Popis |
|---|---|
| `SERPAPI_KEY` | Kľúč SerpAPI pre Google News |
| `DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASS` | MySQL pripojenie |
| `DEEPL_API_KEY` | Kľúč DeepL (voliteľné) |
| `FLASK_SECRET_KEY` | Secret pre Flask session |
| `FLASK_PORT` | Port webservera (default: `5088`) |
| `SMTP_HOST / SMTP_USER / SMTP_PASS / NOTIFY_TO` | Emailové notifikácie (voliteľné) |

### Použitie

**Manuálne spustenie pipeline:**

```bash
# Krok 1 – vyhľadanie článkov
python search_flow_news.py

# Krok 2 – ingest do databázy
python ingest_to_dz_news_reworked.py

# Krok 3 – extrakcia textu (+ preklad arabčiny)
python extract_bulk.py
```

**Spustenie webového dashboardu:**

```bash
python app.py
# → http://localhost:5088
```

**Spustenie schedulera (denná automatizácia):**

```bash
python scheduler.py
```

### Docker

```bash
docker compose up --build
```

Image je dostupný aj na Docker Hub:

```bash
docker pull eavfeavf/dz-news:latest
```

---

## Français

### À propos

**DZ News Monitor** est une application web Flask qui surveille la couverture médiatique algérienne des relations Slovaquie–Algérie. Elle recherche automatiquement les articles pertinents, en extrait le contenu et les présente dans un tableau de bord de révision permettant de les étiqueter par pertinence et de les exporter pour analyse.

![Tableau de bord](docs/dashboard.png)

### Fonctionnalités

- Recherche automatique quotidienne via **SerpAPI** (Google Actualités)
- Extraction du texte intégral via **trafilatura**
- Traduction arabe → français via **l'API DeepL**
- Étiquetage de pertinence et workflow de suppression douce
- Export en **Word** et **CSV**
- Pipeline planifié avec **notifications par e-mail**
- Déploiement dockerisé

### Architecture

```
SerpAPI (Google Actualités)
    ↓  search_flow_news.py       — recherche et sauvegarde des bundles JSON
bundle/news_bundle_*.json
    ↓  ingest_to_dz_news_reworked.py  — traitement et insertion en base
MySQL / MariaDB
    ↓  extract_bulk.py           — récupération des URL, extraction, traduction AR→FR
    ↓  app.py (Flask)
Tableau de bord web              — navigation, étiquetage, export
```

Le démon `scheduler.py` exécute le pipeline quotidiennement et envoie un résumé par e-mail.

### Prérequis

- Python 3.12+
- MySQL / MariaDB
- Clé [SerpAPI](https://serpapi.com/)
- Clé [API DeepL](https://www.deepl.com/pro-api) (optionnel, pour la traduction de l'arabe)
- Docker (pour le déploiement conteneurisé)

### Installation

```bash
git clone https://github.com/eavf/SK_DZ_media.git
cd SK_DZ_media

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copiez `.env.example` en `.env` et renseignez vos identifiants :

```bash
cp .env.example .env
```

### Configuration

Variables clés dans `.env` :

| Variable | Description |
|---|---|
| `SERPAPI_KEY` | Clé SerpAPI pour Google Actualités |
| `DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASS` | Connexion MySQL |
| `DEEPL_API_KEY` | Clé DeepL (optionnel) |
| `FLASK_SECRET_KEY` | Secret de session Flask |
| `FLASK_PORT` | Port du serveur web (défaut : `5088`) |
| `SMTP_HOST / SMTP_USER / SMTP_PASS / NOTIFY_TO` | Notifications e-mail (optionnel) |

### Utilisation

**Exécution manuelle du pipeline :**

```bash
# Étape 1 – recherche des articles
python search_flow_news.py

# Étape 2 – ingestion en base de données
python ingest_to_dz_news_reworked.py

# Étape 3 – extraction du texte (+ traduction de l'arabe)
python extract_bulk.py
```

**Démarrage du tableau de bord web :**

```bash
python app.py
# → http://localhost:5088
```

**Démarrage du planificateur (automatisation quotidienne) :**

```bash
python scheduler.py
```

### Docker

```bash
docker compose up --build
```

L'image est également disponible sur Docker Hub :

```bash
docker pull eavfeavf/dz-news:latest
```

---

*Developed for monitoring Slovak–Algerian media relations.*