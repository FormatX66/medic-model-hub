# Medic Model Hub v1.1.0

One OpenAI-compatible API for every model. Any model, script, or agent on
this machine talks to the hub; the hub routes to GPT, Claude, Gemini, or
Sonar behind the scenes — plus an embeddings endpoint for semantic search.

## Install (Windows)

1. Extract this zip to `C:\Users\bruce\MedicHub\hub` (next to `MedicBridge`).
2. Copy `.env.example` to `.env` and paste your API keys (only the backends
   you configure are enabled). **This `.env` is yours — future bundles never
   overwrite it.**
3. `docker compose up -d --build`
4. Check `http://localhost:8090/health`

## API

**POST** `http://localhost:8090/v1/chat/completions` — OpenAI chat-completions shape:

```json
{
  "model": "claude",
  "messages": [
    {"role": "system", "content": "Be concise."},
    {"role": "user", "content": "Hello"}
  ],
  "max_tokens": 500,
  "temperature": 0.7
}
```

Response is the standard OpenAI completion object (`choices[0].message.content`,
`usage`, `finish_reason`).

**Model names** (aliases work too):

| name | backend | native model |
|---|---|---|
| `gpt` / `gpt-4o-mini` | OpenAI | gpt-4o-mini |
| `claude` / `claude-haiku-4-5-20251001` | Anthropic | claude-haiku-4-5-20251001 |
| `gemini` / `gemini-flash-latest` | Google | gemini-flash-latest |
| `sonar` / `sonar-pro` | Perplexity | sonar / sonar-pro (live web search) |

Unknown or unconfigured models return 400 with the list of what's available.

**POST** `http://localhost:8090/v1/embeddings` — OpenAI embeddings shape:

```json
{
  "model": "embed",
  "input": "text to embed (or an array of strings)"
}
```

Response is the standard OpenAI embeddings object (`data[].embedding`,
`usage`). Embedding models:

| name | backend | native model |
|---|---|---|
| `embed` / `text-embedding-3-small` | OpenAI | text-embedding-3-small |
| `text-embedding-3-large` | OpenAI | text-embedding-3-large |

**GET** `/v1/models` — list enabled models.
**GET** `/health` — `{"ok": true, "backends": {...}}`.

## Notes

- Binds to 127.0.0.1 only — local machine, no auth needed. Don't expose it
  to a network without adding some.
- `stream: true` is rejected in v1; non-streaming only.
- Keys are never logged. Provider errors are sanitized before returning.
- Tests: `python3 tests/test_hub.py` (41 checks, no network or keys needed).
- The hub is a separate service from the Medic Bridge relay; they run side
  by side and don't depend on each other.

## Calling it from anything

curl example:

```bash
curl http://localhost:8090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini","messages":[{"role":"user","content":"Say hi"}]}'
```

Any tool that speaks OpenAI's API can point at `http://localhost:8090/v1`
as its base URL.
