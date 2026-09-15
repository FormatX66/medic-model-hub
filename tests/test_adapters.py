#!/usr/bin/env python3
"""Offline unit tests for the incoming Azure Quantum + Google Quantum Engine
adapters. No network, no credentials — all HTTP is mocked.

Run: python3 tests/test_adapters.py   (from adapters-incoming/)
"""
import io
import json
import os
import sys
import tempfile
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
import azure_quantum
import google_quantum

_real_azure_http = azure_quantum._http
_real_gq_http = google_quantum._http
_real_gq_bearer = google_quantum._bearer

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} {extra}")


def http_fake_factory(responses):
    """responses: list of (status, body). Records every call in .calls."""
    calls = []

    def fake(method, url, payload=None, headers=None, timeout=60, **kw):
        calls.append({"method": method, "url": url, "payload": payload,
                      "headers": headers, "timeout": timeout, "kw": kw})
        status, body = responses[min(len(calls) - 1, len(responses) - 1)]
        return status, body
    fake.calls = calls
    return fake


def tmp_ledger(mod):
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    f.close()
    mod.LEDGER_PATH = f.name
    return f.name


# ============================================================ AZURE ======
print("azure_quantum:")

# --- config gates ---
azure_quantum.SUBSCRIPTION_ID = azure_quantum.RESOURCE_GROUP = ""
azure_quantum.WORKSPACE = azure_quantum.TENANT_ID = ""
azure_quantum.CLIENT_ID = azure_quantum.CLIENT_SECRET = ""
check("not configured without creds", azure_quantum.configured() is False)
s, r, e = azure_quantum.backends()
check("backends refuses unconfigured", s == 0 and "not configured" in e["error"])
s, r, e = azure_quantum.submit_job("x", 10, {})
check("submit refuses unconfigured", s == 0 and "not configured" in e["error"])

azure_quantum.SUBSCRIPTION_ID, azure_quantum.RESOURCE_GROUP = "sub1", "rg1"
azure_quantum.WORKSPACE, azure_quantum.TENANT_ID = "ws1", "ten1"
azure_quantum.CLIENT_ID, azure_quantum.CLIENT_SECRET = "cid", "csec"
azure_quantum._CONN = {"quantumendpoint": "https://eastus.quantum.azure.com"}
azure_quantum.TARGETS = []
check("configured with creds", azure_quantum.configured() is True)

# --- connection string parsing ---
cs = ("SubscriptionId=sub9;ResourceGroupName=rg9;WorkspaceName=ws9;"
      "ApiKey=sekret;QuantumEndpoint=https://eastus.quantum.azure.com")
parsed = azure_quantum._parse_conn_str(cs)
check("conn str subscription", parsed.get("subscriptionid") == "sub9")
check("conn str endpoint", parsed.get("quantumendpoint") ==
      "https://eastus.quantum.azure.com")
check("conn str apikey kept, unused", parsed.get("apikey") == "sekret")

# --- backends() shape ---
azure_quantum._aad_token = lambda scope: ("tok123", None)
azure_quantum._http = http_fake_factory([(200, {
    "providers": [{"providerId": "ionq",
                    "targets": [{"id": "ionq.simulator",
                                 "currentAvailability": "Available"},
                                {"id": "ionq.qpu.aria-1",
                                 "currentAvailability": "Available"}]}]})])
s, r, e = azure_quantum.backends()
call = azure_quantum._http.calls[0]
check("backends url = ARM providers",
      "management.azure.com" in call["url"] and "/providers?" in call["url"])
check("backends bearer", call["headers"] == {"Authorization": "Bearer tok123"})
check("backends slim", r["count"] == 2 and
      r["backends"][0] == {"provider": "ionq", "target": "ionq.simulator",
                           "availability": "Available"}, json.dumps(r))

# --- usage() shape ---
azure_quantum._http = http_fake_factory([(200, {
    "quotas": [{"dimension": {"name": "Jobs"}, "providerId": "ionq",
                "scope": "Workspace", "utilization": 3,
                "limit": 100, "period": "Monthly"}]})])
s, r, e = azure_quantum.usage()
call = azure_quantum._http.calls[0]
check("usage url = quotas", "/quotas?" in call["url"] and call["method"] == "GET")
check("usage slim", r["quotas"][0]["utilization"] == 3 and
      r["quotas"][0]["limit"] == 100)

