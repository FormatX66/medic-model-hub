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
import qpu

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
    "object": "response",
    "output": [
        {"type": "message", "role": "assistant", "status": "completed",
         "content": [{"type": "output_text", "text": "live ", "annotations": []},
                     {"type": "output_text", "text": "answer", "annotations": []}]},
    ],
    "usage": {"input_tokens": 40, "output_tokens": 12, "total_tokens": 52}})
_real_websearch = hub.websearch.web_search
hub.websearch.web_search = lambda q, count=5: (0, None, {"error": "stubbed"})  # offline: no grounding
s, r, e = hub.perplexity_complete("sonar", "sys", [{"role": "user", "content": "news?"}], 100, 0.7)
check("perplexity text", r["text"] == "live answer")
check("perplexity agent endpoint",
      hub._post.last["url"] == "https://api.perplexity.ai/v1/responses")
check("perplexity instructions passthrough", hub._post.last["payload"]["instructions"] == "sys")
check("perplexity input passthrough",
      hub._post.last["payload"]["input"] == [{"role": "user", "content": "news?"}])
check("perplexity no legacy messages key", "messages" not in hub._post.last["payload"])
check("perplexity max_output_tokens", hub._post.last["payload"]["max_output_tokens"] == 100)
check("perplexity no tools by default (no per-call search fee)",
      "tools" not in hub._post.last["payload"])
check("perplexity bearer auth", hub._post.last["headers"]["Authorization"].startswith("Bearer "))
check("perplexity usage", r["prompt_tokens"] == 40 and r["completion_tokens"] == 12)

s, r, e = hub.perplexity_complete("sonar-pro", None, [{"role": "user", "content": "x"}], None, 0.7)
check("perplexity no instructions when absent", "instructions" not in hub._post.last["payload"])
check("perplexity no max_output_tokens when absent",
      "max_output_tokens" not in hub._post.last["payload"])

# --- perplexity: paid search opt-in vs free hub grounding ------------------
print("perplexity search behavior:")
import os as _os2


def fake_search_ok(query, count=5):
    fake_search_ok.last = (query, count)
    return 200, [{"title": "T1", "url": "https://example.com/1", "snippet": "s1"},
                 {"title": "T2", "url": "https://example.com/2", "snippet": "s2"}], None


def fake_search_fail(query, count=5):
    return 0, None, {"error": "down"}


_os2.environ.pop("SONAR_WEB_SEARCH", None)
hub.websearch.web_search = fake_search_ok
s, r, e = hub.perplexity_complete("sonar", "sys", [{"role": "user", "content": "news?"}], 100, 0.7)
inp = hub._post.last["payload"]["input"]
check("perplexity grounding prepended", len(inp) == 2 and "T1" in inp[0]["content"]
      and "https://example.com/1" in inp[0]["content"])
check("perplexity original chat preserved", inp[1] == {"role": "user", "content": "news?"})
check("perplexity search queried from user turn", fake_search_ok.last[0] == "news?")
check("perplexity grounded call has no tools", "tools" not in hub._post.last["payload"])

hub.websearch.web_search = fake_search_fail
s, r, e = hub.perplexity_complete("sonar", None, [{"role": "user", "content": "x"}], None, 0.7)
check("perplexity ungrounded fallback on search failure",
      hub._post.last["payload"]["input"] == [{"role": "user", "content": "x"}]
      and "tools" not in hub._post.last["payload"])

_os2.environ["SONAR_WEB_SEARCH"] = "1"
hub.websearch.web_search = fake_search_ok
s, r, e = hub.perplexity_complete("sonar", None, [{"role": "user", "content": "x"}], None, 0.7)
check("perplexity env opt-in attaches paid tools",
      {"type": "web_search"} in hub._post.last["payload"].get("tools", []))
check("perplexity env opt-in skips hub grounding",
      hub._post.last["payload"]["input"] == [{"role": "user", "content": "x"}])
