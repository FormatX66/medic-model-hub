#!/usr/bin/env python3
"""Medic Model Hub — Google Quantum Engine adapter (wired into the hub since v1.2.3).

Thin authenticated gateway to the Quantum Engine v1alpha1 API
(quantum.googleapis.com). Mirrors the IBM qpu.py adapter interface:

    configured() -> bool
    backends()   -> (status, result, err)   # processor list (slim)
    usage()      -> (status, result, err)   # reservation budgets (see note)
    submit_job(backend, shots, params) -> (status, result, err)
    job_get(job_id)      -> (status, result, err)
    job_results(job_id)  -> (status, result, err)
    job_cancel(job_id)   -> (status, result, err)

All public functions return (status, result, err) hub-style and never raise
on provider HTTP errors; network failures surface as status 0.

Auth model (stdlib-feasible):
  - OAuth2 refresh-token flow: GOOGLE_QUANTUM_CLIENT_ID /
    GOOGLE_QUANTUM_CLIENT_SECRET / GOOGLE_QUANTUM_REFRESH_TOKEN are
    exchanged at oauth2.googleapis.com/token for a short-lived access
    token (plain form POST — no SDK, no RSA signing needed).
  - Alternatively GOOGLE_QUANTUM_ACCESS_TOKEN carries a pre-minted
    bearer token (short-lived; the operator refreshes it).
  - Service-account JSON direct signing is NOT implemented: RS256
    signing is outside the stdlib. Documented as the known gap.

REST surface: v1alpha1 resource paths below follow the published
QuantumEngineService proto's REST transcoding
(projects/{pid}/processors, programs, jobs, calibrations). They are
OFFLINE-UNVERIFIED against the live service in this session — flagged
for the laptop agent's live verification (reads first). If REST
transcoding is not enabled for a method, the fallback is cirq-google
(non-stdlib); this adapter is the stdlib best-effort path.

Access reality check: hardware access requires Google approval and an
approved Cloud project. This adapter only makes sense once
GOOGLE_QUANTUM_PROJECT_ID belongs to an approved project; otherwise
every call fails provider-side and the adapter reports it honestly.

Safety gates (same philosophy as the IBM adapter):
  - processor allowlist GOOGLE_QUANTUM_PROCESSORS (default EMPTY —
    submit refuses until the operator names processors explicitly)
  - repetitions cap GOOGLE_QUANTUM_MAX_REPETITIONS (default 1024)
  - append-only local ledger (audit trail)
  - NO quota gate: Quantum Engine exposes no general quota endpoint;
    usage() surfaces reservation budgets only. Hardware submits always
    need the operator's explicit go-ahead per Bruce's rules.

The caller serializes its own circuits (program_code string — Cirq JSON,
QASM, or provider-native text); the hub never interprets results —
output is evidence, returned raw.

stdlib only — no pip dependencies.
"""
import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid

