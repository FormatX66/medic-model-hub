#!/usr/bin/env python3
"""Medic Model Hub v1.0.0 — one OpenAI-compatible API for every model.

POST /v1/chat/completions with {"model": ..., "messages": [...]} and the hub
routes to the right provider behind the scenes. Keys live in one local .env
that builds never touch.

Backends: openai, anthropic, google (gemini). Only configured backends are
advertised. stdlib only — no pip dependencies.
"""
import json
import os
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

VERSION = "1.0.0"
PORT = int(os.environ.get("PORT", "8090"))

# ---------------------------------------------------------------- config ---
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

# model name (or alias) -> (backend, provider-native model id)
ROUTES = {
    "gpt": ("openai", "gpt-4o-mini"),
    "gpt-4o-mini": ("openai", "gpt-4o-mini"),
    "claude": ("anthropic", "claude-haiku-4-5-20251001"),
    "claude-haiku-4-5-20251001": ("anthropic", "claude-haiku-4-5-20251001"),
    "gemini": ("google", "gemini-flash-latest"),
    "gemini-flash-latest": ("google", "gemini-flash-latest"),
}

BACKEND_KEYS = {"openai": OPENAI_KEY, "anthropic": ANTHROPIC_KEY, "google": GEMINI_KEY}


def configured_backends():
    return {b: bool(k) for b, k in BACKEND_KEYS.items()}


def available_models():
    return [m for m, (b, _) in ROUTES.items() if BACKEND_KEYS[b]]


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


def openai_complete(model, system, chat, max_tokens, temperature):
    msgs = ([{"role": "system", "content": system}] if system else []) + chat
    payload = {"model": model, "messages": msgs, "temperature": temperature}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    status, body = _post("https://api.openai.com/v1/chat/completions", payload,
                         {"Authorization": f"Bearer {OPENAI_KEY}"})
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


ADAPTERS = {"openai": openai_complete, "anthropic": anthropic_complete, "google": gemini_complete}


# ---------------------------------------------------------------- server ---
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
        if self.path == "/health":
            self._json(200, {"ok": True, "version": VERSION, "backends": configured_backends()})
        elif self.path == "/v1/models":
            self._json(200, {"object": "list", "data": [
                {"id": m, "object": "model", "owned_by": b}
                for m, (b, _) in ROUTES.items() if BACKEND_KEYS[b]]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": "not found"})
            return
        body = self._read_json()
        if body is None:
            self._json(400, {"error": "invalid JSON body"})
            return
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
        except Exception as e:
            self._json(502, {"error": f"backend '{backend}' failed: {type(e).__name__}"})
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


if __name__ == "__main__":
    print(f"medic-model-hub v{VERSION} on :{PORT}  backends={configured_backends()}", flush=True)
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
