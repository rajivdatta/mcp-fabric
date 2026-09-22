"""Auth + thin REST client for the Microsoft Fabric MCP.

Configuration lives in `config.json` (auth type, optional tenant/client, scope,
writable flag). No secrets in that file; a service-principal config names an
environment variable (`client_secret_env`) loaded from `.env`.

Handles the two awkward parts of the Fabric REST API:
  * Definition create/update are long-running operations (202 + poll Location).
  * Jobs (RunNotebook / Refresh / Publish) return a job instance to monitor
    separately rather than polling to completion.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import time

import requests
from dotenv import load_dotenv

_HERE = pathlib.Path(__file__).parent
load_dotenv(_HERE / ".env")
_CFG = json.loads((_HERE / "config.json").read_text(encoding="utf-8"))

BASE = _CFG.get("base_url", "https://api.fabric.microsoft.com/v1")
SCOPE = _CFG.get("scope", "https://api.fabric.microsoft.com/.default")
_TIMEOUT = 60          # per-request seconds
_LRO_MAX_WAIT = 600    # cap on definition LRO polling, seconds

_cred = None
_AUTH_RECORD = _HERE / ".auth_record.json"


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
def _credential():
    global _cred
    if _cred is not None:
        return _cred
    auth = _CFG.get("auth", "broker")
    tenant = _CFG.get("tenant_id")
    if auth == "broker":
        import ctypes
        from azure.identity import AuthenticationRecord, TokenCachePersistenceOptions
        from azure.identity.broker import InteractiveBrowserBrokerCredential
        try:
            handle = ctypes.windll.kernel32.GetConsoleWindow()
        except Exception:
            handle = 0
        broker_kwargs = dict(
            tenant_id=tenant,
            parent_window_handle=handle or 0,
            use_default_broker_account=_CFG.get("use_default_broker_account", True),
            # Persist the MSAL cache to disk (DPAPI-encrypted on Windows). Without
            # this the cache is per-process, so every server restart re-prompts.
            cache_persistence_options=TokenCachePersistenceOptions(name="mcp-fabric"),
        )
        login_hint = _CFG.get("login_hint")
        if login_hint:
            broker_kwargs["login_hint"] = login_hint
        # The persisted cache is only usable if we can name the account it holds;
        # the AuthenticationRecord does that, and keeps the identity pinned to
        # login_hint instead of falling back to an account picker.
        record = None
        if _AUTH_RECORD.exists():
            try:
                record = AuthenticationRecord.deserialize(
                    _AUTH_RECORD.read_text(encoding="utf-8"))
                broker_kwargs["authentication_record"] = record
            except Exception:
                record = None
        _cred = InteractiveBrowserBrokerCredential(**broker_kwargs)
        if record is None:
            # First run (or a corrupt record): sign in once, then remember who.
            try:
                rec = _cred.authenticate(scopes=[SCOPE])
                _AUTH_RECORD.write_text(rec.serialize(), encoding="utf-8")
            except Exception:
                pass  # fall through; get_token() will surface the real error
    elif auth == "azure-cli":
        from azure.identity import AzureCliCredential
        _cred = AzureCliCredential(tenant_id=tenant) if tenant else AzureCliCredential()
    elif auth == "interactive":
        from azure.identity import InteractiveBrowserCredential
        _cred = InteractiveBrowserCredential(tenant_id=tenant)
    elif auth == "service-principal":
        from azure.identity import ClientSecretCredential
        _cred = ClientSecretCredential(
            tenant_id=_CFG["tenant_id"],
            client_id=_CFG["client_id"],
            client_secret=os.environ[_CFG["client_secret_env"]],
        )
    elif auth == "default":
        from azure.identity import DefaultAzureCredential
        _cred = DefaultAzureCredential()
    else:
        raise ValueError(f"Unknown auth type '{auth}'.")
    return _cred


def _headers(json_body: bool = False) -> dict:
    h = {"Authorization": f"Bearer {_credential().get_token(SCOPE).token}"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def is_writable() -> bool:
    return bool(_CFG.get("writable", True))


# --------------------------------------------------------------------------- #
# base64 helpers
# --------------------------------------------------------------------------- #
def b64(text_or_obj) -> str:
    if isinstance(text_or_obj, (dict, list)):
        raw = json.dumps(text_or_obj, separators=(",", ":"), ensure_ascii=False)
    else:
        raw = text_or_obj
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def unb64(payload: str) -> str:
    return base64.b64decode(payload).decode("utf-8")


# --------------------------------------------------------------------------- #
# REST core
# --------------------------------------------------------------------------- #
def _raw(method: str, path: str, *, params=None, json_body=None) -> requests.Response:
    url = path if path.startswith("http") else f"{BASE}{path}"
    return requests.request(
        method, url,
        headers=_headers(json_body is not None),
        params=params, json=json_body, timeout=_TIMEOUT,
    )


def request(method: str, path: str, *, params=None, json_body=None):
    """Call Fabric REST and return parsed JSON. Polls 202 long-running ops."""
    resp = _raw(method, path, params=params, json_body=json_body)
    if resp.status_code == 202:
        return _poll_lro(resp)
    if not resp.ok:
        raise RuntimeError(f"{resp.status_code} {resp.reason}: {resp.text}")
    if resp.content and resp.headers.get("Content-Type", "").startswith("application/json"):
        return resp.json()
    return {}


def _poll_lro(resp: requests.Response):
    op_url = resp.headers.get("Location")
    retry = int(resp.headers.get("Retry-After", 5) or 5)
    if not op_url:
        return {}
    waited = 0
    while True:
        time.sleep(retry)
        waited += retry
        r = requests.get(op_url, headers=_headers(), timeout=_TIMEOUT)
        if r.status_code == 202:
            retry = int(r.headers.get("Retry-After", retry) or retry)
            if waited > _LRO_MAX_WAIT:
                raise RuntimeError(f"LRO still running after {waited}s")
            continue
        if not r.ok:
            raise RuntimeError(f"LRO poll {r.status_code}: {r.text}")
        body = r.json() if r.content else {}
        status = body.get("status")
        if status is None:                       # response is already the result
            return body
        if status in ("Succeeded", "Completed"):
            rr = requests.get(op_url.rstrip("/") + "/result", headers=_headers(), timeout=_TIMEOUT)
            if rr.ok and rr.content:
                try:
                    return rr.json()
                except ValueError:
                    return body
            return body
        if status in ("Failed", "Cancelled", "Undefined"):
            raise RuntimeError(f"LRO {status}: {json.dumps(body)}")
        retry = int(r.headers.get("Retry-After", retry) or retry)
        if waited > _LRO_MAX_WAIT:
            raise RuntimeError(f"LRO timed out after {waited}s; last status {status}")


# --------------------------------------------------------------------------- #
# listing / resolution (handles continuationToken paging)
# --------------------------------------------------------------------------- #
def _get_all(path: str, params=None) -> list:
    out: list = []
    params = dict(params or {})
    while True:
        data = request("GET", path, params=params)
        out.extend(data.get("value", []))
        token = data.get("continuationToken")
        if not token:
            return out
        params["continuationToken"] = token


def list_workspaces() -> list:
    return _get_all("/workspaces")


def resolve_workspace(name_or_id: str) -> str:
    for w in list_workspaces():
        if w.get("id") == name_or_id or w.get("displayName") == name_or_id:
            return w["id"]
    raise ValueError(f"Workspace '{name_or_id}' not found (by id or display name).")


def list_items(ws_id: str, item_type: str | None = None) -> list:
    params = {"type": item_type} if item_type else None
    return _get_all(f"/workspaces/{ws_id}/items", params)


def resolve_item(ws_id: str, name_or_id: str, item_type: str | None = None) -> str:
    for it in list_items(ws_id, item_type):
        if it.get("id") == name_or_id or it.get("displayName") == name_or_id:
            return it["id"]
    raise ValueError(f"Item '{name_or_id}' (type={item_type}) not found in workspace.")


def list_folders(ws_id: str) -> list:
    return _get_all(f"/workspaces/{ws_id}/folders")


def resolve_folder(ws_id: str, name_or_id: str) -> str:
    matches = [f for f in list_folders(ws_id)
               if f.get("id") == name_or_id or f.get("displayName") == name_or_id]
    if not matches:
        raise ValueError(f"Folder '{name_or_id}' not found in workspace.")
    if len(matches) > 1:
        raise ValueError(
            f"Folder name '{name_or_id}' is ambiguous ({len(matches)} matches); "
            "pass the folder id instead.")
    return matches[0]["id"]


# --------------------------------------------------------------------------- #
# jobs (do NOT poll to completion - return the instance to monitor)
# --------------------------------------------------------------------------- #
def start_job(ws_id: str, item_id: str, job_type: str, execution_data: dict | None = None) -> dict:
    resp = _raw(
        "POST", f"/workspaces/{ws_id}/items/{item_id}/jobs/instances",
        params={"jobType": job_type},
        json_body={"executionData": execution_data or {}},
    )
    if resp.status_code not in (200, 201, 202):
        raise RuntimeError(f"{resp.status_code} {resp.reason}: {resp.text}")
    data = {}
    if resp.content:
        try:
            data = resp.json()
        except ValueError:
            data = {}
    job_id = None
    if isinstance(data, list) and data:
        job_id = data[0].get("jobInstanceId") or data[0].get("id")
    elif isinstance(data, dict):
        job_id = data.get("jobInstanceId") or data.get("id")
    loc = resp.headers.get("Location", "")
    if not job_id and loc:
        job_id = loc.rstrip("/").split("/")[-1]
    return {"jobInstanceId": job_id, "location": loc, "response": data}


def get_job(ws_id: str, item_id: str, job_instance_id: str) -> dict:
    return request("GET", f"/workspaces/{ws_id}/items/{item_id}/jobs/instances/{job_instance_id}")


def list_item_schedules(ws_id: str, item_id: str, job_type: str) -> list:
    """List the schedules configured on an item for a given job type
    (e.g. 'Pipeline' for Data pipelines, 'RunNotebook' for notebooks)."""
    return _get_all(f"/workspaces/{ws_id}/items/{item_id}/jobs/{job_type}/schedules")


def update_item_schedule(ws_id: str, item_id: str, job_type: str, schedule_id: str,
                         configuration: dict, enabled: bool) -> dict:
    """Update one existing schedule (PATCH). Fabric's Update Item Schedule API
    replaces the whole schedule, so the current `configuration` (recurrence)
    must be echoed back alongside the desired `enabled` flag."""
    body = {"enabled": enabled, "configuration": configuration}
    return request(
        "PATCH",
        f"/workspaces/{ws_id}/items/{item_id}/jobs/{job_type}/schedules/{schedule_id}",
        json_body=body,
    )


def cancel_job(ws_id: str, item_id: str, job_instance_id: str) -> dict:
    resp = _raw("POST", f"/workspaces/{ws_id}/items/{item_id}/jobs/instances/{job_instance_id}/cancel")
    return {"status_code": resp.status_code, "location": resp.headers.get("Location", "")}


# --------------------------------------------------------------------------- #
# content builders
# --------------------------------------------------------------------------- #
def ipynb_from_source(source: str, language: str = "python") -> dict:
    cells = [{
        "cell_type": "code",
        "source": source.splitlines(keepends=True) or [""],
        "execution_count": None,
        "outputs": [],
        "metadata": {},
    }]
    return {
        "nbformat": 4, "nbformat_minor": 5,
        "cells": cells,
        "metadata": {"language_info": {"name": language}},
    }


def source_from_ipynb(nb: dict) -> str:
    chunks = []
    for c in nb.get("cells", []):
        src = c.get("source", "")
        text = "".join(src) if isinstance(src, list) else src
        prefix = "" if c.get("cell_type") == "code" else "# [markdown]\n"
        chunks.append(prefix + text)
    return "\n\n# ---\n\n".join(chunks)


def dataflow_content(mashup_document: str, doc_locale: str = "en-US") -> dict:
    """Wrap a Power Query M document into the editingSessionMashup payload that
    the Create/Update Dataflow-with-definition API expects (dataflow-content.json)."""
    return {
        "editingSessionMashup": {
            "mashupName": "",
            "mashupDocument": mashup_document,
            "queryGroups": [],
            "documentLocale": doc_locale,
            "gatewayObjectId": None,
            "queriesMetadata": None,
            "connectionOverrides": [],
            "trustedConnections": None,
            "useHostConnectionProvider": False,
            "fastCombine": False,
            "allowNativeQueries": True,
            "allowedModules": None,
            "skipAutomaticTypeAndHeaderDetection": False,
            "disableAutoAnonymousConnectionUpsert": None,
            "hostProperties": {
                "DataflowRefreshOutputFileFormat": "Parquet",
                "EnableDateTimeFieldsForStaging": "true",
                "EnablePublishWithoutLoadedQueries": "true",
            },
            "defaultOutputDestinationConfiguration": None,
            "stagingDefinition": None,
        }
    }