USER_AGENT = "medic-model-hub/1.2.3"
API_BASE = "https://quantum.googleapis.com/v1alpha1"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# ---------------------------------------------------------------- config ---
PROJECT_ID = os.environ.get("GOOGLE_QUANTUM_PROJECT_ID", "").strip()
CLIENT_ID = os.environ.get("GOOGLE_QUANTUM_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("GOOGLE_QUANTUM_CLIENT_SECRET", "").strip()
REFRESH_TOKEN = os.environ.get("GOOGLE_QUANTUM_REFRESH_TOKEN", "").strip()
ACCESS_TOKEN = os.environ.get("GOOGLE_QUANTUM_ACCESS_TOKEN", "").strip()
PROCESSORS = [p.strip() for p in
              os.environ.get("GOOGLE_QUANTUM_PROCESSORS", "").split(",")
              if p.strip()]
MAX_REPS = int(os.environ.get("GOOGLE_QUANTUM_MAX_REPETITIONS", "1024") or 1024)
LEDGER_PATH = os.environ.get("GOOGLE_QPU_LEDGER", "data/google_qpu_ledger.json")

_token = None
_token_at = 0.0
_token_ttl = 3600


def configured():
    """True when we have a project and a way to mint/use a bearer token."""
    return bool(PROJECT_ID and (ACCESS_TOKEN or
                                (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN)))


def _config_error():
    if not PROJECT_ID:
        return "google quantum not configured (GOOGLE_QUANTUM_PROJECT_ID missing)"
    return ("google quantum not configured (need GOOGLE_QUANTUM_ACCESS_TOKEN "
            "or CLIENT_ID + CLIENT_SECRET + REFRESH_TOKEN)")


def _parent():
    return f"projects/{PROJECT_ID}"


def _processor_name(backend):
    return f"{_parent()}/processors/{backend}"


# ------------------------------------------------------------------ http ---
def _http(method, url, payload=None, bearer=None, timeout=60):
    """(status, body_dict). Never raises on HTTP errors; network -> (0, ...)."""
    data = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    if bearer:
        hdrs["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {"message": f"google quantum HTTP {e.code}"}
        return e.code, {"provider_error": body}
    except Exception as e:
        return 0, {"provider_error":
                   {"message": f"google quantum unreachable: {type(e).__name__}"}}


def _bearer():
    """OAuth2 access token, cached. Returns (token, err)."""
    global _token, _token_at, _token_ttl
    if ACCESS_TOKEN:
        return ACCESS_TOKEN, None
    now = time.time()
    if _token and (now - _token_at) < max(_token_ttl - 300, 60):
        return _token, None
    form = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json",
                 "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return None, f"OAuth token refresh failed: HTTP {e.code}"
    except Exception as e:
        return None, f"OAuth token refresh failed: {type(e).__name__}"
    token = payload.get("access_token")
    if not token:
        return None, "OAuth token refresh returned no access_token"
    try:
        _token_ttl = int(payload.get("expires_in", 3600))
    except (TypeError, ValueError):
        _token_ttl = 3600
    _token, _token_at = str(token), now
    return _token, None


def _authed(method, path, payload=None, timeout=60):
    """Authed v1alpha1 call. Returns (status, result, err) hub-style."""
    if not configured():
        return 0, None, {"error": _config_error()}
    bearer, err = _bearer()
    if err:
        return 0, None, {"error": err}
    status, body = _http(method, API_BASE + path, payload, bearer, timeout)
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
    """(status, result, err): slim processor list."""
    status, body, err = _authed("GET", f"/{_parent()}/processors")
    if status != 200:
        return status, None, err
    slim = []
    for p in body.get("processors", []) or []:
        name = p.get("name", "")
        slim.append({"id": name.split("/")[-1] if name else None,
                     "name": name,
                     "health": p.get("health")})
    return 200, {"backends": slim, "count": len(slim)}, None


def usage():
    """(status, result, err): reservation budgets.

    NOTE: Quantum Engine exposes no general quota/usage endpoint. This
    surfaces reservation budgets for approved reservation holders only;
    an empty list does NOT mean unlimited access — hardware submits
    always need the operator's explicit go-ahead.
    """
    status, body, err = _authed("GET", f"/{_parent()}/reservationBudgets")
    if status != 200:
        return status, None, err
    return 200, {"budgets": body.get("reservationBudgets", []) or [],
                 "note": "reservation budgets only; no general quota endpoint exists"}, None


def submit_job(backend, shots, params):
    """Create a program + job. Returns (status, result, err).

    params (dict) must carry:
      program_code  str   serialized circuit (Cirq JSON / QASM / native text)
    optional: code_type (label only), job_name.

    `shots` maps to the job's repetitions, capped by
    GOOGLE_QUANTUM_MAX_REPETITIONS.

    Safety gates: configured, backend in GOOGLE_QUANTUM_PROCESSORS
    allowlist (empty allowlist refuses everything), 1 <= repetitions <=
    MAX_REPS. No quota gate exists (see usage() note) — the operator's
    explicit approval is the gate.

    LIVE-VERIFY FLAG: the exact wire placement of `repetitions` in the
    CreateQuantumJob body follows the v1alpha1 proto as understood
    offline; confirm against the live service before trusting submits.
    """
    if not configured():
        return 0, None, {"error": _config_error()}
    if not PROCESSORS:
        return 400, None, {"error": "no GOOGLE_QUANTUM_PROCESSORS allowlist configured; "
                                    "refusing to submit"}
    if backend not in PROCESSORS:
        return 400, None, {"error": f"backend '{backend}' not in allowlist",
                           "allowlist": PROCESSORS}
    try:
        reps = int(shots)
    except (TypeError, ValueError):
        return 400, None, {"error": "'shots' (repetitions) must be an integer"}
    if reps < 1 or reps > MAX_REPS:
        return 400, None, {"error": f"'shots' must be 1..{MAX_REPS}"}
    if not isinstance(params, dict) or not params.get("program_code"):
        return 400, None, {"error": "'params' needs 'program_code' (serialized circuit)"}

    program_code = params["program_code"]
    code = program_code.decode() if isinstance(program_code, bytes) else program_code

    # Step 1: create the program.
    pstatus, pbody, perr = _authed("POST", f"/{_parent()}/programs", {
        "code": {"file": {"code": code}},
        "labels": {"created_by": "medic-hub"},
    }, timeout=120)
    if pstatus != 200:
        return pstatus, None, perr
    program_name = pbody.get("name")
    if not program_name:
        return 502, None, {"error": "program creation returned no resource name",
                           "detail": pbody}

    # Step 2: create the job against the program + processor.
    job_name = params.get("job_name") or f"job-{uuid.uuid4().hex}"
    jstatus, jbody, jerr = _authed("POST", f"/{_parent()}/jobs", {
        "name": f"{_parent()}/jobs/{job_name}",
        "program": {"name": program_name},
        "processor": {"name": _processor_name(backend)},
        "repetitions": reps,
        "labels": {"created_by": "medic-hub"},
    }, timeout=120)
    if jstatus != 200:
        return jstatus, None, jerr
    job_id = (jbody.get("name") or "").split("/")[-1] or job_name
    _ledger_append({"job_id": job_id, "job_name": job_name,
                    "program": program_name, "backend": backend,
                    "repetitions": reps,
                    "submitted_at": int(time.time())})
    return 200, {"job_id": job_id, "job_name": job_name,
                 "program": program_name, "backend": backend,
                 "repetitions": reps,
                 "status": ((jbody.get("executionStatus") or {})
                            .get("state"))}, None


def job_get(job_id):
    """(status, result, err): job metadata + execution status."""
    path = f"/{_parent()}/jobs/{urllib.parse.quote(str(job_id), safe='')}"
    return _authed("GET", path)


def job_results(job_id):
    """(status, result, err): raw QuantumResult (evidence, not interpretation)."""
    path = (f"/{_parent()}/jobs/{urllib.parse.quote(str(job_id), safe='')}"
            f"/result")
    return _authed("GET", path, timeout=120)


def job_cancel(job_id):
    """(status, result, err): request cancellation of a job."""
    path = (f"/{_parent()}/jobs/{urllib.parse.quote(str(job_id), safe='')}"
            f":cancel")
    return _authed("POST", path, {})
