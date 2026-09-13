# CHANGELOG — Medic Model Hub

## v1.2.2 (2026-09-12)

Kills the Perplexity per-call web-search fee by making paid search opt-in
and grounding Sonar with the hub's own free search. `.env` handling
untouched (builds never overwrite it); the hub's `/v1/chat/completions`
shape is unchanged.

- **Paid Sonar search is now opt-in.** v1.2.1 attached Perplexity's
  `web_search` tool to every Sonar call (billed per call). The adapter now
  sends no `tools` by default. Set `SONAR_WEB_SEARCH=1` (or pass
  `paid_search=True` to the adapter) to restore Perplexity's own search
  tool for anyone who wants it.
- **New: POST /v1/web_search** (`server/websearch.py`, stdlib only).
  Hub-native free web search returning
  `{"query", "source", "results": [{"title", "url", "snippet"}]}`.
  Uses the Brave Search API free tier (2,000 queries/month, no card) when
  `BRAVE_SEARCH_API_KEY` is set; otherwise falls back to DuckDuckGo's
  keyless Instant Answer API. Honest `medic-model-hub/x.y.z` User-Agent on
  all outbound calls.
- **Sonar grounding is free by default.** Sonar calls now prepend the top
  free `web_search` results as context into the Agent API `input`
  (Perplexity model + hub search = zero Perplexity search fee). If search
  fails or returns nothing the call proceeds ungrounded rather than
  failing. Sonar token costs remain — that's the model call itself.
- `/health` now also reports `"web_search": "brave" | "duckduckgo"`.
- New env vars (`.env.example`): `BRAVE_SEARCH_API_KEY` (empty = DDG
  fallback) and `SONAR_WEB_SEARCH` (default 0).
- Tests: 118 checks (was 87) — Sonar no-tools-by-default, opt-in paths
  (env + explicit param), grounding prepend + ungrounded fallback,
  `/v1/web_search` HTTP shape and error paths, Brave/DDG request shapes
  and response parsing with stubbed HTTP. No network, no keys.

## v1.2.1 (2026-09-12)

Fixes the two blockers the laptop agent hit on the v1.2.0 live install.
No new env vars; `.env` handling untouched (builds never overwrite it).
The hub's own `/v1/chat/completions` and `/v1/qpu/*` interfaces are unchanged.

- **Sonar → Perplexity Agent API.** Perplexity deprecated `/chat/completions`
  for Sonar; the adapter now posts to `https://api.perplexity.ai/v1/responses`
  with the Responses shape (`model`, `input`, `instructions`, `temperature`,
  `max_output_tokens`, `tools: [{web_search}]`, `tool_choice: auto`) and
  parses `output[].content[]` (`output_text` parts) plus `usage.input_tokens` /
  `usage.output_tokens`. `web_search` is attached so Sonar keeps the grounded
  search behavior the legacy endpoint had by default. Model aliases
  (`sonar`, `sonar-pro`) and the hub's OpenAI-compatible request shape are
  unchanged.
- **QPU User-Agent.** Every outbound IBM Quantum HTTP call (Runtime API via
  `_http`, IAM token exchange via `_bearer`) now sends
  `User-Agent: medic-model-hub/1.2.1`. urllib's default `Python-urllib/3.x`
  UA was tripping Cloudflare's bot check (HTTP 403 on reads despite a valid
  IAM token). The UA is an honest client identifier, bumped with the hub
  version (`qpu.HUB_VERSION`).
- Tests: 87 checks (was 73) — new coverage for the Agent API request shape
  and response parsing, and for the User-Agent on all QPU outbound calls
  (Runtime + IAM). No network, no keys.

## v1.2.0 (2026-09-12)

- QPU gateway: GET/POST `/v1/qpu/*` (backends, usage, job submit/poll/
  results/cancel) with hardware safety gates (backend allowlist, shot cap,
  live quota check before submit, append-only ledger).
- 73/73 tests.

## v1.1.0 (2026-09-12)

- Perplexity Sonar chat adapter; `/v1/embeddings` (OpenAI).

## v1.0.0 (2026-09-12)

- Initial release: one OpenAI-compatible endpoint routing to GPT, Claude,
  Gemini. 25/25 tests.
