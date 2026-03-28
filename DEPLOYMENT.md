# Deployment – SK_DZ_media

Aplikácia beží v Docker kontajneri na Synology NAS (192.168.0.202, port 5088).
Image je uložený na Docker Hub: `eavfeavf/dz-news:latest`.

## Požiadavky

- Docker nainštalovaný lokálne
- Prístup na Docker Hub účet `eavfeavf`
- SSH prístup na Synology cez alias `synology` v `~/.ssh/config` (port 22222, kľúč `~/.ssh/synology_key`)

> Synology DSM nemá `rsync` — deploy skript používa `scp`.

## Štruktúra na Synology

```
/volume1/docker/dz_news/
├── docker-compose.yml
├── docker-compose.yml
├── templates/          ← volume mount (Flask šablóny)
├── config/             ← volume mount (konfig + JSON súbory)
├── static/             ← volume mount (CSS, JS, obrázky)
├── bundle/             ← volume mount (JSON bundles zo search_flow_news.py)
├── logs/               ← volume mount (log súbory)
├── data/               ← volume mount (dáta)
└── .env                ← environment premenné (DB, API kľúče)
```

> `.env` sa nikdy nesynchronizuje skriptom – musí byť nastavený manuálne na Synology.

## Prvé spustenie – prihlásenie na Docker Hub

```bash
docker login
```

Prihlási na Docker Hub účet `eavfeavf`. Pri ďalších deployoch nie je potrebný (prihlasovacie údaje sú uložené).

---

## Full deploy (zmeny v Python kóde)

```bash
./deploy.sh
```

Skript automaticky:
1. Zbuilduje nový Docker image lokálne
2. Pushne image na Docker Hub (`eavfeavf/dz-news:latest`)
3. Skopíruje `docker-compose.yml`, `templates/`, `config/`, `static/` na Synology cez SCP
4. Nastaví práva na `logs/`, `data/`, `templates/`
5. Pullne nový image na Synology a reštartuje kontajner

## Rýchly deploy (zmeny v config, static alebo templates – bez rebuildu)

```bash
./deploy.sh --no-rebuild
```

Skopíruje `templates/`, `config/` a `static/` na Synology a reštartuje kontajner.
Vhodné pre zmeny v `config/*.json`, `config/*.py`, šablónach alebo statickom obsahu.
**Reštart je nutný** – Python moduly sa načítajú len raz pri štarte.

## Rýchly deploy (zmeny iba v šablónach)

```bash
./deploy.sh --tpl-only
```

Skopíruje iba `templates/` na Synology. Zmeny sú aktívne ihneď po refresh stránky – bez rebuildu a bez reštartu.

## Len reštart kontajnera

```bash
./deploy.sh --restart
```

Reštartuje kontajner na Synology bez kopírovania čohokoľvek.

## Len build a push image (bez nasadenia)

```bash
./deploy.sh --rebuild
```

Zbuilduje a pushne nový image na Docker Hub. Nasadenie treba spustiť samostatne cez `./deploy.sh --restart`.

## Manuálny deploy (ak automatický zlyhá)

```bash
# Lokálne – zbuilduj a pushni image
docker build -t eavfeavf/dz-news:latest . && docker push eavfeavf/dz-news:latest

# Na Synology – pullni nový image a reštartuj
ssh -p 22222 vovo@192.168.0.202
cd /volume1/docker/dz_news
sudo docker compose pull
sudo docker compose up -d
```

## DB migrácie

Spúšťajú sa manuálne cez Container Manager → Terminal:

```bash
python -c "
from config.config import get_db_engine
from sqlalchemy import text
with get_db_engine().begin() as c:
    c.execute(text('ALTER TABLE articles ADD COLUMN IF NOT EXISTS snippet_fr text DEFAULT NULL AFTER content_text_fr'))
print('OK')
"
```

## Časté problémy

### Permission denied na logs/ alebo iných adresároch
```bash
sudo chmod 777 /volume1/docker/dz_news/logs
sudo chmod 777 /volume1/docker/dz_news/data
sudo chmod 777 /volume1/docker/dz_news/templates
sudo chmod 777 /volume1/docker/dz_news/config
sudo chmod 777 /volume1/docker/dz_news/static
```

### MariaDB zablokovala host (too many connection errors)
V phpMyAdmin → SQL:
```sql
FLUSH HOSTS;
```

### Kontajner beží so starým image
```bash
ssh -p 22222 vovo@192.168.0.202
cd /volume1/docker/dz_news
sudo docker compose down
sudo docker compose pull
sudo docker compose up -d
```

### Overenie verzie bežiaceho app.py
V Container Manager → Terminal:
```bash
grep -n "def browse" /app/app.py
```
Nová verzia má `browse` na riadku ~270.