"""Core tests for the v2 metrics-driven PR review agent.

Covers PR URL validation, objective signal extraction, issue-ref parsing,
the deterministic health-score/severity-gate/verdict logic, cost estimation,
and the FastAPI surface. No network or LLM calls.
"""

import json

import pytest
from fastapi.testclient import TestClient

import agent
from agent import (
    DIMENSIONS,
    SEVERITY,
    _assemble_review,
    _compute_health,
    _cost_estimate,
    _decide_verdict,
    _extract_json,
    _grade,
    _normalize_scores,
)
from github import compute_signals, extract_issue_refs, parse_pr_url
from main import app

# --- URL parsing -----------------------------------------------------------

def test_parse_pr_url_accepts_pull_url():
    assert parse_pr_url("https://github.com/fastapi/fastapi/pull/123") == (
        "fastapi", "fastapi", "123",
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/fastapi/fastapi/issues/123",
        "https://evil.example.com/fastapi/fastapi/pull/123",
        "https://github.com/fastapi/fastapi/pull/not-a-number",
        "file:///etc/passwd",
        "not a url",
    ],
)
def test_parse_pr_url_rejects_invalid(url):
    with pytest.raises(ValueError):
        parse_pr_url(url)


# --- issue-ref extraction ----------------------------------------------------

def test_extract_issue_refs_closing_and_mentions():
    closing, mentioned = extract_issue_refs(
        "This fixes #123 and relates to #456. Also closes https://github.com/o/r/issues/789"
    )
    assert closing == {123, 789}
    assert mentioned == {456}


def test_extract_issue_refs_empty_body():
    assert extract_issue_refs(None, "") == (set(), set())


# --- objective signals -------------------------------------------------------

def _mkfile(name, status="modified", add=10, rm=2, patch="@@\n+x = 1"):
    return {"filename": name, "status": status, "additions": add,
            "deletions": rm, "changes": add + rm, "patch": patch}


def test_compute_signals_classifies_tests_docs_deps():
    files = [
        _mkfile("src/app/main.py"),
        _mkfile("tests/test_main.py", status="added"),
        _mkfile("requirements.txt", patch="@@\n+requests==2.31.0"),
        _mkfile("docs/guide.md"),
    ]
    meta = {"changed_files": 4, "additions": 40, "deletions": 8, "commits": 2}
    s = compute_signals(files, meta, truncated=False)
    assert s["test_files"] == 1
    assert s["code_files"] == 1
    assert s["doc_files"] == 1
    assert s["new_dependencies"] == ["requests==2.31.0"]
    assert s["has_tests"] is True
    assert s["blast_radius"] == 4


def test_compute_signals_flags_missing_tests():
    s = compute_signals([_mkfile("src/x.py")], {"additions": 12, "deletions": 0}, False)
    assert any("no tests" in f.lower() for f in s["risk_flags"])


# --- scoring engine ----------------------------------------------------------

def test_dimension_weights_sum_to_one():
    assert round(sum(d["weight"] for d in DIMENSIONS), 6) == 1.0


def test_grade_bands():
    assert _grade(95) == "A"
    assert _grade(85) == "B"
    assert _grade(75) == "C"
    assert _grade(65) == "D"
    assert _grade(30) == "F"


def test_normalize_scores_clamps_and_defaults():
    raw = {"correctness": 150, "security": -5, "testing": "bad", "performance": None}
    scores = _normalize_scores(raw)
    assert scores["correctness"] == 100
    assert scores["security"] == 0
    assert scores["testing"] is None
    assert scores["performance"] is None
    assert set(scores) == {d["key"] for d in DIMENSIONS}


def _uniform_scores(v):
    return {d["key"]: v for d in DIMENSIONS}


def test_health_uncapped_when_no_issues():
    health, _weighted, cap = _compute_health(_uniform_scores(90), {})
    assert health == 90 and cap == 100


def test_blocker_caps_health():
    health, _, cap = _compute_health(_uniform_scores(95), {"blocker": 1})
    assert cap == SEVERITY["blocker"]["gate"] == 39
    assert health <= 39


