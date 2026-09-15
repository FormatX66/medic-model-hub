#!/usr/bin/env python3
"""Medic Model Hub v1.2.3 — one OpenAI-compatible API for every model.

POST /v1/chat/completions with {"model": ..., "messages": [...]} and the hub
routes to the right provider behind the scenes. Keys live in one local .env
that builds never touch.

Chat backends: openai, anthropic, google (gemini), perplexity (sonar, via
Perplexity's Agent API). POST /v1/embeddings routes to OpenAI's embedding
models for semantic search. GET/POST /v1/qpu/* is the IBM Quantum gateway
(backends, usage, job submit/poll/results/cancel) with hardware safety gates;
`/v1/azure/*` (Azure Quantum) and `/v1/google/*` (Google Quantum Engine)
mirror it provider-for-provider. Provider selection is explicit per URL —
requests never default or fall through to another provider's hardware.
POST /v1/web_search is the hub's own free web search (Brave free tier, or
keyless DuckDuckGo fallback) — Sonar uses it for grounding by default so no
Perplexity per-call search fee is incurred. Only configured backends are
advertised. stdlib only — no pip dependencies.

House rule: every provider API we integrate gets a hub adapter — the hub
stays the single gateway on this machine.
"""
import json
import os
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

import qpu
import azure_quantum
import google_quantum
import websearch

VERSION = "1.2.4"
PORT = int(os.environ.get("PORT", "8090"))

# ---------------------------------------------------------------- config ---
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
PERPLEXITY_KEY = os.environ.get("PERPLEXITY_API_KEY", "").strip()

# model name (or alias) -> (backend, provider-native model id)
ROUTES = {
    "gpt": ("openai", "gpt-4o-mini"),
    "gpt-4o-mini": ("openai", "gpt-4o-mini"),
    "claude": ("anthropic", "claude-haiku-4-5-20251001"),
    "claude-haiku-4-5-20251001": ("anthropic", "claude-haiku-4-5-20251001"),
    "gemini": ("google", "gemini-flash-latest"),
    "gemini-flash-latest": ("google", "gemini-flash-latest"),
    "sonar": ("perplexity", "sonar"),
    "sonar-pro": ("perplexity", "sonar-pro"),
}

# embedding model name (or alias) -> (backend, provider-native model id)
EMBED_ROUTES = {
    "embed": ("openai", "text-embedding-3-small"),
    "text-embedding-3-small": ("openai", "text-embedding-3-small"),
    "text-embedding-3-large": ("openai", "text-embedding-3-large"),
}

BACKEND_KEYS = {"openai": OPENAI_KEY, "anthropic": ANTHROPIC_KEY,
                "google": GEMINI_KEY, "perplexity": PERPLEXITY_KEY}


def configured_backends():
    return {b: bool(k) for b, k in BACKEND_KEYS.items()}


def available_models():
    return [m for m, (b, _) in ROUTES.items() if BACKEND_KEYS[b]]


def available_embedding_models():
    return [m for m, (b, _) in EMBED_ROUTES.items() if BACKEND_KEYS[b]]


# ------------------------------------------------------------- adapters ---
def _post(url, payload, headers, timeout=120):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {"message": f"provider HTTP {e.code}"}
        return e.code, {"provider_error": body}
    except Exception as e:  # network / timeout
        return 0, {"provider_error": {"message": f"backend unreachable: {type(e).__name__}"}}


def split_messages(messages):
    """Pull out system prompt; return (system_text_or_None, chat_messages)."""
    system_parts, chat = [], []
    for m in messages:
        if m.get("role") == "system":
            c = m.get("content")
            system_parts.append(c if isinstance(c, str) else json.dumps(c))
        else:
            chat.append({"role": m.get("role", "user"), "content": m.get("content", "")})
    return ("\n".join(system_parts) or None, chat)


def _openai_shaped_complete(url, key, model, system, chat, max_tokens, temperature):
    """Shared translation for OpenAI-shaped chat APIs (OpenAI)."""
    msgs = ([{"role": "system", "content": system}] if system else []) + chat
    payload = {"model": model, "messages": msgs, "temperature": temperature}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    status, body = _post(url, payload, {"Authorization": f"Bearer {key}"})
    if status != 200:
        return status, None, body
    ch = body["choices"][0]
    usage = body.get("usage", {})
    return 200, {
        "text": ch["message"].get("content") or "",
        "finish": ch.get("finish_reason") or "stop",
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
    }, None


def openai_complete(model, system, chat, max_tokens, temperature):
    return _openai_shaped_complete("https://api.openai.com/v1/chat/completions",
                                   OPENAI_KEY, model, system, chat, max_tokens, temperature)


