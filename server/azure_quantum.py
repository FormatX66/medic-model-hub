#!/usr/bin/env python3
"""Medic Model Hub — Azure Quantum adapter (wired into the hub since v1.2.3).

Thin authenticated gateway to the Azure Quantum Jobs data plane
(api-version 2022-09-12-preview) plus the ARM management plane for the
provider/target listing. Mirrors the IBM qpu.py adapter interface:

    configured() -> bool
    backends()   -> (status, result, err)   # slim provider/target list
    usage()      -> (status, result, err)   # quota list (slim)
    submit_job(backend, shots, params) -> (status, result, err)
    job_get(job_id)      -> (status, result, err)
    job_results(job_id)  -> (status, result, err)
    job_cancel(job_id)   -> (status, result, err)

All public functions return (status, result, err) hub-style and never raise
on provider HTTP errors; network failures surface as status 0.

Auth model (stdlib-feasible):
  - Resource addressing comes from AZURE_QUANTUM_CONNECTION_STRING
    (portal -> workspace -> Operations -> Access Keys; the documented
    AZURE_QUANTUM_CONNECTION_STRING convention). Parsed generically as
    key=value pairs; expected keys: SubscriptionId, ResourceGroupName,
    WorkspaceName, ApiKey, QuantumEndpoint (or derive from
    AZURE_QUANTUM_LOCATION).
  - Data/management-plane calls use Microsoft Entra client-credentials
    (AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET), a plain
    form POST — no SDK needed.
  - Direct workspace-API-key auth on the data plane is NOT attempted:
    the exact header semantics could not be verified from official docs
    in this session, and this adapter never invents auth headers.

Safety gates (same philosophy as the IBM adapter):
  - backend allowlist AZURE_QUANTUM_TARGETS (default EMPTY — submit
    refuses until the operator names targets explicitly)
  - shot cap AZURE_QUANTUM_MAX_SHOTS (default 1024)
  - live quota check before every submit (refuses on utilization>=limit)
  - append-only local ledger (audit trail)

Job input model: Azure Quantum jobs read input from blob storage. The
caller supplies a container SAS URI; the adapter uploads the input
payload as a blob (plain REST PUT) and references the container in the
job. The caller serializes its own circuits; the hub never interprets
results — output is evidence, returned raw.

stdlib only — no pip dependencies.
"""
import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
import xml.etree.ElementTree as ET

USER_AGENT = "medic-model-hub/1.2.3"
API_VERSION = "2022-09-12-preview"
ARM_API_VERSION = "2022-11-18-preview"

# ---------------------------------------------------------------- config ---
CONN_STR = os.environ.get("AZURE_QUANTUM_CONNECTION_STRING", "").strip()
TENANT_ID = os.environ.get("AZURE_TENANT_ID", "").strip()
CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET", "").strip()
LOCATION = os.environ.get("AZURE_QUANTUM_LOCATION", "").strip().lower()
TARGETS = [t.strip() for t in
           os.environ.get("AZURE_QUANTUM_TARGETS", "").split(",")
           if t.strip()]
MAX_SHOTS = int(os.environ.get("AZURE_QUANTUM_MAX_SHOTS", "1024") or 1024)
LEDGER_PATH = os.environ.get("AZURE_QPU_LEDGER", "data/azure_qpu_ledger.json")
TOKEN_URL_TMPL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

_tokens = {}  # scope -> (token, acquired_at, expires_in)


