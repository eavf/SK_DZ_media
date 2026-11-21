"""
mcp_server.py — MCP server pre AI klientov (Claude, Claude Code, ...) nad read-only API (api.py).

Nepotrebuje prístup k DB, aplikáciu volá cez HTTP. Dva režimy:

1) Lokálne (stdio) — beží na počítači s AI klientom:
    DZ_API_URL   základná URL aplikácie, napr. https://dznews.example.sk  (bez /api)
    DZ_API_KEY   jeden z kľúčov z API_KEYS v .env aplikácie
   Claude Code: claude mcp add dz-news --env DZ_API_URL=... --env DZ_API_KEY=... -- python /cesta/k/mcp_server.py

2) Vzdialene (HTTP, kontajner dz-mcp) — pre claude.ai a aplikáciu Claude (Nastavenia → Konektory →
   Pridať vlastný konektor). Spustenie: python mcp_server.py --http
    MCP_PORT     port (default 5090), MCP_HOST (default 127.0.0.1 — von len cez reverzný proxy s HTTPS)
    MCP_TOKENS   prístupové kľúče (čiarkou), default = API_KEYS
    DZ_API_URL   default http://127.0.0.1:5088, DZ_API_KEY default = prvý z API_KEYS
   Adresa konektora: https://<host>/mcp/<kľúč>  — claude.ai nevie poslať vlastnú hlavičku, kľúč je preto
   v ceste. Iní klienti môžu poslať Authorization: Bearer <kľúč> na https://<host>/mcp.
   Bez platného kľúča server odpovie 404. /health vráti „ok“ (kontrola reverzného proxy).

Inštalácia:  pip install -r requirements-mcp.txt  (v Docker image je)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError  # text sa zobrazí AI (RuntimeError SDK skryje)

server = MCPServer(
    name="dz-news",
    instructions=(
        "Prístup k článkom alžírskych médií o vzťahoch Slovensko–Alžírsko, ktoré zbiera aplikácia DZ News Monitor. "
        "Vyradené články (šport, bez slovenského kontextu, duplikáty) sa nevracajú. "
        "Najprv použi list_articles (predvolene posledných 7 dní), detail a celý text získaš cez get_article. "
        "Ak článok nemá text (has_text = false, fetch_error uvádza dôvod), stiahni stránku z poľa url najprv "
        "vlastným nástrojom na čítanie webu; až keď to zlyhá (blokovanie, 403), použi fetch_article_page — stiahne "
        "ju zo servera aplikácie; pri method = page_text je v texte aj menu webu, vyber z neho len článok. "
        "Táto databáza je len jeden zdroj — pri prehľadoch hľadaj aj sám na webe (prompt prehlad_sk_dz). "
        "Arabské články majú francúzsky preklad v poliach *_fr. Odpovedaj po slovensky, ak používateľ nepíše inak."
    ),
)


def _csv_env(name: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, "").split(",") if v.strip()]


def _get(path: str, params: dict[str, Any] | None = None) -> dict:
    base = os.getenv("DZ_API_URL", "").rstrip("/")
    key = os.getenv("DZ_API_KEY", "") or next(iter(_csv_env("API_KEYS")), "")
    if not base or not key:
        raise ToolError("Nastav DZ_API_URL a DZ_API_KEY v konfigurácii MCP servera")
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "", [])}
    url = f"{base}/api{path}" + (f"?{urllib.parse.urlencode(clean)}" if clean else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body).get("error", body)
        except ValueError:
            msg = body[:300]
        raise ToolError(f"API {e.code}: {msg}") from None
    except urllib.error.URLError as e:
        raise ToolError(f"Aplikácia nedostupná ({base}): {e.reason}") from None


@server.tool()
def list_articles(
    date_from: str | None = None,
    date_to: str | None = None,
    date_field: str = "found",
    relevance: str = "relevant,unrated",
    topic: str | None = None,
    lang: str | None = None,
    source: str | None = None,
    has_text: bool = False,
    include_text: bool = False,
    max_chars: int = 4000,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Zoznam článkov za obdobie (predvolene posledných 7 dní), najnovšie prvé.

    Args:
        date_from: od dátumu YYYY-MM-DD (predvolene date_to − 7 dní)
        date_to: do dátumu YYYY-MM-DD vrátane (predvolene dnes)
        date_field: "found" = kedy aplikácia článok našla, "published" = dátum publikácie
        relevance: čiarkou oddelené: relevant, unrated, irrelevant, all
        topic: čiarkou oddelené témy: politics, economy, security
        lang: čiarkou oddelené jazyky, napr. "ar,fr"
        source: čiarkou oddelené domény zdrojov, napr. "aps.dz"
        has_text: len články so stiahnutým textom
        include_text: pridať text článku (text, text_fr) skrátený na max_chars
        max_chars: max. dĺžka textu pri include_text
        limit: počet článkov (max 200)
        offset: posun pre stránkovanie
    """
    return _get("/articles", {
        "from": date_from, "to": date_to, "date_field": date_field, "relevance": relevance,
        "topic": topic, "lang": lang, "source": source,
        "has_text": "1" if has_text else None, "include_text": "1" if include_text else None,
        "max_chars": max_chars if include_text else None, "limit": limit, "offset": offset,
    })