del _os2.environ["SONAR_WEB_SEARCH"]

s, r, e = hub.perplexity_complete("sonar", None, [{"role": "user", "content": "x"}], None, 0.7,
                                  paid_search=True)
check("perplexity explicit paid_search=True attaches tools",
      {"type": "web_search"} in hub._post.last["payload"].get("tools", []))
_os2.environ["SONAR_WEB_SEARCH"] = "1"
s, r, e = hub.perplexity_complete("sonar", None, [{"role": "user", "content": "x"}], None, 0.7,
                                  paid_search=False)
check("perplexity explicit paid_search=False wins over env",
      "tools" not in hub._post.last["payload"])
del _os2.environ["SONAR_WEB_SEARCH"]
check("perplexity opt-in helper off by default",
      hub.sonar_paid_search_opt_in() is False)
hub.websearch.web_search = _real_websearch

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
check("health reports web_search source", b["backends"]["web_search"] in ("brave", "duckduckgo"))

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

_real_g = hub.websearch.web_search


def _fake_ws_ep(q, count=5):
    if not isinstance(q, str) or not q.strip():
        return 400, None, {"error": "need a non-empty 'query'"}
    return 200, [{"title": "T", "url": "https://e.example", "snippet": "snip"}], None


hub.websearch.web_search = _fake_ws_ep
st, b = req("POST", "/v1/web_search", {"query": "latest news", "count": 3})
check("POST /v1/web_search happy path",
      st == 200 and b["query"] == "latest news"
      and b["results"] == [{"title": "T", "url": "https://e.example", "snippet": "snip"}]
      and b["source"] in ("brave", "duckduckgo"))
st, b = req("POST", "/v1/web_search", {"query": ""})
check("POST /v1/web_search empty query 400", st == 400)
st, b = req("POST", "/v1/web_search", {"count": 2})
check("POST /v1/web_search missing query 400", st == 400)
st, b = req("POST", "/v1/web_search", {"query": "x", "count": "zzz"})
check("POST /v1/web_search bad count 400", st == 400)
hub.websearch.web_search = lambda q, count=5: (500, None, {"provider_error": {"error": "down"}})
st, b = req("POST", "/v1/web_search", {"query": "x"})
check("POST /v1/web_search provider failure -> 502 sanitized",
      st == 502 and "detail" in b)
hub.websearch.web_search = _real_g

hub.BACKEND_KEYS["openai"] = ""
st, b = req("POST", "/v1/embeddings", {"model": "embed", "input": "x"})
check("embed unconfigured backend 400", st == 400 and "embed" not in b["available"])
hub.BACKEND_KEYS["openai"] = "k1"
srv.shutdown()

# --- QPU adapter + HTTP tests (stubbed Runtime API, no network) ------------
print("qpu:")
import tempfile
import os as _os

qpu.API_KEY = ""  # start unconfigured
qpu.BACKENDS = ["ibm_kingston", "ibm_fez"]
qpu.MAX_SHOTS = 1024
qpu.LEDGER_PATH = _os.path.join(tempfile.mkdtemp(), "ledger.json")

# --- outbound HTTP headers (real _http/_bearer, stubbed urlopen) -----------
print("qpu headers:")
_captured = []
_real_urlopen = urllib.request.urlopen


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _hdrs(req):
    return {k.lower(): v for k, v in req.header_items()}