def sonar_paid_search_opt_in():
    """True when the operator wants Perplexity's own (per-call-fee) search
    tool on Sonar: SONAR_WEB_SEARCH=1 in the environment."""
    return os.environ.get("SONAR_WEB_SEARCH", "0").strip() == "1"


def _grounding_message(chat):
    """Build a web-results context message from the last user turn, or None
    when search fails/returns nothing (chat then proceeds ungrounded)."""
    query = ""
    for m in reversed(chat):
        if m.get("role") == "user":
            c = m.get("content")
            query = c if isinstance(c, str) else ""
            break
    if not query.strip():
        return None
    status, results, _err = websearch.web_search(query, count=5)
    if status != 200 or not results:
        print(f"sonar grounding: web search unavailable (status {status}), "
              f"proceeding ungrounded", flush=True)
        return None
    lines = ["Current web search results — use them to ground your answer:"]
    for r in results:
        lines.append(f"- {r['title']}\n  {r['url']}\n  {r['snippet']}")
    return {"role": "user", "content": "\n".join(lines)}


def perplexity_complete(model, system, chat, max_tokens, temperature,
                        paid_search=None):
    """Sonar via Perplexity's Agent API (Responses shape).

    Perplexity deprecated /chat/completions for Sonar; the Agent API at
    /v1/responses takes `model`, `input` (message array), `instructions`
    (system prompt), `temperature`, `max_output_tokens`, and `tools`.

    Search behavior: by default NO Perplexity tools are attached (their
    `web_search` tool carries a per-call fee). Instead the hub grounds the
    call itself — top free web_search results are prepended as context.
    Set SONAR_WEB_SEARCH=1 (or pass paid_search=True) to restore
    Perplexity's own web_search tool and skip hub grounding.
    The hub's own /v1/chat/completions interface is unchanged.
    """
    opt_in = paid_search if paid_search is not None else sonar_paid_search_opt_in()
    payload = {"model": model, "temperature": temperature}
    if opt_in:
        payload["input"] = chat
        payload["tools"] = [{"type": "web_search"}]
        payload["tool_choice"] = "auto"
    else:
        ground = _grounding_message(chat)
        payload["input"] = ([ground] + list(chat)) if ground else chat
    if system:
        payload["instructions"] = system
    if max_tokens:
        payload["max_output_tokens"] = max_tokens
    status, body = _post("https://api.perplexity.ai/v1/responses", payload,
                         {"Authorization": f"Bearer {PERPLEXITY_KEY}"})
    if status != 200:
        return status, None, body
    text = ""
    for item in body.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    text += part.get("text", "")
    usage = body.get("usage", {})
    return 200, {
        "text": text,
        "finish": "stop",
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
    }, None