# --- submit gates ---
azure_quantum.TARGETS = ["ionq.simulator"]
lp = tmp_ledger(azure_quantum)
s, r, e = azure_quantum.submit_job("rigetti.sim.qvm", 10, {})
check("submit allowlist gate", s == 400 and "allowlist" in e["error"])
s, r, e = azure_quantum.submit_job("ionq.simulator", 0, {})
check("submit shots low", s == 400 and "1.." in e["error"])
s, r, e = azure_quantum.submit_job("ionq.simulator", 99999, {})
check("submit shots high", s == 400 and "1.." in e["error"])
s, r, e = azure_quantum.submit_job("ionq.simulator", 10, {"input_data": "x"})
check("submit params incomplete", s == 400 and "container_sas_uri" in e["error"])
azure_quantum.TARGETS = []
s, r, e = azure_quantum.submit_job("ionq.simulator", 10, {
    "input_data": "x", "input_data_format": "qir.v1",
    "container_sas_uri": "https://a.blob.core.windows.net/c?sv=x"})
check("submit empty allowlist refuses", s == 400 and "allowlist" in e["error"])
azure_quantum.TARGETS = ["ionq.simulator"]

# --- submit happy path ---
uploaded = {}
azure_quantum._upload_input_blob = lambda uri, name, content, ctype: (
    uploaded.update(uri=uri, name=name, content=content, ctype=ctype),
    (True, None))[1]
azure_quantum._aad_token = lambda scope: ("tok123", None)
azure_quantum._http = http_fake_factory([
    (200, {"quotas": [{"providerId": "ionq", "utilization": 1, "limit": 100}]}),
    (200, {"id": "job-abc", "status": "Waiting"}),
])
s, r, e = azure_quantum.submit_job("ionq.simulator", 100, {
    "input_data": "QIR-BYTES", "input_data_format": "qir.v1",
    "container_sas_uri": "https://a.blob.core.windows.net/c?sv=x&sig=y",
    "provider_id": "ionq"})
check("submit ok", s == 200 and r["shots"] == 100 and
      r["backend"] == "ionq.simulator", json.dumps(e))
put = azure_quantum._http.calls[1]
check("submit PUT jobs url", "/jobs/job-" in put["url"] and
      "api-version=" in put["url"] and put["method"] == "PUT")
body = put["payload"]
check("submit body fields", body["target"] == "ionq.simulator" and
      body["inputDataFormat"] == "qir.v1" and
      body["inputDataUri"] == "https://a.blob.core.windows.net/c?sv=x&sig=y" and
      body["inputParams"]["shots"] == 100, json.dumps(body))
check("submit uploaded blob", uploaded["content"] == "QIR-BYTES" and
      uploaded["uri"].startswith("https://a.blob.core.windows.net/c?"))
check("submit bearer on PUT",
      put["headers"] == {"Authorization": "Bearer tok123"})
with open(lp, encoding="utf-8") as f:
    ledger = json.load(f)
check("submit ledger", len(ledger) == 1 and
      ledger[0]["backend"] == "ionq.simulator" and ledger[0]["shots"] == 100)
os.unlink(lp)

# --- submit quota gate blocks upload ---
calls = {"n": 0}
def counting_upload(uri, name, content, ctype):
    calls["n"] += 1
    return True, None
azure_quantum._upload_input_blob = counting_upload
azure_quantum._http = http_fake_factory([
    (200, {"quotas": [{"providerId": "ionq", "utilization": 100, "limit": 100}]}),
])
s, r, e = azure_quantum.submit_job("ionq.simulator", 10, {
    "input_data": "x", "input_data_format": "qir.v1",
    "container_sas_uri": "https://a.blob.core.windows.net/c?sv=x"})
check("submit quota-exhausted refuses", s == 400 and "quota" in e["error"].lower())
check("quota gate blocks upload", calls["n"] == 0)

# --- job_get / cancel shapes ---
azure_quantum._http = http_fake_factory([(200, {"id": "job-1", "status": "Succeeded"})])
s, r, e = azure_quantum.job_get("job-1")
check("job_get url", azure_quantum._http.calls[0]["url"].endswith(
    "/jobs/job-1?api-version=" + azure_quantum.API_VERSION))
azure_quantum._http = http_fake_factory([(200, {})])
s, r, e = azure_quantum.job_cancel("job-1")
check("job_cancel POST", azure_quantum._http.calls[0]["method"] == "POST" and
      azure_quantum._http.calls[0]["url"].endswith("/jobs/job-1/cancel?api-version=" +
                                                  azure_quantum.API_VERSION))

# --- job_results blob download ---
azure_quantum.job_get = lambda jid: (200, {"id": jid, "status": "Succeeded",
    "outputDataUri": "https://a.blob.core.windows.net/out?sv=x&sig=y"}, None)
