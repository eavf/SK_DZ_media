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
        "Ak článok nemá text (has_text = false, fetch_error uvádza dôvod), skús fetch_article_page — stiahne "
        "pôvodnú stránku teraz; pri method = page_text je v texte aj menu webu, vyber z neho len článok. "
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
    """Stiahne pôvodnú stránku článku teraz (zo servera na Slovensku) a vráti jej text.

    Použi, keď článok nemá uložený text (has_text = false). Nič neukladá. method: "article" = hlavný text,
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