try:
    def _cap(req, timeout=None):
        _captured.append(req)
        return _FakeResp(200, {"ok": True})

    urllib.request.urlopen = _cap
    st, body = qpu._http("GET", "https://example.invalid/api/v1/backends",
                         bearer="tok123")
    check("qpu _http ok through stub", st == 200 and body == {"ok": True})
    check("qpu _http sends hub User-Agent",
          _hdrs(_captured[-1]).get("user-agent") == qpu.USER_AGENT,
          repr(_hdrs(_captured[-1]).get("user-agent")))
    check("qpu User-Agent is not python-urllib",
          "python-urllib" not in (_hdrs(_captured[-1]).get("user-agent") or ""))
    check("qpu _http keeps bearer auth",
          _hdrs(_captured[-1]).get("authorization") == "Bearer tok123")
    check("qpu _http keeps json accept",
          _hdrs(_captured[-1]).get("accept") == "application/json")

    _captured.clear()
    qpu.API_KEY = "kq"
    qpu._token, qpu._token_at = None, 0.0

    def _cap_token(req, timeout=None):
        _captured.append(req)
        return _FakeResp(200, {"access_token": "iam-abc"})

    urllib.request.urlopen = _cap_token
    tok, terr = qpu._bearer()
    check("qpu IAM exchange returns token", tok == "iam-abc" and terr is None)
    check("qpu IAM request sends hub User-Agent",
          _hdrs(_captured[-1]).get("user-agent") == qpu.USER_AGENT)
    check("qpu IAM request is form-encoded",
          _hdrs(_captured[-1]).get("content-type") == "application/x-www-form-urlencoded")
finally:
    urllib.request.urlopen = _real_urlopen
    qpu._token, qpu._token_at = None, 0.0
    qpu.API_KEY = ""

# --- websearch module (Brave + DuckDuckGo, stubbed HTTP) -------------------
print("websearch:")
import websearch as ws

_cap2 = []
_real2 = urllib.request.urlopen


def _brave_resp(req, timeout=None):
    _cap2.append(req)
    return _FakeResp(200, {"web": {"results": [
        {"title": "B1", "url": "https://b1.example", "description": "desc one"},
        {"title": "B2", "url": "https://b2.example", "description": "desc two"}]}})


def _ddg_resp(req, timeout=None):
    _cap2.append(req)
    return _FakeResp(200, {"RelatedTopics": [
        {"Text": "Alpha - first result text", "FirstURL": "https://a.example/1"},
        {"Name": "Group", "Topics": [
            {"Text": "Beta - nested result", "FirstURL": "https://b.example/2"}]},
        {"Text": "junk", "FirstURL": ""},  # no usable URL -> skipped
    ]})


def _boom(req, timeout=None):
    raise urllib.error.HTTPError(req.full_url, 500, "oops", {}, None)


import os as _os3
_os3.environ["BRAVE_SEARCH_API_KEY"] = "test-brave-key"
try:
    urllib.request.urlopen = _brave_resp
    check("websearch source brave with key", ws.source() == "brave")
    st, res, err = ws.web_search("hello world", count=2)
    check("websearch brave ok", st == 200 and err is None and len(res) == 2)
    check("websearch brave result shape",
          res[0] == {"title": "B1", "url": "https://b1.example", "snippet": "desc one"})
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(_cap2[-1].full_url).query))
    check("websearch brave query encoded", q.get("q") == "hello world")
    check("websearch brave subscription header",
          _hdrs(_cap2[-1]).get("x-subscription-token") == "test-brave-key")
    check("websearch brave UA is hub UA, not python-urllib",
          _hdrs(_cap2[-1]).get("user-agent") == ws.UA
          and "python-urllib" not in _hdrs(_cap2[-1]).get("user-agent"))
finally:
    del _os3.environ["BRAVE_SEARCH_API_KEY"]

_os3.environ.pop("BRAVE_SEARCH_API_KEY", None)
try:
    urllib.request.urlopen = _ddg_resp
    check("websearch source duckduckgo without key", ws.source() == "duckduckgo")
    st, res, err = ws.web_search("q", count=5)
    check("websearch ddg ok", st == 200 and err is None and len(res) == 2)
    check("websearch ddg flattens topic groups",
          res[1] == {"title": "Beta - nested result",
                     "url": "https://b.example/2",
                     "snippet": "Beta - nested result"})
    check("websearch ddg title from text", res[0]["title"].startswith("Alpha"))
    check("websearch ddg hits ddg endpoint",
          "api.duckduckgo.com" in _cap2[-1].full_url)
    check("websearch ddg honest UA", _hdrs(_cap2[-1]).get("user-agent") == ws.UA)

    st, res, err = ws.web_search("   ")
    check("websearch empty query 400", st == 400 and res is None)
    st, res, err = ws.web_search("q", count=999)
    check("websearch count clamped to 10", len(res) <= 10)

    urllib.request.urlopen = _boom
    st, res, err = ws.web_search("q")
    check("websearch http error surfaces", st == 500 and "provider_error" in err)
