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
- Direct scraping of **MFA Algeria** press releases (no SerpAPI required)
- Full-text extraction via **trafilatura** (HTML) and **pymupdf** (PDF)
- Language detection at search time via **langdetect** (title + snippet), refined by trafilatura during extraction
- Arabic → French translation via **DeepL API**: `title` + `snippet` translated automatically (pipeline + backfill); `content_text` translated only during fresh extraction — articles extracted before DeepL was configured require manual translation via the article detail page
- Relevance labeling and soft-delete workflow
- Export to **Word** and **CSV**
- Scheduled pipeline with **email notifications** (new/re-seen articles, translation status, DeepL usage)
- **Authentication** — admin/read-only access control (session-based, `users` table in DB)
- Dockerized deployment

### Architecture

```
SerpAPI (Google News)  +  MFA Algeria (mfa.gov.dz)
    ↓  search_flow_news.py       — search, save JSON bundles, detect language (langdetect)
    ↓  search_mfa_gov.py         — scrape MFA press releases (integrated or standalone)
bundle/news_bundle_*.json
    ↓  ingest_to_dz_news_reworked.py  — parse & insert into DB (with language field)
MySQL / MariaDB
    ↓  extract_bulk.py           — fetch URLs, extract text, refine lang_detected, translate AR→FR
    ↓  app.py (Flask)
Web dashboard                    — browse, label, export
```

A `scheduler.py` daemon runs the full pipeline daily and sends a detailed email summary (new articles with titles/snippets, re-seen articles, DeepL credit usage).

### Requirements