def test_major_caps_health():
    health, _, cap = _compute_health(_uniform_scores(95), {"major": 2})
    assert cap == 84 and health == 84


def test_weight_redistribution_with_na_dimension():
    scores = _uniform_scores(80)
    scores["guidelines"] = None
    health, _, _ = _compute_health(scores, {})
    assert health == 80  # remaining dims all 80 -> still 80 after renormalizing


def test_verdicts():
    assert _decide_verdict(95, {})["verdict"] == "APPROVE"
    assert _decide_verdict(85, {"minor": 1})["verdict"] == "APPROVE_WITH_NITS"
    assert _decide_verdict(80, {"major": 1})["verdict"] == "REQUEST_CHANGES"
    assert _decide_verdict(30, {"blocker": 1})["verdict"] == "BLOCK"
    assert _decide_verdict(45, {})["verdict"] == "BLOCK"


def test_assemble_review_forces_guidelines_na_without_contributing():
    parsed = {"summary": "s", "scores": _uniform_scores(90), "issues": []}
    review = _assemble_review(parsed, {"contributing": {"found": False}})
    gdim = next(d for d in review["scorecard"] if d["key"] == "guidelines")
    assert gdim["score"] is None and gdim["grade"] == "N/A"


def test_assemble_review_sorts_issues_by_severity():
    parsed = {
        "summary": "s",
        "scores": _uniform_scores(90),
        "issues": [
            {"severity": "minor", "file": "a", "comment": "c"},
            {"severity": "blocker", "file": "b", "comment": "c"},
            {"severity": "weird", "file": "c", "comment": "c"},  # -> minor
        ],
    }
    review = _assemble_review(parsed, {"contributing": {"found": True}})
    assert [i["severity"] for i in review["issues"]] == ["blocker", "minor", "minor"]
    assert review["verdict"] == "BLOCK"


def test_cost_estimate_floors_savings():
    cost = _cost_estimate(1400, 300, "gpt-5.6-terra")
    assert cost["available"] is True
    assert 0 < cost["usd"] < 0.01
    assert cost["savings_pct"] < 100.0
    assert cost["claude_price_usd"] == agent.CLAUDE_REVIEW_PRICE


# --- JSON extraction ---------------------------------------------------------

def test_extract_json_plain_and_fenced():
    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('noise {"a": 1} trailing') == {"a": 1}
    assert _extract_json("not json at all") is None


# --- FastAPI surface ---------------------------------------------------------

client = TestClient(app)


def test_root_serves_ui():
    r = client.get("/")
    assert r.status_code == 200
    assert "PR" in r.text


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert "openai_api_key_configured" in r.json()


def test_review_rejects_invalid_url():
    r = client.post("/review", json={"pr_url": "https://example.com/nope"})
    assert r.status_code == 400


# --- diff snippets -------------------------------------------------------------

SAMPLE_PATCH = (
    "@@ -1,4 +1,8 @@\n"
    " import os\n"
    "+import sys\n"
    "+def new_fn():\n"
    "+    return 42\n"
    " x = 1\n"
    "@@ -10,2 +14,3 @@\n"
    " y = 2\n"
    "+z = 3\n"
    " w = 4"
)


def test_snippet_targets_line_in_second_hunk():
    snippet = agent._snippet_for(SAMPLE_PATCH, 15)
    assert "+z = 3" in snippet


def test_snippet_without_line_returns_head():
    snippet = agent._snippet_for(SAMPLE_PATCH, None)
    assert snippet.startswith("@@ -1,4 +1,8 @@")


def test_snippet_handles_missing_patch():
    assert agent._snippet_for("", 3) is None
    assert agent._snippet_for(None, 3) is None


def test_assemble_review_attaches_snippets():
    parsed = {
        "summary": "s",
        "scores": _uniform_scores(90),
        "issues": [{"severity": "minor", "file": "app.py", "line": 3, "comment": "c"}],
    }
    pr = {"contributing": {"found": True}, "patches": {"app.py": SAMPLE_PATCH}}
    review = _assemble_review(parsed, pr)
    assert "+def new_fn():" in review["issues"][0]["snippet"]