finally:
    urllib.request.urlopen = _real2

rt_calls = []


def fake_authed(method, path, payload=None, timeout=60):
    if not qpu.configured():
        return 0, None, {"error": "qpu not configured (IBM_QUANTUM_API_KEY missing)"}
    rt_calls.append((method, path, payload))
    if path == "/instances/usage":
        return 200, {"usage_consumed_seconds": 12, "usage_remaining_seconds": 588,
                     "usage_limit_seconds": 600, "usage_limit_reached": False}, None
    if path == "/backends":
        return 200, {"backends": [
            {"backend_name": "ibm_kingston", "number_of_qubits": 156,
             "status": {"operational": True, "pending_jobs": 3}}]}, None
    if method == "POST" and path == "/jobs":
        return 200, {"id": "job123", "status": "QUEUED"}, None
    if path == "/jobs/job123":
        return 200, {"id": "job123", "status": "DONE"}, None
    if path == "/jobs/job123/results":
        return 200, {"results": [{"data": {"counts": {"00": 100}}}]}, None
    if method == "POST" and path == "/jobs/job123/cancel":
        return 200, {"cancelled": True}, None
    return 404, None, {"provider_error": {"message": "nope"}}


qpu._authed = fake_authed

s, r, e = qpu.submit_job("ibm_kingston", 256, [{"shots": 1}])
check("qpu unconfigured submit blocked", s == 0 and "not configured" in e["error"])

s, r, e = qpu.backends()
check("qpu unconfigured backends blocked", s == 0 and r is None)

qpu.API_KEY = "kq"
s, r, e = qpu.backends()
check("qpu backends slim", s == 200 and r["backends"][0]["name"] == "ibm_kingston"
      and r["backends"][0]["qubits"] == 156 and r["backends"][0]["operational"] is True)

s, r, e = qpu.usage()
check("qpu usage passthrough", s == 200 and r["usage_remaining_seconds"] == 588)

rt_calls.clear()
s, r, e = qpu.submit_job("ibm_kingston", 256, [{"shots": 999, "circuit": "qasm..."}])
check("qpu submit ok", s == 200 and r["job_id"] == "job123" and r["shots"] == 256)
sent = [c for c in rt_calls if c[0] == "POST" and c[1] == "/jobs"][0][2]
check("qpu submit program/tags", sent["program_id"] == "sampler" and sent["tags"] == ["medic-hub"])
check("qpu submit shots normalized", sent["params"][0]["shots"] == 256)
check("qpu submit checked quota first",
      rt_calls[0][1] == "/instances/usage")
with open(qpu.LEDGER_PATH, encoding="utf-8") as f:
    ledger = json.load(f)
check("qpu ledger appended", isinstance(ledger, list) and ledger[-1]["job_id"] == "job123"
      and ledger[-1]["shots"] == 256)

s, r, e = qpu.submit_job("ibm_bogus", 256, [{"shots": 256}])
check("qpu backend allowlist enforced", s == 400 and "allowlist" in e["error"])

s, r, e = qpu.submit_job("ibm_kingston", 99999, [{"shots": 1}])
check("qpu shot cap enforced", s == 400 and "1024" in e["error"])

s, r, e = qpu.submit_job("ibm_kingston", 0, [{"shots": 1}])
check("qpu zero shots rejected", s == 400)

s, r, e = qpu.submit_job("ibm_kingston", 256, "notalist")
check("qpu params shape enforced", s == 400 and "params" in e["error"])

