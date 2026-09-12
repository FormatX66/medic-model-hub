#!/usr/bin/env python3
"""Unit tests for the Medic Model Hub — translation + HTTP layer.
Run: python3 tests/test_hub.py  (no network, no keys needed)"""
import json
import sys
import threading
import urllib.request
import urllib.error

sys.path.insert(0, "server")
import hub

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} {extra}")


def fake_post_factory(response_body, status=200):
    def fake(url, payload, headers, timeout=120):
        fake.last = {"url": url, "payload": payload, "headers": headers}
        if status != 200:
            return status, {"provider_error": response_body}
        return status, response_body
    return fake


# --- adapter translation tests -------------------------------------------
print("adapter translation:")
hub._post = fake_post_factory({
    "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
s, r, e = hub.openai_complete("gpt-4o-mini", "sys", [{"role": "user", "content": "yo"}], 50, 0.7)
check("openai text", r["text"] == "hi")
check("openai system passthrough", hub._post.last["payload"]["messages"][0] == {"role": "system", "content": "sys"})
check("openai usage", r["prompt_tokens"] == 5 and r["completion_tokens"] == 2)
check("openai auth header", hub._post.last["headers"]["Authorization"].startswith("Bearer "))

hub._post = fake_post_factory({
    "content": [{"type": "text", "text": "hello"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 8, "output_tokens": 3}})
s, r, e = hub.anthropic_complete("claude-haiku-4-5-20251001", "be nice", [{"role": "user", "content": "yo"}], None, 0.5)
check("anthropic text", r["text"] == "hello")
check("anthropic default max_tokens", hub._post.last["payload"]["max_tokens"] == 1024)
check("anthropic system extracted", hub._post.last["payload"]["system"] == "be nice")
check("anthropic version header", hub._post.last["headers"]["anthropic-version"] == "2023-06-01")
check("anthropic finish map", r["finish"] == "stop")

hub._post = fake_post_factory({
    "candidates": [{"content": {"parts": [{"text": "hey"}]}, "finishReason": "STOP"}],
    "usageMetadata": {"promptTokenCount": 6, "candidatesTokenCount": 1}})
s, r, e = hub.gemini_complete("gemini-flash-latest", None, [{"role": "user", "content": "yo"}], 40, 0.9)
check("gemini text", r["text"] == "hey")
check("gemini role map", hub._post.last["payload"]["contents"][0]["role"] == "user")
check("gemini no systemInstruction when absent", "systemInstruction" not in hub._post.last["payload"])
check("gemini key header", "x-goog-api-key" in hub._post.last["headers"])

hub._post = fake_post_factory({
    "choices": [{"message": {"content": "live answer"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 40, "completion_tokens": 12}})
s, r, e = hub.perplexity_complete("sonar", "sys", [{"role": "user", "content": "news?"}], 100, 0.7)
check("perplexity text", r["text"] == "live answer")
check("perplexity endpoint", "api.perplexity.ai/chat/completions" in hub._post.last["url"])
check("perplexity system passthrough", hub._post.last["payload"]["messages"][0] == {"role": "system", "content": "sys"})
check("perplexity bearer auth", hub._post.last["headers"]["Authorization"].startswith("Bearer "))
check("perplexity usage", r["prompt_tokens"] == 40 and r["completion_tokens"] == 12)

hub._post = fake_post_factory({
    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]},
             {"object": "embedding", "index": 1, "embedding": [0.4, 0.5, 0.6]}],
    "usage": {"prompt_tokens": 7, "total_tokens": 7}})
s, r, e = hub.openai_embed("text-embedding-3-small", ["a", "b"])
check("embed vectors", r["embeddings"] == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])
check("embed endpoint", hub._post.last["url"] == "https://api.openai.com/v1/embeddings")
check("embed payload passthrough", hub._post.last["payload"] == {"model": "text-embedding-3-small", "input": ["a", "b"]})
check("embed usage", r["prompt_tokens"] == 7)

hub._post = fake_post_factory({"error": {"message": "bad key", "type": "auth"}}, status=401)
s, r, e = hub.openai_complete("gpt-4o-mini", None, [{"role": "user", "content": "x"}], None, 0.7)
check("provider error passthrough", s == 401 and r is None and e["provider_error"]["error"]["type"] == "auth")

sys_msg, chat = hub.split_messages([{"role": "system", "content": "s1"},
                                    {"role": "system", "content": "s2"},
                                    {"role": "user", "content": "q"}])
check("split_messages joins systems", sys_msg == "s1\ns2" and len(chat) == 1)

# --- HTTP layer tests (live server, stubbed adapters) ---------------------
print("http layer:")
hub.BACKEND_KEYS.update({"openai": "k1", "anthropic": "k2", "google": "k3", "perplexity": "k4"})
hub.ADAPTERS = {"openai": lambda *a: (200, {"text": "O", "finish": "stop", "prompt_tokens": 1, "completion_tokens": 1}, None),
                "anthropic": lambda *a: (200, {"text": "A", "finish": "stop", "prompt_tokens": 1, "completion_tokens": 1}, None),
                "google": lambda *a: (200, {"text": "G", "finish": "stop", "prompt_tokens": 1, "completion_tokens": 1}, None),
                "perplexity": lambda *a: (200, {"text": "P", "finish": "stop", "prompt_tokens": 1, "completion_tokens": 1}, None)}
hub.EMBED_ADAPTERS = {"openai": lambda *a: (200, {"embeddings": [[0.1, 0.2]], "prompt_tokens": 3}, None)}
srv = hub.HTTPServer(("127.0.0.1", 18090), hub.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def req(method, path, body=None):
    r = urllib.request.Request(f"http://127.0.0.1:18090{path}",
                               data=json.dumps(body).encode() if body is not None else None,
                               headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


st, b = req("GET", "/health")
check("GET /health", st == 200 and b["ok"] and b["backends"]["openai"] is True)

st, b = req("GET", "/v1/models")
check("GET /v1/models lists 11", st == 200 and len(b["data"]) == 11, str(len(b.get("data", []))))

st, b = req("POST", "/v1/chat/completions",
            {"model": "claude", "messages": [{"role": "user", "content": "hi"}]})
check("chat via alias", st == 200 and b["choices"][0]["message"]["content"] == "A"
      and b["object"] == "chat.completion" and b["usage"]["total_tokens"] == 2)

st, b = req("POST", "/v1/chat/completions",
            {"model": "gpt-4o-mini", "messages": [{"role": "system", "content": "s"},
                                                  {"role": "user", "content": "hi"}]})
check("chat openai shape", st == 200 and b["model"] == "gpt-4o-mini"
      and b["choices"][0]["finish_reason"] == "stop")

st, b = req("POST", "/v1/chat/completions", {"model": "nope", "messages": [{"role": "user", "content": "x"}]})
check("unknown model 400 + available list", st == 400 and "available" in b)

st, b = req("POST", "/v1/chat/completions",
            {"model": "gemini", "messages": [{"role": "user", "content": "x"}], "stream": True})
check("stream rejected", st == 400 and "stream" in b["error"])

st, b = req("POST", "/v1/chat/completions", {"model": "gpt"})
check("missing messages 400", st == 400)

hub.BACKEND_KEYS["google"] = ""
st, b = req("POST", "/v1/chat/completions", {"model": "gemini", "messages": [{"role": "user", "content": "x"}]})
check("unconfigured backend 400", st == 400 and "gemini" not in b["available"])
hub.BACKEND_KEYS["google"] = "k3"

hub.ADAPTERS["openai"] = lambda *a: (429, None, {"provider_error": {"message": "rate limited"}})
st, b = req("POST", "/v1/chat/completions", {"model": "gpt", "messages": [{"role": "user", "content": "x"}]})
check("provider 429 -> 502 sanitized", st == 502 and b["detail"] == {"message": "rate limited"})

st, b = req("GET", "/nope")
check("unknown path 404", st == 404)

st, b = req("POST", "/v1/chat/completions",
            {"model": "sonar", "messages": [{"role": "user", "content": "x"}]})
check("chat via sonar alias", st == 200 and b["choices"][0]["message"]["content"] == "P")

st, b = req("POST", "/v1/embeddings", {"model": "embed", "input": "hello"})
check("embeddings happy path", st == 200 and b["object"] == "list"
      and b["data"][0]["embedding"] == [0.1, 0.2] and b["model"] == "embed"
      and b["usage"]["total_tokens"] == 3)

st, b = req("POST", "/v1/embeddings",
            {"model": "text-embedding-3-small", "input": ["a", "b"]})
check("embeddings array input", st == 200 and b["data"][0]["object"] == "embedding")

st, b = req("POST", "/v1/embeddings", {"model": "nope", "input": "x"})
check("unknown embed model 400 + available", st == 400 and "available" in b)

st, b = req("POST", "/v1/embeddings", {"model": "embed"})
check("missing input 400", st == 400)

st, b = req("POST", "/v1/embeddings", {"model": "embed", "input": [""]})
check("empty-string array element passes validation", st == 200)
hub.BACKEND_KEYS["openai"] = ""
st, b = req("POST", "/v1/embeddings", {"model": "embed", "input": "x"})
check("embed unconfigured backend 400", st == 400 and "embed" not in b["available"])
hub.BACKEND_KEYS["openai"] = "k1"
srv.shutdown()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
