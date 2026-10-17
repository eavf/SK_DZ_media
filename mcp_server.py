"""
mcp_server.py — MCP server pre AI klientov (Claude Desktop, Claude Code, ...) nad read-only API (api.py).

Beží na počítači s AI klientom (stdio), aplikáciu volá cez HTTP — nepotrebuje prístup k DB.

Konfigurácia (env):
    DZ_API_URL   základná URL aplikácie, napr. https://dznews.example.sk  (bez /api)
    DZ_API_KEY   jeden z kľúčov z API_KEYS v .env aplikácie

Inštalácia:  pip install -r requirements-mcp.txt
Claude Code: claude mcp add dz-news --env DZ_API_URL=... --env DZ_API_KEY=... -- python /cesta/k/mcp_server.py
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
        "Arabské články majú francúzsky preklad v poliach *_fr."
    ),
)


def _get(path: str, params: dict[str, Any] | None = None) -> dict:
    base = os.getenv("DZ_API_URL", "").rstrip("/")
    key = os.getenv("DZ_API_KEY", "")
    if not base or not key:
        raise ToolError("Nastav DZ_API_URL a DZ_API_KEY v konfigurácii MCP servera")
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "", [])}
    url = f"{base}/api{path}" + (f"?{urllib.parse.urlencode(clean)}" if clean else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
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
def get_filters() -> dict:
    """Dostupné hodnoty filtrov: témy, relevancia, jazyky a najčastejšie zdroje."""
    return _get("/meta")


if __name__ == "__main__":
    server.run()  # stdio