@server.tool()
def get_article(article_id: int) -> dict:
    """Detail jedného článku vrátane celého textu (originál aj francúzsky preklad)."""
    return _get(f"/articles/{article_id}")


@server.tool()
def fetch_article_page(article_id: int, max_chars: int = 20000) -> dict:
    """Stiahne pôvodnú stránku článku teraz (zo servera aplikácie na Slovensku) a vráti jej text.

    Záložná možnosť: keď článok nemá uložený text (has_text = false) a stránku z jeho url sa nepodarilo
    otvoriť vlastným nástrojom na čítanie webu. Server obchádza časť ochrán proti robotom. Nič neukladá. method: "article" = hlavný text,
    "page_text" = celá stránka vrátane menu (vyber z nej článok), "pdf" = strany PDF so zmienkou o Slovensku.
    Niektoré alžírske weby (aps.dz, elmoudjahid.dz) prístup zo Slovenska blokujú — vtedy vráti chybu.

    Args:
        article_id: ID článku zo zoznamu
        max_chars: max. dĺžka vráteného textu
    """
    return _get(f"/articles/{article_id}/page", {"max_chars": max_chars})


@server.tool()
def get_filters() -> dict:
    """Dostupné hodnoty filtrov: témy, relevancia, jazyky a najčastejšie zdroje."""
    return _get("/meta")


# ── zadanie prehľadu (MCP prompt — v Claude cez „+“ → DZ News) ──────────────────

def _search_terms() -> dict:
    """Termíny a preferované zdroje z config/ (rovnaké ako hľadá aplikácia); bez súborov prázdne."""
    from pathlib import Path
    base = Path(__file__).resolve().parent / "config"
    out = {}
    for name, key in (("search_terms.json", None), ("sources.json", "preferred")):
        try:
            data = json.loads((base / name).read_text(encoding="utf-8"))
            out.update(data if key is None else {key: data.get(key, [])})
        except (OSError, ValueError):
            pass
    return out


def build_overview_prompt(dni: str = "7", zameranie: str = "") -> str:
    from datetime import date, timedelta
    try:
        days = max(1, min(int(str(dni).strip() or 7), 366))
    except ValueError:
        days = 7
    date_to = date.today()
    date_from = date_to - timedelta(days=days)
    cfg = _search_terms()
    sk = ", ".join(cfg.get("slovakia") or ["Slovaquie", "Slovakia", "سلوفاكيا", "Bratislava"])
    dz = ", ".join(cfg.get("algeria") or ["Algérie", "الجزائر"])
    sources = ", ".join(cfg.get("preferred") or ["aps.dz", "elmoudjahid.dz", "elwatan.com", "tsa-algerie.com"])
    focus = f"\n**Zameranie:** {zameranie.strip()} — tomuto daj prednosť, ostatné stručne.\n" if zameranie.strip() else ""
    return f"""Priprav prehľad vzťahov **Slovensko–Alžírsko** za obdobie **{date_from:%d.%m.%Y} – {date_to:%d.%m.%Y}** ({days} {"deň" if days == 1 else "dni" if days < 5 else "dní"}).
{focus}
## Čo do prehľadu patrí
- diplomacia: návštevy, rokovania, telefonáty, listy a blahoželania ústavných činiteľov a ministrov,
  veľvyslanci (akreditácia, stretnutia), politické konzultácie, spoločné komisie
- zmluvy, memorandá, dohody (aj pripravované)
- ekonomika: obchod, investície, slovenské firmy v Alžírsku, veľtrhy a podnikateľské misie, energetika (plyn),
  doprava, zdravotníctvo, obranný priemysel
- vzdelávanie, veda, kultúra, štipendiá, konzulárne a vízové veci
- multilaterálne: keď sa **výslovne** spomína Slovensko alebo jeho postoj (EÚ, OSN, Palestína, Západná Sahara, energetika EÚ)

## Čo nepatrí
- šport (aj keď hrá slovenský a alžírsky tím), reklamy, inzeráty, stránky bez obsahu
- voľby alžírskej diaspóry (volebné obvody v zahraničí), kde je Slovensko len v zozname krajín
- Slovensko len ako slovo bez súvisu s Alžírskom (napr. článok o Slovensku a inej krajine)
- **okrajové zmienky** (Slovensko v zozname krajín na veľtrhu a pod.) — nevyhadzuj, daj ich do samostatnej sekcie

## Zdroje — použi oba
1. **MCP DZ News** (táto aplikácia; zbiera Google News a stránky MZV Alžírska):
   `list_articles(date_from="{date_from}", date_to="{date_to}")`, pri potrebe ďalšie strany (`offset`).
   Text cez `get_article`; ak `has_text` = false, stiahni `url` vlastným nástrojom na čítanie webu
   a až keď to zlyhá, `fetch_article_page`. Arabské články majú francúzsky preklad (`text_fr`).
2. **Vlastné hľadanie na webe** — databáza nie je úplná. Hľadaj po francúzsky, arabsky a anglicky
   kombinácie: [{sk}] × [{dz}]. Alžírske médiá: {sources}, mfa.gov.dz, embbratislava.mfa.gov.dz.
   Slovenské zdroje: mzv.sk (aj Veľvyslanectvo SR v Alžíri), teraz.sk / TASR, vlada.gov.sk, prezident.sk,
   SARIO. Berie sa len to, čo vyšlo v danom období.

## Výstup (po slovensky)
1. **Zhrnutie** — 3–5 viet: najdôležitejšie udalosti obdobia; ak sa nič podstatné nestalo, povedz to.
2. **Udalosti podľa tém** (Diplomacia · Ekonomika a energetika · Ostatné), pri každej:
   dátum (dd.mm.rrrr) — 1–2 vety čo sa stalo — zdroj (doména, odkaz) — `[DB #id]` alebo `[web]`.
   Rovnakú udalosť z viacerých médií spoj do jednej položky so všetkými odkazmi.
3. **Okrajové zmienky** — krátky zoznam.
4. **Nájdené mimo DB** — relevantné články z webu, ktoré v DZ News chýbajú (odkaz + doména).
5. **Poznámky k DB** — články z DZ News, ktoré podľa pravidiel vyššie nie sú relevantné (`#id` + prečo).

Uvádzaj len to, čo máš doložené zdrojom; pri neistote (dátum, kto, čo) to napíš. Nevymýšľaj odkazy."""