s, r, e = qpu.submit_job("ibm_kingston", 256, [])
check("qpu empty params rejected", s == 400)

orig_usage = qpu.usage
qpu.usage = lambda: (200, {"usage_limit_reached": True, "usage_remaining_seconds": 0}, None)
n_before = len(rt_calls)
s, r, e = qpu.submit_job("ibm_kingston", 256, [{"shots": 256}])
check("qpu quota gate refuses submit", s == 400 and "limit reached" in e["error"]
      and len(rt_calls) == n_before)  # no POST /jobs attempted
qpu.usage = orig_usage

s, r, e = qpu.job_get("job123")
check("qpu job_get", s == 200 and r["status"] == "DONE")

s, r, e = qpu.job_results("job123")
check("qpu job_results raw", s == 200 and r["results"][0]["data"]["counts"]["00"] == 100)

s, r, e = qpu.job_cancel("job123")
check("qpu job_cancel", s == 200 and r["cancelled"] is True)

s, r, e = qpu.job_get("missing")
check("qpu provider 404 -> provider_error", s == 404 and "provider_error" in e)

# HTTP layer with the real adapter wired in (Runtime API still stubbed)
srv2 = hub.HTTPServer(("127.0.0.1", 18091), hub.Handler)
threading.Thread(target=srv2.serve_forever, daemon=True).start()


def req2(method, path, body=None):
    r = urllib.request.Request(f"http://127.0.0.1:18091{path}",
                               data=json.dumps(body).encode() if body is not None else None,
                               headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


st, b = req2("GET", "/v1/qpu")
check("GET /v1/qpu index", st == 200 and "GET /v1/qpu/backends" in b["endpoints"]
      and b["configured"] is True)

st, b = req2("GET", "/v1/qpu/backends")
check("GET /v1/qpu/backends", st == 200 and b["backends"][0]["name"] == "ibm_kingston")

st, b = req2("GET", "/v1/qpu/usage")
check("GET /v1/qpu/usage", st == 200 and b["usage_remaining_seconds"] == 588)

st, b = req2("POST", "/v1/qpu/jobs",
             {"backend": "ibm_kingston", "shots": 256, "params": [{"shots": 5}]})
check("POST /v1/qpu/jobs", st == 200 and b["job_id"] == "job123")

st, b = req2("POST", "/v1/qpu/jobs",
             {"backend": "ibm_bogus", "shots": 256, "params": [{"shots": 5}]})
check("POST /v1/qpu/jobs bad backend 400", st == 400 and "allowlist" in b["error"])

st, b = req2("POST", "/v1/qpu/jobs",
             {"backend": "ibm_kingston", "shots": 99999, "params": [{"shots": 5}]})
check("POST /v1/qpu/jobs over cap 400", st == 400 and "shots" in b["error"])

st, b = req2("GET", "/v1/qpu/jobs/job123")
check("GET /v1/qpu/jobs/{id}", st == 200 and b["status"] == "DONE")

st, b = req2("GET", "/v1/qpu/jobs/job123/results")
check("GET /v1/qpu/jobs/{id}/results", st == 200 and "results" in b)

st, b = req2("POST", "/v1/qpu/jobs/job123/cancel", {})
check("POST /v1/qpu/jobs/{id}/cancel", st == 200 and b["cancelled"] is True)

st, b = req2("GET", "/v1/qpu/jobs/missing")
check("GET /v1/qpu unknown job -> 502 sanitized", st == 502 and "detail" in b)

st, b = req2("GET", "/v1/qpu/nope")
check("GET /v1/qpu unknown path 404", st == 404)

qpu.API_KEY = ""
st, b = req2("GET", "/v1/qpu/backends")
check("qpu unconfigured -> 400", st == 400 and "not configured" in b["error"])
st, b = req2("GET", "/health")
check("health reports qpu flag", st == 200 and b["backends"]["qpu"] is False)
srv2.shutdown()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