def test_review_model_whitelist():
    assert agent.DEFAULT_MODEL == "gpt-5.6-terra"
    assert agent.ALLOWED_MODELS == ["gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-sol"]


def test_models_endpoint():
    r = client.get("/models")
    assert r.status_code == 200
    assert r.json()["default"] in r.json()["models"]


@pytest.mark.asyncio
async def test_review_requires_openai_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = await agent.review_pr({})
    assert result == {"error": "OPENAI_API_KEY is not set in .env"}


@pytest.mark.asyncio
async def test_provider_error_does_not_leak_raw_exception(monkeypatch):
    class FailingResponses:
        async def create(self, **kwargs):
            import httpx
            import openai

            _req = httpx.Request("POST", "https://api.openai.com/v1/responses")
            raise openai.APIError(
                "upstream leaked sk-sensitive-value", request=_req, body={}
            )

    class FailingClient:
        def __init__(self, **kwargs):
            self.responses = FailingResponses()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(agent, "AsyncOpenAI", FailingClient)
    result = await agent.review_pr({})
    assert result == {"error": "OpenAI request failed. Check API configuration and try again."}
    assert "sensitive" not in result["error"]


@pytest.mark.asyncio
async def test_api_error_is_caught_not_nameerror(monkeypatch):
    """Regression: except openai.APIError must not raise NameError (agent.py
    imports APIError from openai; previously only AsyncOpenAI was imported).
    """
    import httpx
    import openai

    req = httpx.Request("POST", "https://api.openai.com/v1/responses")

    class FailingResponses:
        async def create(self, **kwargs):
            raise openai.APIError("upstream provider failure", request=req, body={})

    class FailingClient:
        def __init__(self, **kwargs):
            self.responses = FailingResponses()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(agent, "AsyncOpenAI", FailingClient)
    result = await agent.review_pr({})
    assert result == {"error": "OpenAI request failed. Check API configuration and try again."}
    assert "sensitive" not in result["error"]


def test_unknown_model_cost_is_na():
    cost = _cost_estimate(1400, 300, "custom-provider-model")
    assert cost["available"] is False
    assert cost["usd"] is None
    assert cost["savings_pct"] is None


def test_extra_models_appended(monkeypatch):
    monkeypatch.setenv("EXTRA_MODELS", "llama-3.1-8b, qwen-2.5-coder")
    assert agent._allowed_models() == [
        *agent.BASE_ALLOWED_MODELS,
        "llama-3.1-8b",
        "qwen-2.5-coder",
    ]


def test_openai_client_uses_base_url(monkeypatch):
    captured = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openrouter.ai/api/v1")
    monkeypatch.setattr(agent, "AsyncOpenAI", fake_client)
    agent._openai_client()
    assert captured.get("base_url") == "https://api.openrouter.ai/api/v1"


def test_openai_client_defaults_when_no_base_url(monkeypatch):
    captured = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(agent, "AsyncOpenAI", fake_client)
    agent._openai_client()
    assert "base_url" not in captured


@pytest.mark.asyncio
async def test_review_falls_back_to_chat_completions(monkeypatch):
    import httpx
    import openai

    class FailingResponses:
        async def create(self, **kwargs):
            req = httpx.Request("POST", "https://api.provider.example/v1/responses")
            resp = httpx.Response(404, request=req)
            raise openai.APIStatusError("Not Found", response=resp, body={})

    class Msg:
        content = json.dumps({"summary": "ok", "scores": {}, "issues": []})

    class Choice:
        message = Msg()

    class Resp:
        def __init__(self):
            self.choices = [Choice()]

    class ChatCompletions:
        async def create(self, **kwargs):
            return Resp()

    class FakeChat:
        completions = ChatCompletions()

    class FakeClient:
        def __init__(self, **kwargs):
            self.responses = FailingResponses()
            self.chat = FakeChat()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(agent, "AsyncOpenAI", FakeClient)
    result = await agent.review_pr({})
    assert "error" not in result
    assert result["summary"] == "ok"
