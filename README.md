# Medic Model Hub v1.2.0

One API for every model — and now, one API for the QPU too. Any model,
script, or agent on this machine talks to the hub; the hub routes to GPT,
Claude, Gemini, or Sonar behind the scenes, serves embeddings for semantic
search, and gateways IBM Quantum hardware jobs through `/v1/qpu/*`.

House rule: every provider API we integrate gets a hub adapter — the hub
stays the single gateway on this machine. See "Adding a provider" below.

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
**GET** `/health` — `{"ok": true, "backends": {...}}` (includes `"qpu"`).

## QPU gateway (`/v1/qpu/*`)

IBM Quantum hardware through the hub. Set `IBM_QUANTUM_API_KEY` (and
`IBM_QUANTUM_CRN`) in `.env`; without them every `/v1/qpu/*` call returns
400. The hub exchanges the key for a short-lived IAM token itself.

| endpoint | what it does |
|---|---|
| `GET /v1/qpu/backends` | backends: name, qubits, operational, pending jobs |
| `GET /v1/qpu/usage` | live quota: consumed / remaining / limit seconds |
| `POST /v1/qpu/jobs` | submit a Sampler job: `{"backend", "shots", "params"}` |
| `GET /v1/qpu/jobs/{id}` | job status |
| `GET /v1/qpu/jobs/{id}/results` | raw results (counts) |
| `POST /v1/qpu/jobs/{id}/cancel` | cancel a queued/running job |

`params` is an array of SamplerV2 PUBs — serialize circuits with your own
qiskit and hand the hub the params; the hub normalizes every PUB's `shots`
to the validated `shots` value.

Hardware safety gates (all checked before anything touches the QPU):

- backend must be in the `QPU_BACKENDS` allowlist
  (default: `ibm_kingston,ibm_fez,ibm_marrakesh`)
- `1 <= shots <= QPU_MAX_SHOTS` (default 1024)
- live quota check first: refuses when IBM reports the usage limit
  reached or no time remaining
- every submission is appended to `data/qpu_ledger.json` (audit trail,
  survives rebuilds via the `./data` volume)

QPU output is evidence, not authority: results come back raw, the hub never
interprets them.

```bash
curl http://localhost:8090/v1/qpu/backends
curl -X POST http://localhost:8090/v1/qpu/jobs \
  -H "Content-Type: application/json" \
  -d '{"backend":"ibm_kingston","shots":256,"params":[{...pubs...}]}'
```

## Adding a provider

New provider API → new hub adapter, not a one-off client. Pattern:

1. New module in `server/` (stdlib only) owning the provider's auth,
   request translation, and any safety gates.
2. Route(s) wired in `hub.py` (`/v1/...`), advertised only when its key
   is set in `.env`.
3. Key + tuning knobs in `.env.example` (never overwrite a live `.env`).
4. Tests in `tests/test_hub.py` that stub the provider's HTTP boundary —
   no network, no keys.
5. README section + version bump.

## Notes

- Binds to 127.0.0.1 only — local machine, no auth needed. Don't expose it
  to a network without adding some.
- `stream: true` is rejected in v1; non-streaming only.
- Keys are never logged. Provider errors are sanitized before returning.
- Tests: `python3 tests/test_hub.py` (73 checks, no network or keys needed).
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