def _parse_conn_str(s):
    """Parse 'K=V;K=V' connection string into a case-insensitive dict."""
    out = {}
    for part in (s or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


_CONN = _parse_conn_str(CONN_STR)
SUBSCRIPTION_ID = _CONN.get("subscriptionid", "")
RESOURCE_GROUP = _CONN.get("resourcegroupname", "")
WORKSPACE = _CONN.get("workspacename", "")
API_KEY = _CONN.get("apikey", "")  # stored, never sent (see module docstring)


def configured():
    """True when we can address the workspace AND do Entra auth."""
    return bool(SUBSCRIPTION_ID and RESOURCE_GROUP and WORKSPACE
                and TENANT_ID and CLIENT_ID and CLIENT_SECRET)


def _config_error():
    if not (SUBSCRIPTION_ID and RESOURCE_GROUP and WORKSPACE):
        return ("azure quantum not configured "
                "(AZURE_QUANTUM_CONNECTION_STRING missing or incomplete)")
    return ("azure quantum not configured "
            "(AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET missing; "
            "direct API-key data-plane auth is not verified — see module docs)")


def _endpoint():
    ep = _CONN.get("quantumendpoint", "").rstrip("/")
    if ep:
        return ep
    if LOCATION:
        return f"https://{LOCATION}.quantum.azure.com"
    return ""


def _jobs_base():
    return (f"{_endpoint()}/subscriptions/{SUBSCRIPTION_ID}"
            f"/resourceGroups/{RESOURCE_GROUP}"
            f"/providers/Microsoft.Quantum/workspaces/{WORKSPACE}")


# ------------------------------------------------------------------ http ---
def _http(method, url, payload=None, headers=None, timeout=60):
    """(status, body_dict). Never raises on HTTP errors; network -> (0, ...)."""
    data = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, {"raw": raw[:2000]}
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {"message": f"azure quantum HTTP {e.code}"}
        return e.code, {"provider_error": body}
    except Exception as e:
        return 0, {"provider_error":
                   {"message": f"azure quantum unreachable: {type(e).__name__}"}}


def _aad_token(scope):
    """Entra client-credentials token for `scope`, cached. Returns (token, err)."""
    now = time.time()
    hit = _tokens.get(scope)
    if hit and (now - hit[1]) < max(hit[2] - 300, 60):
        return hit[0], None
    form = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": scope,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL_TMPL.format(tenant=urllib.parse.quote(TENANT_ID, safe="")),
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json",
                 "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return None, f"Entra token exchange failed: HTTP {e.code}"
    except Exception as e:
        return None, f"Entra token exchange failed: {type(e).__name__}"
    token = payload.get("access_token")
    if not token:
        return None, "Entra token exchange returned no access_token"
    try:
        ttl = int(payload.get("expires_in", 3600))
    except (TypeError, ValueError):
        ttl = 3600
    _tokens[scope] = (str(token), now, ttl)
    return str(token), None


def _authed(method, url, payload=None, timeout=60,
            scope="https://quantum.azure.com/.default"):
    """Authed call. Returns (status, result, err) hub-style."""
    if not configured():
        return 0, None, {"error": _config_error()}
    if not _endpoint():
        return 0, None, {"error": "azure quantum endpoint unknown "
                                 "(QuantumEndpoint not in connection string and "
                                 "AZURE_QUANTUM_LOCATION unset)"}
    token, err = _aad_token(scope)
    if err:
        return 0, None, {"error": err}
    status, body = _http(method, url, payload,
                         {"Authorization": f"Bearer {token}"}, timeout)
    if status >= 400 or (status == 0):
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
    """(status, result, err): slim provider/target list via the ARM plane."""
    url = (f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
           f"/resourceGroups/{RESOURCE_GROUP}"
           f"/providers/Microsoft.Quantum/workspaces/{WORKSPACE}"
           f"/providers?api-version={ARM_API_VERSION}")
    status, body, err = _authed("GET", url,
                                scope="https://management.azure.com/.default")
    if status != 200:
        return status, None, err
    slim = []
    for p in body.get("providers", []) or []:
        pid = p.get("providerId")
        for t in p.get("targets", []) or []:
            slim.append({"provider": pid,
                         "target": t.get("id"),
                         "availability": t.get("currentAvailability")})
    return 200, {"backends": slim, "count": len(slim)}, None


def usage():
    """(status, result, err): slim quota list for the workspace."""
    url = f"{_jobs_base()}/quotas?api-version={API_VERSION}"
    status, body, err = _authed("GET", url)
    if status != 200:
        return status, None, err
    slim = []
    for q in body.get("quotas", []) or []:
        dim = q.get("dimension") or {}
        slim.append({"name": dim.get("name"),
                     "provider": q.get("providerId"),
                     "scope": q.get("scope"),
                     "utilization": q.get("utilization"),
                     "limit": q.get("limit"),
                     "period": q.get("period")})
    return 200, {"quotas": slim, "count": len(slim)}, None


def _upload_input_blob(container_sas_uri, blob_name, content, content_type):
    """PUT a blob via container SAS URI. Returns (ok, err)."""
    base, _, query = container_sas_uri.partition("?")
    blob_url = f"{base.rstrip('/')}/{urllib.parse.quote(blob_name, safe='')}?{query}"
    data = content.encode() if isinstance(content, str) else content
    req = urllib.request.Request(
        blob_url, data=data, method="PUT",
        headers={"x-ms-blob-type": "BlockBlob",
                 "Content-Type": content_type,
                 "Content-Length": str(len(data)),
                 "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            if 200 <= resp.status < 300:
                return True, None
            return False, f"blob upload HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"blob upload HTTP {e.code}"
    except Exception as e:
        return False, f"blob upload failed: {type(e).__name__}"


def submit_job(backend, shots, params):
    """Submit a quantum job. Returns (status, result, err).

    params (dict) must carry:
      input_data         str|bytes  serialized circuit / program
      input_data_format  str        e.g. "qir.v1", "ionq.circuit.v1"
      container_sas_uri  str        container SAS URI for input upload
    optional: provider_id, output_data_format, job_name, content_type,
              blob_name.

    Safety gates: configured, backend in AZURE_QUANTUM_TARGETS allowlist
    (empty allowlist refuses everything), 1 <= shots <=
    AZURE_QUANTUM_MAX_SHOTS, live quota check (refuses on
    utilization >= limit for the job's provider).
    """
    if not configured():
        return 0, None, {"error": _config_error()}
    if not TARGETS:
        return 400, None, {"error": "no AZURE_QUANTUM_TARGETS allowlist configured; "
                                    "refusing to submit"}
    if backend not in TARGETS:
        return 400, None, {"error": f"backend '{backend}' not in allowlist",
                           "allowlist": TARGETS}
    try:
        shots = int(shots)
    except (TypeError, ValueError):
        return 400, None, {"error": "'shots' must be an integer"}
    if shots < 1 or shots > MAX_SHOTS:
        return 400, None, {"error": f"'shots' must be 1..{MAX_SHOTS}"}
    if not isinstance(params, dict):
        return 400, None, {"error": "'params' must be an object"}
    input_data = params.get("input_data")
    input_format = params.get("input_data_format")
    container_sas = params.get("container_sas_uri")
    if input_data is None or not input_format or not container_sas:
        return 400, None, {"error": "'params' needs 'input_data', "
                                    "'input_data_format' and 'container_sas_uri'"}
    provider_id = params.get("provider_id") or backend.split(".")[0]

    # Live quota gate.
    ustatus, ubody, _uerr = usage()
    if ustatus == 200 and isinstance(ubody, dict):
        for q in ubody.get("quotas", []) or []:
            if q.get("provider") != provider_id:
                continue
            try:
                if float(q.get("utilization", 0)) >= float(q.get("limit", 0)) \
                        and float(q.get("limit", 0)) > 0:
                    return 400, None, {"error": "Azure reports quota exhausted "
                                                f"for provider '{provider_id}'; not submitting",
                                       "quota": q}
            except (TypeError, ValueError):
                continue

    blob_name = params.get("blob_name") or f"input-{uuid.uuid4().hex}.dat"
    ok, err = _upload_input_blob(container_sas, blob_name, input_data,
                                 params.get("content_type", "application/octet-stream"))
    if not ok:
        return 502, None, {"error": err}

    job_id = f"job-{uuid.uuid4().hex}"
    job_name = params.get("job_name") or job_id
    body = {
        "id": job_id,
        "name": job_name,
        "providerId": provider_id,
        "target": backend,
        "inputDataFormat": input_format,
        "outputDataFormat": params.get("output_data_format",
                                       "microsoft.quantum-results.v1"),
        "inputDataUri": container_sas,
        "inputParams": {"shots": shots,
                        **(params.get("input_params") or {})},
    }
    url = f"{_jobs_base()}/jobs/{urllib.parse.quote(job_id, safe='')}?api-version={API_VERSION}"
    status, result, jerr = _authed("PUT", url, body, timeout=120)
    if status != 200:
        return status, None, jerr
    _ledger_append({"job_id": job_id, "job_name": job_name, "backend": backend,
                    "shots": shots, "input_format": input_format,
                    "submitted_at": int(time.time())})
    return 200, {"job_id": job_id, "job_name": job_name, "backend": backend,
                 "shots": shots, "status": (result or {}).get("status")}, None


def job_get(job_id):
    """(status, result, err): job metadata + status."""
    url = (f"{_jobs_base()}/jobs/{urllib.parse.quote(str(job_id), safe='')}"
           f"?api-version={API_VERSION}")
    return _authed("GET", url)


def _list_blobs(container_sas_uri):
    """List blob names in the container via SAS. Returns (names, err)."""
    base, _, query = container_sas_uri.partition("?")
    sep = "&" if query else ""
    url = f"{base}?{query}{sep}restype=container&comp=list"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            xml = resp.read().decode()
    except urllib.error.HTTPError as e:
        return None, f"blob list HTTP {e.code}"
    except Exception as e:
        return None, f"blob list failed: {type(e).__name__}"
    try:
        root = ET.fromstring(xml)
        names = [n.text for n in root.iter("Name")]
        return names, None
    except ET.ParseError:
        return None, "blob list XML unparseable"


def _get_blob(container_sas_uri, blob_name):
    """Download one blob via SAS. Returns (bytes, err)."""
    base, _, query = container_sas_uri.partition("?")
    url = (f"{base.rstrip('/')}/{urllib.parse.quote(blob_name, safe='')}"
           f"?{query}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read(), None
    except urllib.error.HTTPError as e:
        return None, f"blob download HTTP {e.code}"
    except Exception as e:
        return None, f"blob download failed: {type(e).__name__}"


def job_results(job_id):
    """(status, result, err): job metadata + downloaded output blobs (raw)."""
    status, meta, err = job_get(job_id)
    if status != 200:
        return status, None, err
    out_uri = (meta or {}).get("outputDataUri")
    if not out_uri:
        return 200, {"job": meta, "blobs": {},
                     "note": "no outputDataUri yet; job may still be running"}, None
    names, err = _list_blobs(out_uri)
    if err:
        return 502, None, {"error": err, "job": meta}
    blobs = {}
    for name in names or []:
        data, derr = _get_blob(out_uri, name)
        if derr:
            blobs[name] = {"error": derr}
            continue
        try:
            blobs[name] = json.loads(data.decode())
        except Exception:
            blobs[name] = {"raw_bytes": len(data),
                           "note": "non-JSON blob; byte length reported"}
    return 200, {"job": meta, "blobs": blobs}, None


def job_cancel(job_id):
    """(status, result, err): cancel a queued/running job."""
    url = (f"{_jobs_base()}/jobs/{urllib.parse.quote(str(job_id), safe='')}"
           f"/cancel?api-version={API_VERSION}")
    return _authed("POST", url, {})