azure_quantum._list_blobs = lambda uri: (["histogram.json", "raw.bin"], None)
def fake_get_blob(uri, name):
    if name == "histogram.json":
        return json.dumps({"0x0": 512, "0x1": 512}).encode(), None
    return b"\x00\x01", None
azure_quantum._get_blob = fake_get_blob
s, r, e = azure_quantum.job_results("job-1")
check("results blobs parsed",
      r["blobs"]["histogram.json"] == {"0x0": 512, "0x1": 512} and
      r["blobs"]["raw.bin"]["raw_bytes"] == 2, json.dumps(r)[:200])
azure_quantum.job_get = lambda jid: (200, {"id": jid, "status": "Executing"}, None)
s, r, e = azure_quantum.job_results("job-1")
check("results no-output note", s == 200 and r["blobs"] == {} and
      "note" in r)

# --- _http error paths (real function, mocked urlopen) ---
azure_quantum._http = _real_azure_http
import urllib.request as _ureq
real_urlopen = _ureq.urlopen
def boom_http(url, timeout=60, **kw):
    raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b'{"e":"no"}'))
_ureq.urlopen = boom_http
st, bd = azure_quantum._http("GET", "https://x.example/")
check("http 403 -> provider_error", st == 403 and "provider_error" in bd)
def boom_net(url, timeout=60, **kw):
    raise ConnectionError("down")
_ureq.urlopen = boom_net
st, bd = azure_quantum._http("GET", "https://x.example/")
check("http netfail -> status 0", st == 0 and "provider_error" in bd)
_ureq.urlopen = real_urlopen

# --- User-Agent on direct calls ---
sent = {}
def cap_urlopen(url, timeout=60, **kw):
    sent["headers"] = dict(url.headers)
    class R:
        status = 200
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return R()
_ureq.urlopen = cap_urlopen
azure_quantum._http("GET", "https://x.example/")
_ureq.urlopen = real_urlopen
ua = sent["headers"].get("User-agent", "")
check("user-agent header present", ua.startswith("medic-model-hub/"))

# ============================================================ GOOGLE =====
print("google_quantum:")

google_quantum.PROJECT_ID = ""
check("gq not configured w/o project", google_quantum.configured() is False)
s, r, e = google_quantum.backends()
check("gq backends refuses", s == 0 and "not configured" in e["error"])
google_quantum.PROJECT_ID = "proj-1"
check("gq still unconfigured w/o token path",
      google_quantum.configured() is False)
google_quantum.CLIENT_ID, google_quantum.CLIENT_SECRET = "cid", "csec"
google_quantum.REFRESH_TOKEN = "rtok"
check("gq configured w/ refresh creds", google_quantum.configured() is True)

# --- backends() ---
google_quantum._bearer = lambda: ("gtok", None)
google_quantum._http = http_fake_factory([(200, {"processors": [
    {"name": "projects/proj-1/processors/sycamore", "health": "OK"},
    {"name": "projects/proj-1/processors/weber", "health": "OK"}]})])
s, r, e = google_quantum.backends()
call = google_quantum._http.calls[0]
check("gq backends url",
      call["url"] == "https://quantum.googleapis.com/v1alpha1/projects/proj-1/processors"
      and call["method"] == "GET", call["url"])
check("gq backends bearer", call["headers"] == "gtok")
check("gq backends slim", r["count"] == 2 and r["backends"][0]["id"] == "sycamore"
      and r["backends"][0]["health"] == "OK", json.dumps(r))

# --- usage() budgets ---
google_quantum._http = http_fake_factory([(200, {"reservationBudgets": [
    {"name": "projects/proj-1/reservationBudgets/b1"}]})])
s, r, e = google_quantum.usage()
check("gq usage budgets path",
      google_quantum._http.calls[0]["url"].endswith("/projects/proj-1/reservationBudgets"))
check("gq usage note", "no general quota" in r["note"] and len(r["budgets"]) == 1)

# --- submit gates ---
google_quantum.PROCESSORS = []
s, r, e = google_quantum.submit_job("sycamore", 10, {"program_code": "x"})
check("gq empty allowlist refuses", s == 400 and "allowlist" in e["error"])
google_quantum.PROCESSORS = ["sycamore"]
s, r, e = google_quantum.submit_job("weber", 10, {"program_code": "x"})
check("gq backend allowlist", s == 400 and "allowlist" in e["error"])
s, r, e = google_quantum.submit_job("sycamore", 0, {"program_code": "x"})
check("gq reps low", s == 400 and "1.." in e["error"])
s, r, e = google_quantum.submit_job("sycamore", 10, {})
check("gq program_code required", s == 400 and "program_code" in e["error"])

