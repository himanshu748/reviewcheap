"""Tests for the GitHub API TTL cache and conditional (ETag) revalidation.

The cache is the rate-limit mitigation: repeat GETs within the TTL are served
from memory, and once stale they are revalidated with If-None-Match so a 304
refreshes the entry without counting against the API limit.
"""

import base64
import json

import pytest

import github
from github import fetch_pr_data

CONTRIBUTING_BLOB = {
    "content": base64.b64encode(b"Follow the contribution guidelines.").decode(),
    "encoding": "base64",
    "path": "CONTRIBUTING.md",
    "html_url": "https://github.com/o/r/blob/main/CONTRIBUTING.md",
}
CONTRIBUTING_URL = "https://api.github.com/repos/o/r/contents/CONTRIBUTING.md"


def _pr_payload():
    return {
        "title": "Fix bug",
        "body": "No linked issue refs here.",
        "user": {"login": "alice"},
        "base": {"ref": "main"},
        "head": {"ref": "feature"},
        "commits": 2,
        "changed_files": 1,
        "additions": 10,
        "deletions": 2,
    }


FILES_PAYLOAD = [
    {"filename": "a.txt", "status": "modified", "additions": 5,
     "deletions": 1, "changes": 6, "patch": "@@ -1 +1 @@\n+x"},
]


class FakeResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.content = json.dumps(payload).encode()

    @property
    def text(self):
        return json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeAsyncClient:
    """Simulates the GitHub API for a full fetch_pr_data run."""

    def __init__(self, *args, **kwargs):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, *, headers=None, params=None):
        self.calls.append((url, dict(params or {})))
        if "community/profile" in url:
            return FakeResponse(200, {
                "files": {"contributing": {
                    "url": CONTRIBUTING_URL,
                    "html_url": CONTRIBUTING_BLOB["html_url"],
                }},
            })
        if url == CONTRIBUTING_URL:
            return FakeResponse(200, CONTRIBUTING_BLOB)
        if url.endswith("/files"):
            return FakeResponse(200, FILES_PAYLOAD)
        return FakeResponse(200, _pr_payload())


@pytest.mark.asyncio
async def test_second_identical_review_makes_no_uncached_calls(monkeypatch):
    github.GIT_CACHE.clear()
    fake = FakeAsyncClient()
    monkeypatch.setattr(github.httpx, "AsyncClient", lambda *a, **k: fake)

    await fetch_pr_data("https://github.com/o/r/pull/1")
    first_count = len(fake.calls)
    assert first_count > 0

    # The module-level cache is shared, so an identical re-review triggers no
    # new network calls within the TTL window.
    await fetch_pr_data("https://github.com/o/r/pull/1")
    assert len(fake.calls) == first_count


class FakeAsyncClientNoContributing(FakeAsyncClient):
    """Community profile finds no CONTRIBUTING; fallback probes 404 for each."""

    async def get(self, url, *, headers=None, params=None):
        self.calls.append((url, dict(params or {})))
        if "community/profile" in url:
            return FakeResponse(200, {"files": {}})
        if "/contents/" in url:
            return FakeResponse(404, {"message": "Not Found"})
        if url.endswith("/files"):
            return FakeResponse(200, FILES_PAYLOAD)
        return FakeResponse(200, _pr_payload())


@pytest.mark.asyncio
async def test_second_review_caches_missing_contributing_lookups(monkeypatch):
    github.GIT_CACHE.clear()
    fake = FakeAsyncClientNoContributing()
    monkeypatch.setattr(github.httpx, "AsyncClient", lambda *a, **k: fake)

    await fetch_pr_data("https://github.com/o/r/pull/1")
    first_count = len(fake.calls)
    assert first_count == 10  # PR + files + profile + 7 candidate probes

    await fetch_pr_data("https://github.com/o/r/pull/1")
    assert len(fake.calls) == first_count


class _RevalidatingClient:
    def __init__(self):
        self.etag = '"v1"'
        self.conditional = 0

    async def get(self, url, *, headers=None, params=None):
        if headers and headers.get("If-None-Match"):
            self.conditional += 1
            return FakeResponse(304, {}, headers={"etag": self.etag})
        return FakeResponse(200, {"ok": True}, headers={"etag": self.etag})


@pytest.mark.asyncio
async def test_cache_revalidates_via_conditional_request_on_ttl_expiry():
    github.GIT_CACHE.clear()
    inner = _RevalidatingClient()
    wrapper = github._CachingClient(inner, github.GIT_CACHE, ttl=0)

    first = await wrapper.get("https://api.github.com/x")
    assert first.status_code == 200 and first.json() == {"ok": True}

    # ttl=0 forces staleness, so the second call sends If-None-Match and the
    # resulting 304 serves the cached body without a fresh 200 payload.
    second = await wrapper.get("https://api.github.com/x")
    assert inner.conditional == 1
    assert second.status_code == 200
    assert second.json() == {"ok": True}