@server.prompt(name="prehlad_sk_dz", title="Prehľad SK–DZ",
               description="Prehľad vzťahov Slovensko–Alžírsko za posledné dni: DZ News + vlastné hľadanie na webe")
def prehlad_sk_dz(dni: str = "7", zameranie: str = "") -> str:
    """Zadanie prehľadu.

    Args:
        dni: počet dní dozadu (predvolene 7)
        zameranie: voliteľné — napr. „ekonomika“, „návšteva ministra“, „plyn“
    """
    return build_overview_prompt(dni, zameranie)


# ── vzdialený režim (HTTP) ─────────────────────────────────────────────────────

MCP_PATH = "/mcp"


class TokenAuth:
    """ASGI obal: pustí len /mcp/<kľúč> (prepíše na /mcp) alebo /mcp s Authorization: Bearer <kľúč>."""

    def __init__(self, app, tokens: list[str]):
        if not tokens:
            raise SystemExit("Chýba MCP_TOKENS (alebo API_KEYS) — vzdialený MCP server bez kľúča nespustím")
        self.app, self.tokens = app, tokens

    def _valid(self, given: str) -> bool:
        import hmac
        return bool(given) and any(hmac.compare_digest(given.encode(), t.encode()) for t in self.tokens)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":                      # lifespan — spúšťa správcu MCP relácií
            return await self.app(scope, receive, send)
        path = scope["path"].rstrip("/") or "/"
        if path == "/health":
            return await self._plain(send, 200, b"ok")
        parts = path.split("/")                          # ['', 'mcp', '<kľúč>']
        auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if (len(parts) == 3 and parts[1] == MCP_PATH[1:] and self._valid(parts[2])) or \
                (path == MCP_PATH and self._valid(bearer)):
            scope = {**scope, "path": MCP_PATH, "raw_path": MCP_PATH.encode()}
            return await self.app(scope, receive, send)
        return await self._plain(send, 404, b"Not Found")   # neprezradí, že tu MCP server je

    @staticmethod
    async def _plain(send, status: int, body: bytes):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def http_app():
    from mcp.server.transport_security import TransportSecuritySettings
    os.environ.setdefault("DZ_API_URL", "http://127.0.0.1:5088")
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,       # každá požiadavka samostatne — prežije reštart kontajnera aj viac klientov
        json_response=True,
        # ochrana pred DNS rebinding je pre servery bez autentifikácie na localhoste; tu je kľúč a reverzný proxy
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return TokenAuth(app, _csv_env("MCP_TOKENS") or _csv_env("API_KEYS"))


if __name__ == "__main__":
    import sys
    if "--http" in sys.argv:
        import uvicorn
        uvicorn.run(http_app(), host=os.getenv("MCP_HOST", "127.0.0.1"), port=int(os.getenv("MCP_PORT", "5090")),
                    proxy_headers=True, forwarded_allow_ips="127.0.0.1",
                    access_log=False)   # access log by vypísal cestu aj s kľúčom (/mcp/<kľúč>)
    else:
        server.run()  # stdio
