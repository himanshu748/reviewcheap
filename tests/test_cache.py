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


class AuthRecordingClient:
    """Returns a different payload per Authorization header; records every call."""

    def __init__(self, bodies):
        self.bodies = bodies  # {auth_header_value_or_None: payload}
        self.calls = []

    async def get(self, url, *, headers=None, params=None):
        self.calls.append((url, dict(headers or {})))
        auth = (headers or {}).get("Authorization")
        return FakeResponse(200, self.bodies[auth])


@pytest.mark.asyncio
async def test_cache_isolated_per_auth_identity():
    github.GIT_CACHE.clear()
    inner = AuthRecordingClient({
        "Bearer token-a": {"which": "a"},
        "Bearer token-b": {"which": "b"},
        None: {"which": "anon"},
    })
    wrapper = github._CachingClient(inner, github.GIT_CACHE)
    url = "https://api.github.com/repos/private/r/pulls/1"

    a1 = await wrapper.get(url, headers={"Authorization": "Bearer token-a"})
    assert a1.json() == {"which": "a"}
    assert len(inner.calls) == 1

    # Same authentication again -> cache hit, no network call.
    a2 = await wrapper.get(url, headers={"Authorization": "Bearer token-a"})
    assert a2.json() == {"which": "a"}
    assert len(inner.calls) == 1

    # Different token -> real request; must not receive A's cached body.
    b = await wrapper.get(url, headers={"Authorization": "Bearer token-b"})
    assert b.json() == {"which": "b"}
    assert len(inner.calls) == 2

    # Anonymous -> real request; must not receive A's or B's cached body.
    anon = await wrapper.get(url)
    assert anon.json() == {"which": "anon"}
    assert len(inner.calls) == 3

    # Cache keys never contain raw tokens (fingerprints only).
    for key in github.GIT_CACHE:
        assert "token-a" not in repr(key)
        assert "token-b" not in repr(key)


class StaleTokenClient:
    def __init__(self):
        self.calls = []

    async def get(self, url, *, headers=None, params=None):
        auth = (headers or {}).get("Authorization")
        self.calls.append(auth)
        if auth == "Bearer valid-a":
            return FakeResponse(200, {"which": "a"})
        if auth == "Bearer stale":
            return FakeResponse(401, {"message": "Bad credentials"})
        return FakeResponse(200, {"which": "anon"})


@pytest.mark.asyncio
async def test_401_retry_cannot_reuse_privileged_entry():
    github.GIT_CACHE.clear()
    inner = StaleTokenClient()
    wrapper = github._CachingClient(inner, github.GIT_CACHE)
    url = "https://api.github.com/repos/private/r/pulls/1"

    # Private review with a valid token populates a privileged entry.
    priv = await wrapper.get(url, headers={"Authorization": "Bearer valid-a"})
    assert priv.json() == {"which": "a"}

    # Stale env token -> 401 (never cached); the unauthenticated retry goes to
    # the transport under the anonymous identity and cannot read the privileged body.
    stale = await wrapper.get(url, headers={"Authorization": "Bearer stale"})
    assert stale.status_code == 401
    anon = await wrapper.get(url)
    assert anon.json() == {"which": "anon"}
    assert inner.calls == ["Bearer valid-a", "Bearer stale", None]


class NotFoundClient:
    def __init__(self):
        self.calls = 0

    async def get(self, url, *, headers=None, params=None):
        self.calls += 1
        if (headers or {}).get("Authorization") == "Bearer token-a":
            return FakeResponse(404, {"message": "Not Found"})
        return FakeResponse(200, {"which": "anon"})


@pytest.mark.asyncio
async def test_cached_404_not_shared_with_anonymous():
    github.GIT_CACHE.clear()
    inner = NotFoundClient()
    wrapper = github._CachingClient(inner, github.GIT_CACHE)
    url = "https://api.github.com/repos/private/r/pulls/9"

    first = await wrapper.get(url, headers={"Authorization": "Bearer token-a"})
    assert first.status_code == 404
    assert inner.calls == 1

    # Anonymous must not receive the cached privileged 404; it hits the
    # transport and gets its own response.
    second = await wrapper.get(url)
    assert second.status_code == 200
    assert inner.calls == 2


class FakeAsyncClientAuth(FakeAsyncClient):
    """Full fetch_pr_data client that marks authenticated vs anonymous payloads."""

    async def get(self, url, *, headers=None, params=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
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
        payload = _pr_payload()
        payload["title"] = "PRIVATE" if (headers or {}).get("Authorization") else "PUBLIC"
        return FakeResponse(200, payload)


@pytest.mark.asyncio
async def test_fetch_isolates_cache_per_credential(monkeypatch):
    github.GIT_CACHE.clear()
    fake = FakeAsyncClientAuth()
    monkeypatch.setattr(github.httpx, "AsyncClient", lambda *a, **k: fake)
    url = "https://github.com/o/r/pull/1"

    # Authenticated review populates the cache under the token's identity.
    monkeypatch.setenv("GITHUB_TOKEN", "token-a")
    authed = await fetch_pr_data(url)
    assert authed["title"] == "PRIVATE"
    authed_count = len(fake.calls)

    # Same credential again -> cache hits, no network calls.
    again = await fetch_pr_data(url)
    assert again["title"] == "PRIVATE"
    assert len(fake.calls) == authed_count

    # Anonymous review -> every request goes to GitHub; nothing reused.
    monkeypatch.delenv("GITHUB_TOKEN")
    anon = await fetch_pr_data(url)
    assert anon["title"] == "PUBLIC"
    assert len(fake.calls) > authed_count


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
