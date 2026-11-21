"""Tests for mcp_server.TokenAuth — vzdialený MCP server pustí len požiadavky s platným kľúčom."""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp")
import mcp_server  # noqa: E402


def _call(path, headers=None):
    seen, sent = {}, []

    async def app(scope, receive, send):
        seen.update(path=scope["path"], raw=scope.get("raw_path"))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"mcp"})

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "path": path, "raw_path": path.encode(),
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
    asyncio.run(mcp_server.TokenAuth(app, ["k1", "k2"])(scope, receive, send))
    return sent[0]["status"], seen


@pytest.mark.parametrize("path,headers", [
    ("/mcp/k2", None), ("/mcp/k1/", None), ("/mcp", {"Authorization": "Bearer k1"}),
])
def test_valid_key_rewritten_to_mcp(path, headers):
    status, seen = _call(path, headers)
    assert status == 200 and seen == {"path": "/mcp", "raw": b"/mcp"}


@pytest.mark.parametrize("path,headers", [
    ("/mcp", None), ("/mcp/zly", None), ("/mcp/k1/x", None), ("/", None),
    ("/mcp", {"Authorization": "Bearer zly"}), ("/mcp/zly", {"Authorization": "Bearer k1"}),
])
def test_invalid_404(path, headers):
    status, seen = _call(path, headers)
    assert status == 404 and not seen


def test_health():
    assert _call("/health")[0] == 200


def test_no_tokens_refuses_to_start():
    with pytest.raises(SystemExit):
        mcp_server.TokenAuth(lambda *a: None, [])