# --- submit happy path (program then job) ---
lp = tmp_ledger(google_quantum)
google_quantum._http = http_fake_factory([
    (200, {"name": "projects/proj-1/programs/prog9"}),
    (200, {"name": "projects/proj-1/jobs/job9",
           "executionStatus": {"state": "READY"}}),
])
s, r, e = google_quantum.submit_job("sycamore", 500,
                                    {"program_code": "CIRCUIT-JSON"})
check("gq submit ok", s == 200 and r["repetitions"] == 500 and
      r["job_id"] == "job9", json.dumps(e))
prog_call, job_call = google_quantum._http.calls
check("gq program POST path",
      prog_call["url"].endswith("/projects/proj-1/programs") and
      prog_call["method"] == "POST", prog_call["url"])
check("gq program code inline",
      prog_call["payload"]["code"]["file"]["code"] == "CIRCUIT-JSON")
check("gq job POST path",
      job_call["url"].endswith("/projects/proj-1/jobs") and
      job_call["method"] == "POST")
jb = job_call["payload"]
check("gq job body refs",
      jb["program"] == {"name": "projects/proj-1/programs/prog9"} and
      jb["processor"] == {"name": "projects/proj-1/processors/sycamore"} and
      jb["repetitions"] == 500, json.dumps(jb))
with open(lp, encoding="utf-8") as f:
    ledger = json.load(f)
check("gq ledger", len(ledger) == 1 and ledger[0]["repetitions"] == 500)
os.unlink(lp)

# --- program-create failure aborts before job ---
google_quantum._http = http_fake_factory([
    (400, {"provider_error": {"message": "bad program"}}),
])
s, r, e = google_quantum.submit_job("sycamore", 10, {"program_code": "x"})
check("gq program failure aborts", s == 400 and
      len(google_quantum._http.calls) == 1)

# --- job_get / results / cancel shapes ---
google_quantum._http = http_fake_factory([(200, {"name": "projects/proj-1/jobs/j1",
    "executionStatus": {"state": "SUCCESS"}})])
s, r, e = google_quantum.job_get("j1")
check("gq job_get url", google_quantum._http.calls[0]["url"] ==
      "https://quantum.googleapis.com/v1alpha1/projects/proj-1/jobs/j1")
google_quantum._http = http_fake_factory([(200, {"jobExecutionResult": {"histogram": {}}})])
s, r, e = google_quantum.job_results("j1")
check("gq results url", google_quantum._http.calls[0]["url"].endswith("/jobs/j1/result"))
google_quantum._http = http_fake_factory([(200, {})])
s, r, e = google_quantum.job_cancel("j1")
c = google_quantum._http.calls[0]
check("gq cancel POST", c["method"] == "POST" and c["url"].endswith("/jobs/j1:cancel"))

# --- _bearer refresh flow (real function, mocked urlopen) ---
google_quantum._bearer = _real_gq_bearer
google_quantum.ACCESS_TOKEN = ""
google_quantum._token, google_quantum._token_at = None, 0.0
seen = {}
def tok_urlopen(url, timeout=30, **kw):
    seen["url"] = url.full_url if hasattr(url, "full_url") else url
    seen["data"] = url.data.decode()
    class R:
        status = 200
        def read(self): return json.dumps(
            {"access_token": "ya29.new", "expires_in": 3600}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return R()
_ureq.urlopen = tok_urlopen
tok, err = google_quantum._bearer()
_ureq.urlopen = real_urlopen
check("gq refresh token url", seen["url"] == "https://oauth2.googleapis.com/token")
check("gq refresh grant", "grant_type=refresh_token" in seen["data"] and
      "refresh_token=rtok" in seen["data"])
check("gq bearer returned", tok == "ya29.new" and err is None)
# cached: second call must not touch the network
_ureq.urlopen = lambda *a, **k: (_ for _ in ()).throw(AssertionError("nope"))
tok2, _ = google_quantum._bearer()
_ureq.urlopen = real_urlopen
check("gq bearer cached", tok2 == "ya29.new")

# --- gq _http error paths ---
google_quantum._http = _real_gq_http
_ureq.urlopen = boom_http
st, bd = google_quantum._http("GET", "https://x.example/", bearer="t")
check("gq 403 -> provider_error", st == 403 and "provider_error" in bd)
_ureq.urlopen = boom_net
st, bd = google_quantum._http("GET", "https://x.example/", bearer="t")
check("gq netfail -> 0", st == 0)
_ureq.urlopen = real_urlopen

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