- Python 3.12+
- MySQL / MariaDB
- [SerpAPI](https://serpapi.com/) key
- [DeepL API](https://www.deepl.com/pro-api) key (optional, for Arabic translation)
- `langdetect` (installed via `requirements.txt`)
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
# Step 1 – search for articles (SerpAPI + MFA Algeria combined)
python search_flow_news.py

# Step 1b – scrape MFA Algeria press releases only (standalone)
python search_mfa_gov.py --when 30d

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

### Authentication

The app uses session-based authentication with four access levels:

| Role | Access |
|---|---|
| Unauthenticated | Browse articles, view article detail, stats |
| `user` | + Run pipeline: search, ingest, extract, translate |
| `power` | + Label, delete, export, modify article records |
| `admin` | + Sources management, user management |

Roles are hierarchical — each level includes everything below it.

**Initial setup** (run once on a new server):

```bash
# 1. Create database and all tables
mysql -u root -p < migrations/000_init_schema.sql

# 2. Create accounts
python create_admin.py                 # admin (default)
python create_admin.py --role power    # power user
python create_admin.py --role user     # regular user
```

> **Existing installations:** migrations `001_add_users.sql` and `002_user_roles.sql` remain available for upgrading older deployments that predate `000_init_schema.sql`.

### Article date resolution

Each article stores three date-related fields:

| DB field | Description |
|---|---|
| `published_at_text` | Raw date string as returned by SerpAPI (e.g. `"Il y a 5 jours"`, `"2025-12-09"`) |
| `published_at_real` | Parsed datetime used for sorting and display |
| `published_conf` | Confidence label: `search` (from SerpAPI) or `absolute` (from HTML extraction) |

**Processing pipeline:**

```
SerpAPI date field
    ↓ search_flow_news._parse_serp_date()
    │
    ├─ absolute date (e.g. "2025-12-09")
    │       → stored as published_at in bundle JSON
    │       → ingest sets published_at_real, published_conf = 'search'
    │
    └─ relative date (e.g. "yesterday", "il y a 3 jours")
            → NOT stored in bundle — too unreliable for re-surfaced articles
            → published_at_real stays NULL until extraction

HTML extraction (trafilatura via extract_bulk.py / refetch_article.py)
    → reads <meta property="article:published_time"> and similar tags
    → overwrites published_at_real, sets published_conf = 'absolute'
```

**Why relative SerpAPI dates are not used:** Google News can re-surface old articles in new searches. A date like `"yesterday"` is then computed relative to the search time, not the article's true publication date — resulting in values that are months off. Only absolute date strings from SerpAPI are trusted at ingest time; all others are resolved later from the article HTML.

**Maintenance:** `fix_serp_dates.py` can be used to reset any `published_at_real` values that were ingested from relative SerpAPI dates (detects them by matching the raw `published_at_text` against relative-date patterns).

### PDF extraction

When a search result links directly to a PDF (detected by `Content-Type: application/pdf` or a `.pdf` URL), the extractor uses **pymupdf** instead of trafilatura.

**Page-level SK filtering:** Only pages containing at least one configured Slovakia search term are extracted and stored. This keeps `content_text` focused on relevant content instead of storing the full newspaper issue.

**Legacy Arabic encoding detection:** Many Algerian Arabic-language newspapers distribute PDF editions using old proprietary font encodings (AXT and similar). The extracted text appears as garbled Latin characters (`÷õGFô`, `ZƒGJ«ªÉ'`). The extractor detects this automatically and sets `fetch_error = PDF_LEGACY_ENCODING` without storing the garbled content.

| `fetch_error` value | Meaning |
|---|---|
| `PDF_EXTRACTION_EMPTY` | pymupdf returned no text (e.g. image-only PDF) |
| `PDF_LEGACY_ENCODING` | Garbled legacy Arabic font encoding — content not stored |

### MFA Algeria scraper

`search_mfa_gov.py` scrapes press releases directly from the Algerian Ministry of Foreign Affairs website (`mfa.gov.dz`) without using SerpAPI.

**How it works:** The MFA site is built with Next.js. The scraper fetches the `buildId` from the page source and then paginates through the `/_next/data/{buildId}/...` JSON endpoint, filtering results by the configured Slovakia search terms.

**Integration:** `search_flow_news.py` calls the scraper automatically and merges its results into the standard bundle alongside SerpAPI results. The domain `mfa.gov.dz` is excluded from SerpAPI queries to avoid duplicates.

**Standalone use:**

```bash
python search_mfa_gov.py --when 30d   # last 30 days (default)
python search_mfa_gov.py --when 7d    # last 7 days
```

Results are saved to a dedicated `mfa_<timestamp>` run directory inside `bundle/runs/` in the same format as `search_flow_news.py`, and can be ingested with the standard `ingest_to_dz_news_reworked.py`.

**SSL note:** `mfa.gov.dz` uses an unverifiable SSL certificate. The scraper bypasses verification (`verify=False`) and suppresses the resulting warnings — this is expected and intentional.

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
- Priame prehľadávanie press releases **MFA Alžírsko** (bez SerpAPI)
- Extrakcia plného textu cez **trafilatura** (HTML) a **pymupdf** (PDF)
- Detekcia jazyka pri vyhľadávaní cez **langdetect** (title + snippet), upresnená trafilaturou pri extrakcii
- Preklad arabčiny do francúzštiny cez **DeepL API**: `title` + `snippet` sa prekladajú automaticky (pipeline + backfill); `content_text` iba pri čerstvej extrakcii — články extrahované pred konfiguráciou DeepL vyžadujú manuálny preklad cez stránku článku
- Označovanie relevancie a soft-delete workflow
- Export do **Word** a **CSV**
- Automatická pipeline s **emailovými notifikáciami** (nové/znovu videné články, stav prekladu, využitie DeepL kreditov)
- **Autentifikácia** — prístupové práva admin / len čítanie (session-based, tabuľka `users` v DB)
- Dockerizované nasadenie

### Architektúra

```
SerpAPI (Google News)  +  MFA Alžírsko (mfa.gov.dz)
    ↓  search_flow_news.py       — vyhľadávanie, uloženie JSON súborov, detekcia jazyka (langdetect)
    ↓  search_mfa_gov.py         — scraping MFA press releases (integrovaný alebo samostatný)
bundle/news_bundle_*.json
    ↓  ingest_to_dz_news_reworked.py  — spracovanie a vloženie do DB (vrátane poľa language)
MySQL / MariaDB
    ↓  extract_bulk.py           — stiahnutie URL, extrakcia textu, upresnenie lang_detected, preklad AR→FR
    ↓  app.py (Flask)
Webový dashboard                 — prehľad, označovanie, export
```

`scheduler.py` spúšťa celú pipeline denne a posiela podrobný emailový súhrn (nové/znovu videné články s titulmi a snippetmi, stav prekladu, využitie DeepL kreditov).

### Požiadavky

- Python 3.12+
- MySQL / MariaDB
- Kľúč [SerpAPI](https://serpapi.com/)
- Kľúč [DeepL API](https://www.deepl.com/pro-api) (voliteľné, pre preklad arabčiny)
- `langdetect` (inštaluje sa cez `requirements.txt`)
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
# Krok 1 – vyhľadanie článkov (SerpAPI + MFA Alžírsko spolu)
python search_flow_news.py

# Krok 1b – len MFA Alžírsko press releases (samostatný režim)
python search_mfa_gov.py --when 30d

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

### Autentifikácia

Aplikácia používa session-based autentifikáciu so štyrmi úrovňami prístupu:

| Rola | Prístup |
|---|---|
| Neprihlásený | Prehliadanie článkov, detail článku, štatistiky |
| `user` | + Spúšťanie pipeline: search, ingest, extrakcia, preklad |
| `power` | + Označovanie, mazanie, export, úprava záznamov článkov |
| `admin` | + Správa zdrojov, správa používateľov |

Roly sú hierarchické — každá úroveň zahŕňa všetko z nižších úrovní.

**Prvotné nastavenie** (spustiť raz na novom serveri):

```bash
# 1. Vytvorenie databázy a všetkých tabuliek
mysql -u root -p < migrations/000_init_schema.sql

# 2. Vytvorenie účtov
python create_admin.py                 # admin (default)
python create_admin.py --role power    # power user
python create_admin.py --role user     # bežný používateľ
```

> **Existujúce inštalácie:** migrácie `001_add_users.sql` a `002_user_roles.sql` sú naďalej k dispozícii pre upgrade starších nasadení, ktoré predchádzajú `000_init_schema.sql`.

### Určovanie dátumu článku

Každý článok uchováva tri dátumové polia:

| Pole v DB | Popis |
|---|---|
| `published_at_text` | Surový reťazec dátumu z SerpAPI (napr. `"Il y a 5 jours"`, `"2025-12-09"`) |
| `published_at_real` | Sparsovaný datetime používaný na zoradenie a zobrazenie |
| `published_conf` | Dôveryhodnosť: `search` (zo SerpAPI) alebo `absolute` (z HTML extrakcie) |

**Procesný postup:**

```
Pole date zo SerpAPI
    ↓ search_flow_news._parse_serp_date()
    │
    ├─ absolútny dátum (napr. "2025-12-09")
    │       → uložený ako published_at v bundle JSON
    │       → ingest nastaví published_at_real, published_conf = 'search'
    │
    └─ relatívny dátum (napr. "yesterday", "il y a 3 jours")
            → NEULOŽENÝ do bundle — nespoľahlivý pre re-surfované staré články
            → published_at_real ostáva NULL až do extrakcie

HTML extrakcia (trafilatura cez extract_bulk.py / refetch_article.py)
    → číta <meta property="article:published_time"> a podobné tagy
    → prepíše published_at_real, nastaví published_conf = 'absolute'
```

**Prečo sa relatívne dátumy zo SerpAPI nepoužívajú:** Google News môže staré články znovu zaradiť do výsledkov vyhľadávania. Dátum ako `"yesterday"` sa potom vypočíta relatívne od času searchu, nie od skutočného dátumu publikovania — výsledok je o mesiace nesprávny. Pri ingeste sa preto dôveruje iba absolútnym reťazcom; ostatné sa doplnia neskôr z HTML článku.

**Údržba:** `fix_serp_dates.py` resetuje prípadné `published_at_real` hodnoty, ktoré boli nastavené z relatívnych dátumov SerpAPI (detekuje ich podľa vzoru v `published_at_text`).

### Extrakcia PDF

Ak výsledok vyhľadávania odkazuje priamo na PDF (detekcia podľa `Content-Type: application/pdf` alebo prípony `.pdf`), extraktor použije **pymupdf** namiesto trafilatura.

**Filtrovanie stránok podľa SK termínov:** Ukladajú sa len stránky, ktoré obsahujú aspoň jeden nakonfigurovaný slovenský vyhľadávací termín. Vďaka tomu `content_text` obsahuje iba relevantnú časť namiesto celého čísla novín.

**Detekcia starého arabského kódovania:** Mnohé alžírske arabské noviny distribuujú PDF vydania so starými proprietárnymi kódovaniami fontov (AXT a pod.). Extrahovaný text sa zobrazí ako nezmyselné Latin znaky (`÷õGFô`, `ZƒGJ«ªÉ'`). Extraktor to automaticky detekuje a nastaví `fetch_error = PDF_LEGACY_ENCODING` bez uloženia obsahu.

| Hodnota `fetch_error` | Význam |
|---|---|
| `PDF_EXTRACTION_EMPTY` | pymupdf nevrátil žiadny text (napr. PDF tvorené len obrázkami) |
| `PDF_LEGACY_ENCODING` | Staré arabské kódovanie fontov — obsah sa neuloží |

### MFA Alžírsko scraper

`search_mfa_gov.py` prehľadáva press releases priamo z webu Ministerstva zahraničných vecí Alžírska (`mfa.gov.dz`) bez SerpAPI.

**Ako to funguje:** Web MFA je postavený na Next.js. Skript načíta `buildId` zo zdrojového kódu stránky a potom stránkuje cez endpoint `/_next/data/{buildId}/...`, pričom filtruje výsledky podľa nakonfigurovaných slovenských termínov.

**Integrácia:** `search_flow_news.py` volá scraper automaticky a zlúči jeho výsledky do štandardného bundle spolu s výsledkami SerpAPI. Doména `mfa.gov.dz` je vylúčená z SerpAPI dopytov, aby nedochádzalo k duplikátom.

**Samostatné spustenie:**

```bash
python search_mfa_gov.py --when 30d   # posledných 30 dní (default)
python search_mfa_gov.py --when 7d    # posledných 7 dní
```

Výsledky sa uložia do dedikovaného adresára `mfa_<timestamp>` v `bundle/runs/` v rovnakom formáte ako `search_flow_news.py` a možno ich ingestovať štandardným `ingest_to_dz_news_reworked.py`.

**Poznámka k SSL:** `mfa.gov.dz` používa neoveriteľný SSL certifikát. Skript obchádza overenie (`verify=False`) a potláča príslušné varovania — je to zámerné.

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
- Collecte directe des communiqués de presse du **MAE Algérie** (sans SerpAPI)
- Extraction du texte intégral via **trafilatura** (HTML) et **pymupdf** (PDF)
- Détection de la langue dès la recherche via **langdetect** (titre + extrait), affinée par trafilatura à l'extraction
- Traduction arabe → français via **l'API DeepL** : `title` + `snippet` traduits automatiquement (pipeline + remplissage) ; `content_text` uniquement lors d'une extraction fraîche — les articles extraits avant la configuration de DeepL nécessitent une traduction manuelle via la page de détail
- Étiquetage de pertinence et workflow de suppression douce
- Export en **Word** et **CSV**
- Pipeline planifié avec **notifications e-mail détaillées** (articles nouveaux/revus, état de traduction, utilisation des crédits DeepL)
- **Authentification** — contrôle d'accès admin / lecture seule (session Flask, table `users` en base)
- Déploiement dockerisé

### Architecture

```
SerpAPI (Google Actualités)  +  MAE Algérie (mfa.gov.dz)
    ↓  search_flow_news.py       — recherche, bundles JSON, détection langue (langdetect)
    ↓  search_mfa_gov.py         — collecte MAE (intégré ou autonome)
bundle/news_bundle_*.json
    ↓  ingest_to_dz_news_reworked.py  — traitement et insertion en base (champ language inclus)
MySQL / MariaDB
    ↓  extract_bulk.py           — récupération des URL, extraction, affinage lang_detected, traduction AR→FR
    ↓  app.py (Flask)
Tableau de bord web              — navigation, étiquetage, export
```

Le démon `scheduler.py` exécute le pipeline quotidiennement et envoie un e-mail détaillé (articles nouveaux/revus avec titres et extraits, état des traductions, utilisation des crédits DeepL).

### Prérequis

- Python 3.12+
- MySQL / MariaDB
- Clé [SerpAPI](https://serpapi.com/)
- Clé [API DeepL](https://www.deepl.com/pro-api) (optionnel, pour la traduction de l'arabe)
- `langdetect` (installé via `requirements.txt`)
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
# Étape 1 – recherche des articles (SerpAPI + MAE Algérie combinés)
python search_flow_news.py

# Étape 1b – collecte MAE Algérie uniquement (mode autonome)
python search_mfa_gov.py --when 30d

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

### Authentification

L'application utilise une authentification par session avec quatre niveaux d'accès :

| Rôle | Accès |
|---|---|
| Non connecté | Navigation des articles, détail article, statistiques |
| `user` | + Exécuter le pipeline : recherche, ingestion, extraction, traduction |
| `power` | + Étiquetage, suppression, export, modification des enregistrements |
| `admin` | + Gestion des sources, gestion des utilisateurs |

Les rôles sont hiérarchiques — chaque niveau inclut tout ce qui est en dessous.

**Configuration initiale** (à exécuter une fois sur un nouveau serveur) :

```bash
# 1. Créer la base de données et toutes les tables
mysql -u root -p < migrations/000_init_schema.sql

# 2. Créer les comptes
python create_admin.py                 # admin (défaut)
python create_admin.py --role power    # power user
python create_admin.py --role user     # utilisateur standard
```

> **Installations existantes :** les migrations `001_add_users.sql` et `002_user_roles.sql` restent disponibles pour mettre à niveau les déploiements antérieurs à `000_init_schema.sql`.

### Résolution de la date des articles

Chaque article stocke trois champs liés à la date :

| Champ DB | Description |
|---|---|
| `published_at_text` | Chaîne brute renvoyée par SerpAPI (ex. `"Il y a 5 jours"`, `"2025-12-09"`) |
| `published_at_real` | Datetime analysé utilisé pour le tri et l'affichage |
| `published_conf` | Niveau de confiance : `search` (SerpAPI) ou `absolute` (extraction HTML) |

**Pipeline de traitement :**

```
Champ date de SerpAPI
    ↓ search_flow_news._parse_serp_date()
    │
    ├─ date absolue (ex. "2025-12-09")
    │       → stockée comme published_at dans le bundle JSON
    │       → l'ingestion définit published_at_real, published_conf = 'search'
    │
    └─ date relative (ex. "yesterday", "il y a 3 jours")
            → NON stockée dans le bundle — trop peu fiable pour les anciens articles re-surfacés
            → published_at_real reste NULL jusqu'à l'extraction

Extraction HTML (trafilatura via extract_bulk.py / refetch_article.py)
    → lit <meta property="article:published_time"> et balises similaires
    → écrase published_at_real, définit published_conf = 'absolute'
```

**Pourquoi les dates relatives de SerpAPI ne sont pas utilisées :** Google News peut remettre en avant d'anciens articles dans de nouvelles recherches. Une date comme `"yesterday"` est alors calculée par rapport à l'heure de la recherche, et non à la date de publication réelle — ce qui peut donner un résultat décalé de plusieurs mois. Seules les chaînes de dates absolues de SerpAPI sont fiables à l'ingestion ; les autres sont résolues ultérieurement depuis le HTML de l'article.

**Maintenance :** `fix_serp_dates.py` réinitialise les valeurs `published_at_real` éventuellement définies à partir de dates relatives SerpAPI (détectées par correspondance de motif dans `published_at_text`).

### Extraction PDF

Lorsqu'un résultat de recherche pointe directement vers un PDF (détecté par `Content-Type: application/pdf` ou l'extension `.pdf`), l'extracteur utilise **pymupdf** à la place de trafilatura.

**Filtrage des pages par termes SK :** Seules les pages contenant au moins un terme de recherche Slovaquie configuré sont extraites et stockées. Cela permet de conserver dans `content_text` uniquement le contenu pertinent plutôt que l'intégralité du numéro du journal.

**Détection de l'encodage arabe hérité :** De nombreux journaux algériens en langue arabe distribuent leurs éditions PDF avec d'anciens encodages de polices propriétaires (AXT et similaires). Le texte extrait apparaît sous forme de caractères latins illisibles (`÷õGFô`, `ZƒGJ«ªÉ'`). L'extracteur détecte cela automatiquement et définit `fetch_error = PDF_LEGACY_ENCODING` sans stocker le contenu illisible.

| Valeur de `fetch_error` | Signification |
|---|---|
| `PDF_EXTRACTION_EMPTY` | pymupdf n'a retourné aucun texte (ex. PDF composé uniquement d'images) |
| `PDF_LEGACY_ENCODING` | Encodage de police arabe hérité — le contenu n'est pas stocké |

### Collecteur MAE Algérie

`search_mfa_gov.py` collecte les communiqués de presse directement depuis le site du Ministère algérien des Affaires étrangères (`mfa.gov.dz`), sans SerpAPI.

**Fonctionnement :** Le site MAE est développé avec Next.js. Le script récupère le `buildId` depuis le code source de la page, puis pagine à travers l'endpoint `/_next/data/{buildId}/...`, en filtrant les résultats selon les termes de recherche Slovaquie configurés.

**Intégration :** `search_flow_news.py` appelle le collecteur automatiquement et fusionne ses résultats dans le bundle standard avec ceux de SerpAPI. Le domaine `mfa.gov.dz` est exclu des requêtes SerpAPI pour éviter les doublons.

**Utilisation autonome :**

```bash
python search_mfa_gov.py --when 30d   # 30 derniers jours (défaut)
python search_mfa_gov.py --when 7d    # 7 derniers jours
```

Les résultats sont sauvegardés dans un répertoire dédié `mfa_<timestamp>` sous `bundle/runs/`, au même format que `search_flow_news.py`, et peuvent être ingérés avec `ingest_to_dz_news_reworked.py`.

**Note SSL :** `mfa.gov.dz` utilise un certificat SSL non vérifiable. Le script contourne la vérification (`verify=False`) et supprime les avertissements correspondants — c'est intentionnel.

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