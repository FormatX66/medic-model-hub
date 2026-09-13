#!/usr/bin/env python3
"""Medic Model Hub — IBM Quantum (QPU) adapter, v1.2.2.

Thin authenticated gateway to Qiskit Runtime on IBM Quantum Platform.
The hub owns the credential (IBM_QUANTUM_API_KEY in .env), exchanges it
for a short-lived IAM bearer token, and enforces Bruce's safety gates:

  - backend allowlist (QPU_BACKENDS; default Kingston/Fez/Marrakesh)
  - shot cap (QPU_MAX_SHOTS; default 1024)
  - sampler program only (v1.2.0)
  - live quota check before every submit (refuses when IBM reports the
    free-tier limit reached or no time remaining)
  - append-only local ledger of every submitted job (audit trail)

The caller serializes its own circuits (SamplerV2 PUB params); the hub
never interprets results — QPU output is evidence, returned raw.

REST reference: qiskit-ibm-runtime 0.49 SDK (api/rest/runtime.py,
program_job.py, api/utils.py default_runtime_url_resolver).
Base URL: https://quantum.cloud.ibm.com/api/v1 (us-east) or
https://{region}.quantum.cloud.ibm.com/api/v1.

stdlib only — no pip dependencies.
"""
import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error

# ---------------------------------------------------------------- config ---
API_KEY = os.environ.get("IBM_QUANTUM_API_KEY", "").strip()
CRN = os.environ.get("IBM_QUANTUM_CRN", "").strip()
REGION = os.environ.get("QPU_REGION", "").strip().lower()
BACKENDS = [b.strip() for b in
            os.environ.get("QPU_BACKENDS",
                           "ibm_kingston,ibm_fez,ibm_marrakesh").split(",")
            if b.strip()]
MAX_SHOTS = int(os.environ.get("QPU_MAX_SHOTS", "1024") or 1024)
LEDGER_PATH = os.environ.get("QPU_LEDGER", "data/qpu_ledger.json")
IAM_TOKEN_URL = "https://iam.cloud.ibm.com/identity/token"
TOKEN_TTL = 3300  # refresh IAM bearer before its 1h expiry

# Explicit client UA on every outbound call. urllib's default
# ("Python-urllib/3.x") trips Cloudflare's bot check on IBM's API front
# door (HTTP 403 on reads even with a valid IAM token); an honest
# client-identifying UA passes. Bumped with the hub version.
HUB_VERSION = "1.2.2"
USER_AGENT = f"medic-model-hub/{HUB_VERSION}"

_token = None
_token_at = 0.0


def configured():
    return bool(API_KEY)


def _region():
    if REGION:
        return REGION
    # CRN shape: crn:v1:bluemix:public:quantum-computing:{location}:a/...:....
    if CRN:
        parts = CRN.split(":")
        if len(parts) > 5 and parts[5]:
            return parts[5].lower()
    return "us-east"


def _api_base():
    region = _region()
    prefix = "" if region == "us-east" else f"{region}."
    return f"https://{prefix}quantum.cloud.ibm.com/api/v1"


# ------------------------------------------------------------------ http ---
def _http(method, url, payload=None, bearer=None, timeout=60):
    """(status, body_dict). Never raises on HTTP errors; network errors -> (0, ...)."""
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if CRN:
        headers["Service-CRN"] = CRN
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {"message": f"qpu HTTP {e.code}"}
        return e.code, {"provider_error": body}
    except Exception as e:
        return 0, {"provider_error": {"message": f"qpu unreachable: {type(e).__name__}"}}


