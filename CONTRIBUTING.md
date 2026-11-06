# Contributing to ReviewCheap

Thanks for helping. ReviewCheap grades pull requests against a project's own CONTRIBUTING.md, so it's only fair that this one is short and followed.

## Before you start

1. Find or open an issue. Comment on it to be assigned before you write code, so two people don't build the same thing.
2. Issues labelled `good first issue` are scoped for a first PR. `help wanted` issues are larger.
3. For a big change (new review dimension, new provider, UI rewrite), describe the approach in the issue first.

## Local setup

```bash
git clone https://github.com/<you>/reviewcheap.git
cd reviewcheap
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add OPENAI_API_KEY; GITHUB_TOKEN is optional
uvicorn main:app --reload --port 8000
```

## Tests

```bash
python -m pytest
python -m compileall agent.py github.py main.py mcp_server.py tests
```

Tests must not call OpenAI or GitHub over the network. Mock `AsyncOpenAI` and `httpx` as the existing tests do.

## Pull requests

- One logical change per PR. Link the issue with `Fixes #N`.
- Add or update tests for behaviour you change. CI must be green.
- Keep the health score deterministic: the model scores dimensions, Python computes the composite, caps and verdict. Don't move that logic into the prompt.
- Never log or return token values (OpenAI, GitHub) in errors, responses or the UI. Keep upstream provider errors generic at the API boundary.
- Keep inputs bounded. Diffs and prompts must stay within the existing size limits.
- Use clear commit messages in the imperative mood ("Add Anthropic provider", not "added stuff").

## Code style

Plain, typed Python 3.10+. Prefer small functions and explicit error handling over bare `except Exception`.

## Code of Conduct

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