def anthropic_complete(model, system, chat, max_tokens, temperature):
    payload = {"model": model, "max_tokens": max_tokens or 1024,
               "messages": chat, "temperature": temperature}
    if system:
        payload["system"] = system
    status, body = _post("https://api.anthropic.com/v1/messages", payload,
                         {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"})
    if status != 200:
        return status, None, body
    text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
    finish = {"end_turn": "stop", "max_tokens": "length",
              "stop_sequence": "stop"}.get(body.get("stop_reason"), "stop")
    usage = body.get("usage", {})
    return 200, {
        "text": text,
        "finish": finish,
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
    }, None


def gemini_complete(model, system, chat, max_tokens, temperature):
    contents = []
    for m in chat:
        role = "model" if m["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})
    payload = {"contents": contents,
               "generationConfig": {"temperature": temperature}}
    if max_tokens:
        payload["generationConfig"]["maxOutputTokens"] = max_tokens
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    status, body = _post(url, payload, {"x-goog-api-key": GEMINI_KEY})
    if status != 200:
        return status, None, body
    cands = body.get("candidates", [])
    text = ""
    finish = "stop"
    if cands:
        parts = (cands[0].get("content") or {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        finish = {"STOP": "stop", "MAX_TOKENS": "length"}.get(cands[0].get("finishReason"), "stop")
    um = body.get("usageMetadata", {})
    return 200, {
        "text": text,
        "finish": finish,
        "prompt_tokens": um.get("promptTokenCount", 0),
        "completion_tokens": um.get("candidatesTokenCount", 0),
    }, None


ADAPTERS = {"openai": openai_complete, "anthropic": anthropic_complete,
            "google": gemini_complete, "perplexity": perplexity_complete}


# ---------------------------------------------------- embedding adapter ---
def openai_embed(model, inputs):
    """inputs: str or list[str]. Returns (status, result, err)."""
    payload = {"model": model, "input": inputs}
    status, body = _post("https://api.openai.com/v1/embeddings", payload,
                         {"Authorization": f"Bearer {OPENAI_KEY}"})
    if status != 200:
        return status, None, body
    usage = body.get("usage", {})
    return 200, {
        "embeddings": [d["embedding"] for d in sorted(body.get("data", []),
                                                      key=lambda d: d.get("index", 0))],
        "prompt_tokens": usage.get("prompt_tokens", 0),
    }, None


EMBED_ADAPTERS = {"openai": openai_embed}


# ---------------------------------------------------------------- server ---
# provider root -> adapter module. Provider selection is explicit per
# request URL; there is no default provider and no cross-provider fallback.
QPU_PROVIDERS = {"qpu": qpu, "azure": azure_quantum, "google": google_quantum}


class Handler(BaseHTTPRequestHandler):
    server_version = f"medic-model-hub/{VERSION}"

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            n = 0
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return None

    def log_message(self, fmt, *args):
        # access log to stdout; never log bodies or keys
        print(f"{self.address_string()} {self.command} {self.path} ->", *args, flush=True)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            health = configured_backends()
            health["qpu"] = qpu.configured()
            health["azure_qpu"] = azure_quantum.configured()
            health["google_qpu"] = google_quantum.configured()
            health["web_search"] = websearch.source()  # 'brave' or 'duckduckgo'
            self._json(200, {"ok": True, "version": VERSION, "backends": health})
        elif path == "/v1/models":
            data = [{"id": m, "object": "model", "owned_by": b}
                    for m, (b, _) in ROUTES.items() if BACKEND_KEYS[b]]
            data += [{"id": m, "object": "model", "owned_by": b}
                     for m, (b, _) in EMBED_ROUTES.items() if BACKEND_KEYS[b]]
            self._json(200, {"object": "list", "data": data})
        elif path == "/v1/qpu" or path.startswith("/v1/qpu/") \
                or path == "/v1/azure" or path.startswith("/v1/azure/") \
                or path == "/v1/google" or path.startswith("/v1/google/"):
            segs = path.split("/")
            self._qpu("GET", segs[3:], None, segs[2])
        else:
            self._json(404, {"error": "not found"})

    def _chat_completions(self, body):
        model = body.get("model", "")
        messages = body.get("messages")
        if not model or not isinstance(messages, list) or not messages:
            self._json(400, {"error": "need 'model' and non-empty 'messages'"})
            return
        if body.get("stream"):
            self._json(400, {"error": "streaming not supported in v1; use stream:false"})
            return
        route = ROUTES.get(model)
        if not route or not BACKEND_KEYS[route[0]]:
            self._json(400, {"error": f"unknown or unconfigured model '{model}'",
                             "available": available_models()})
            return
        backend, native_model = route
        system, chat = split_messages(messages)
        max_tokens = body.get("max_tokens")
        temperature = body.get("temperature", 0.7)
        t0 = time.time()
        try:
            status, result, err = ADAPTERS[backend](native_model, system, chat, max_tokens, temperature)
        except Exception:
            self._json(502, {"error": f"backend '{backend}' failed"})
            return
        ms = int((time.time() - t0) * 1000)
        if status != 200:
            # sanitize: never forward anything that could carry a key
            detail = (err or {}).get("provider_error", "backend error")
            self._json(502, {"error": f"backend '{backend}' returned {status}", "detail": detail})
            return
        total = result["prompt_tokens"] + result["completion_tokens"]
        print(f"chat model={model} backend={backend} tokens={total} {ms}ms", flush=True)
        self._json(200, {
            "id": f"chatcmpl-hub-{int(t0 * 1000)}",
            "object": "chat.completion",
            "created": int(t0),
            "model": model,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": result["text"]},
                         "finish_reason": result["finish"]}],
            "usage": {"prompt_tokens": result["prompt_tokens"],
                      "completion_tokens": result["completion_tokens"],
                      "total_tokens": total},
        })

    def _embeddings(self, body):
        model = body.get("model", "")
        inputs = body.get("input")
        valid_input = isinstance(inputs, str) or (
            isinstance(inputs, list) and inputs
            and all(isinstance(x, str) for x in inputs))
        if not model or not valid_input:
            self._json(400, {"error": "need 'model' and 'input' (string or non-empty string array)"})
            return
        route = EMBED_ROUTES.get(model)
        if not route or not BACKEND_KEYS[route[0]]:
            self._json(400, {"error": f"unknown or unconfigured embedding model '{model}'",
                             "available": available_embedding_models()})
            return
        backend, native_model = route
        t0 = time.time()
        try:
            status, result, err = EMBED_ADAPTERS[backend](native_model, inputs)
        except Exception:
            self._json(502, {"error": f"backend '{backend}' failed"})
            return
        ms = int((time.time() - t0) * 1000)
        if status != 200:
            detail = (err or {}).get("provider_error", "backend error")
            self._json(502, {"error": f"backend '{backend}' returned {status}", "detail": detail})
            return
        print(f"embed model={model} backend={backend} n={len(result['embeddings'])} "
              f"tokens={result['prompt_tokens']} {ms}ms", flush=True)
        self._json(200, {
            "object": "list",
            "data": [{"object": "embedding", "index": i, "embedding": vec}
                     for i, vec in enumerate(result["embeddings"])],
            "model": model,
            "usage": {"prompt_tokens": result["prompt_tokens"],
                      "total_tokens": result["prompt_tokens"]},
        })

    def _qpu_out(self, status, result, err):
        if status == 200:
            self._json(200, result)
            return
        if isinstance(err, dict) and "provider_error" in err:
            self._json(502, {"error": "qpu provider error",
                             "detail": err["provider_error"]})
            return
        msg = err.get("error", "qpu error") if isinstance(err, dict) else str(err)
        if status == 400 or "not configured" in msg:
            self._json(400, {"error": msg})
        else:
            self._json(502, {"error": msg})

    def _qpu(self, method, parts, body, provider):
        """Route /v1/{qpu,azure,google}/* — the per-provider QPU gateways.

        IBM (/v1/qpu), Azure Quantum (/v1/azure), Google Quantum Engine
        (/v1/google). Same endpoint shape, same safety model; `provider`
        comes from the URL, never from a default.
        """
        mod = QPU_PROVIDERS[provider]
        root = f"/v1/{provider}"
        if parts == []:
            self._json(200, {"endpoints": [
                f"GET {root}/backends", f"GET {root}/usage",
                f"POST {root}/jobs", f"GET {root}/jobs/{{id}}",
                f"GET {root}/jobs/{{id}}/results", f"POST {root}/jobs/{{id}}/cancel",
            ], "configured": mod.configured()})
            return
        if method == "GET" and parts == ["backends"]:
            self._qpu_out(*mod.backends())
        elif method == "GET" and parts == ["usage"]:
            self._qpu_out(*mod.usage())
        elif method == "POST" and parts == ["jobs"]:
            if not isinstance(body, dict):
                self._json(400, {"error": "need JSON body with 'backend', 'shots', 'params'"})
                return
            print(f"{provider} submit backend={body.get('backend')} shots={body.get('shots')}",
                  flush=True)
            self._qpu_out(*mod.submit_job(body.get("backend"), body.get("shots"),
                                          body.get("params")))
        elif method == "GET" and len(parts) == 2 and parts[0] == "jobs":
            self._qpu_out(*mod.job_get(parts[1]))
        elif method == "GET" and len(parts) == 3 and parts[0] == "jobs" \
                and parts[2] == "results":
            self._qpu_out(*mod.job_results(parts[1]))
        elif method == "POST" and len(parts) == 3 and parts[0] == "jobs" \
                and parts[2] == "cancel":
            print(f"{provider} cancel job={parts[1]}", flush=True)
            self._qpu_out(*mod.job_cancel(parts[1]))
        else:
            self._json(404, {"error": "not found"})

    def _web_search(self, body):
        """POST /v1/web_search — the hub's own free web search."""
        query = body.get("query")
        count = body.get("count", 5)
        try:
            count = max(1, min(int(count), 10))
        except (TypeError, ValueError):
            self._json(400, {"error": "'count' must be an integer 1-10"})
            return
        status, results, err = websearch.web_search(query, count)
        if status == 400:
            self._json(400, {"error": (err or {}).get("error", "bad request")})
            return
        if status != 200:
            detail = (err or {}).get("provider_error", "search error")
            self._json(502, {"error": f"web search returned {status}", "detail": detail})
            return
        print(f"web_search src={websearch.source()} n={len(results)}", flush=True)
        self._json(200, {"query": query, "source": websearch.source(),
                         "results": results})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/v1/qpu" or path.startswith("/v1/qpu/") \
                or path == "/v1/azure" or path.startswith("/v1/azure/") \
                or path == "/v1/google" or path.startswith("/v1/google/"):
            body = self._read_json()
            if body is None:
                self._json(400, {"error": "invalid JSON body"})
                return
            segs = path.split("/")
            self._qpu("POST", segs[3:], body, segs[2])
        elif path == "/v1/chat/completions":
            body = self._read_json()
            if body is None:
                self._json(400, {"error": "invalid JSON body"})
                return
            self._chat_completions(body)
        elif path == "/v1/embeddings":
            body = self._read_json()
            if body is None:
                self._json(400, {"error": "invalid JSON body"})
                return
            self._embeddings(body)
        elif path == "/v1/web_search":
            body = self._read_json()
            if body is None:
                self._json(400, {"error": "invalid JSON body"})
                return
            self._web_search(body)
        else:
            self._json(404, {"error": "not found"})


if __name__ == "__main__":
    print(f"medic-model-hub v{VERSION} on :{PORT}  backends={configured_backends()}", flush=True)
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