def _bearer():
    """IAM bearer token, cached. Returns (token, err)."""
    global _token, _token_at
    if _token and (time.time() - _token_at) < TOKEN_TTL:
        return _token, None
    form = urllib.parse.urlencode({
        "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
        "apikey": API_KEY,
    }).encode()
    req = urllib.request.Request(
        IAM_TOKEN_URL, data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json",
                 "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return None, f"IAM exchange failed: HTTP {e.code}"
    except Exception as e:
        return None, f"IAM exchange failed: {type(e).__name__}"
    token = payload.get("access_token")
    if not token:
        return None, "IAM exchange returned no access_token"
    _token, _token_at = str(token), time.time()
    return _token, None


def _authed(method, path, payload=None, timeout=60):
    """Authed Runtime API call. Returns (status, result, err) hub-style."""
    if not configured():
        return 0, None, {"error": "qpu not configured (IBM_QUANTUM_API_KEY missing)"}
    bearer, err = _bearer()
    if err:
        return 0, None, {"error": err}
    status, body = _http(method, _api_base() + path, payload, bearer, timeout)
    if status != 200:
        return status, None, body
    return 200, body, None


# ----------------------------------------------------------------- ledger ---
def _ledger_append(entry):
    try:
        base = os.path.dirname(LEDGER_PATH)
        if base:
            os.makedirs(base, exist_ok=True)
        try:
            with open(LEDGER_PATH, encoding="utf-8") as f:
                ledger = json.load(f)
            if not isinstance(ledger, list):
                ledger = []
        except (OSError, ValueError):
            ledger = []
        ledger.append(entry)
        with open(LEDGER_PATH, "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2)
    except OSError:
        pass  # ledger is audit-only; never block a job on it


# ------------------------------------------------------------------ API ----
def backends():
    """(status, result, err): slim backend list."""
    status, body, err = _authed("GET", "/backends")
    if status != 200:
        return status, None, err
    raw = body.get("backends") or body.get("devices") or []
    slim = [{
        "name": b.get("backend_name") or b.get("name"),
        "qubits": b.get("number_of_qubits") or b.get("qubits"),
        "operational": (b.get("status") or {}).get("operational")
        if isinstance(b.get("status"), dict) else b.get("operational"),
        "pending_jobs": (b.get("status") or {}).get("pending_jobs")
        if isinstance(b.get("status"), dict) else b.get("pending_jobs"),
    } for b in raw if isinstance(b, dict)]
    return 200, {"backends": slim, "count": len(slim)}, None


def usage():
    """(status, result, err): live quota/usage."""
    return _authed("GET", "/instances/usage")


def submit_job(backend, shots, params):
    """Submit a SamplerV2 job. Returns (status, result, err).

    Safety gates (all enforced before anything touches hardware):
      - qpu configured
      - backend in QPU_BACKENDS allowlist
      - 1 <= shots <= QPU_MAX_SHOTS
      - live quota: refuses when IBM reports limit reached / none remaining
    """
    if not configured():
        return 0, None, {"error": "qpu not configured (IBM_QUANTUM_API_KEY missing)"}
    if backend not in BACKENDS:
        return 400, None, {"error": f"backend '{backend}' not in allowlist",
                           "allowlist": BACKENDS}
    try:
        shots = int(shots)
    except (TypeError, ValueError):
        return 400, None, {"error": "'shots' must be an integer"}
    if shots < 1 or shots > MAX_SHOTS:
        return 400, None, {"error": f"'shots' must be 1..{MAX_SHOTS}"}
    if not isinstance(params, list) or not params:
        return 400, None, {"error": "'params' must be a non-empty array of SamplerV2 PUBs"}

    # Live quota gate — the free-tier budget is enforced by IBM itself.
    ustatus, ubody, uerr = usage()
    if ustatus == 200 and isinstance(ubody, dict):
        if ubody.get("usage_limit_reached"):
            return 400, None, {"error": "IBM reports QPU usage limit reached; not submitting"}
        remaining = ubody.get("usage_remaining_seconds")
        if isinstance(remaining, (int, float)) and remaining <= 0:
            return 400, None, {"error": "IBM reports no QPU time remaining; not submitting"}

    # Normalize shots: the hub's validated value wins in every PUB.
    pubs = []
    for p in params:
        if isinstance(p, dict):
            p = dict(p)
            p["shots"] = shots
        pubs.append(p)

    status, body, err = _authed("POST", "/jobs", {
        "program_id": "sampler",
        "backend": backend,
        "params": pubs,
        "tags": ["medic-hub"],
    }, timeout=120)
    if status != 200:
        return status, None, err
    job_id = body.get("id") or body.get("job_id")
    _ledger_append({"job_id": job_id, "backend": backend, "shots": shots,
                    "program_id": "sampler", "submitted_at": int(time.time())})
    return 200, {"job_id": job_id, "backend": backend, "shots": shots,
                 "status": body.get("status")}, None


def job_get(job_id):
    """(status, result, err): job metadata + status."""
    return _authed("GET", f"/jobs/{urllib.parse.quote(str(job_id), safe='')}")


def job_results(job_id):
    """(status, result, err): raw job results (evidence, not interpretation)."""
    return _authed("GET", f"/jobs/{urllib.parse.quote(str(job_id), safe='')}/results",
                   timeout=120)


def job_cancel(job_id):
    """(status, result, err): cancel a queued/running job."""
    return _authed("POST", f"/jobs/{urllib.parse.quote(str(job_id), safe='')}/cancel")
