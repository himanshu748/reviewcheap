"""Tests for the MCP endpoint (/mcp).

Covers the JSON-RPC 2.0 surface exposed by mcp_server.handle and the FastAPI
POST /mcp route: protocol negotiation, tool listing, tool calls, and error
handling. No network or LLM calls are made.
"""

import json

import pytest
from fastapi.testclient import TestClient

import mcp_server
from agent import ALLOWED_MODELS
from main import app

client = TestClient(app)


def _result_text(resp):
    """Return the parsed ``text`` field of the first content item."""
    result = resp.json()["result"]
    return json.loads(result["content"][0]["text"])


# --- protocol handshake -----------------------------------------------------


def test_initialize_negotiates_protocol_and_advertises_tools():
    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    result = body["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "reviewcheap"


def test_initialize_falls_back_for_unknown_protocol_version():
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "1999-01-01"}},
    )
    assert r.status_code == 200
    assert r.json()["result"]["protocolVersion"] == "2025-06-18"


# --- tools/list -------------------------------------------------------------


def test_tools_list_returns_both_tools():
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                  "method": "tools/list"})
    assert r.status_code == 200
    tools = r.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"review_pr", "list_models"}


# --- tools/call -------------------------------------------------------------


def test_tools_call_list_models_returns_allowed_models():
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                  "method": "tools/call",
                                  "params": {"name": "list_models",
                                             "arguments": {}}})
    assert r.status_code == 200
    data = _result_text(r)
    assert data["models"] == ALLOWED_MODELS
    assert data["default"] in ALLOWED_MODELS


@pytest.mark.asyncio
async def test_tools_call_review_pr_mocks_network(monkeypatch):
    async def fake_fetch(pr_url, token):
        return {
            "title": "Fix bug",
            "author": "alice",
            "base_branch": "main",
            "head_branch": "fix-bug",
            "commits": 1,
            "changed_files": 1,
            "additions": 10,
            "deletions": 2,
            "signals": {"has_tests": True},
        }

    async def fake_review(pr_data, model=None):
        return {"score": 95, "verdict": "APPROVE", "summary": "Looks good"}

    monkeypatch.setattr(mcp_server, "fetch_pr_data", fake_fetch)
    monkeypatch.setattr(mcp_server, "review_pr", fake_review)

    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "review_pr",
                         "arguments": {"pr_url": "https://github.com/a/b/pull/1"}}},
    )
    assert r.status_code == 200
    data = _result_text(r)
    assert data["metadata"]["title"] == "Fix bug"
    assert data["review"]["verdict"] == "APPROVE"
    assert isinstance(data["review"]["duration_ms"], int)


def test_tools_call_unknown_tool_returns_error_text():
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                  "method": "tools/call",
                                  "params": {"name": "nope",
                                             "arguments": {}}})
    assert r.status_code == 200
    content = r.json()["result"]["content"][0]
    assert content["isError"] is True
    assert "unknown tool" in content["text"]


# --- errors -----------------------------------------------------------------


def test_invalid_jsonrpc_request_returns_rpc_error_not_500():
    r = client.post("/mcp", json={"unexpected": True})
    assert r.status_code == 200  # never a 500
    body = r.json()
    assert body["error"]["code"] == -32600
    assert "invalid json-rpc" in body["error"]["message"].lower()


def test_unknown_method_returns_method_not_found():
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                  "method": "bogus"})
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32601


def test_notification_returns_202():
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "ping"})
    assert r.status_code == 202


def test_malformed_json_body_returns_400():
    r = client.post(
        "/mcp",
        data="{not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
