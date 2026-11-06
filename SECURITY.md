# Security Policy

## Reporting a vulnerability

Please don't open a public issue for security problems. Use GitHub's private reporting instead: **Security → Report a vulnerability** on this repository.

Include what you found, steps to reproduce, and the impact. You'll get an acknowledgement within 7 days.

## Scope

Especially relevant: anything that leaks the server's `OPENAI_API_KEY` or a user-supplied GitHub token, prompt injection from PR content that changes the verdict outside the deterministic scoring, or server-side request forgery through the PR URL.
