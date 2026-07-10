"""fabric - MCP server for developing Microsoft Fabric notebooks and dataflows.

Tools take a `workspace` (display name or GUID) and item name/GUID. Call
`list_workspaces` first to discover what you can reach.

Read tools work always. Tools that create/update/delete items or run jobs
require "writable": true in config.json.

Run standalone:  python server.py
"""
from __future__ import annotations

import json

from fastmcp import FastMCP

import fabric

mcp = FastMCP("fabric")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _dump(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


def _err(exc: Exception) -> str:
    return f"Fabric API error: {exc}"


def _deny() -> str | None:
    if not fabric.is_writable():
        return ('Refused: this server is read-only. Set "writable": true in '
                "config.json to allow create/update/delete/run actions.")
    return None


# --------------------------------------------------------------------------- #
# discovery / read
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_workspaces() -> str:
    """List Fabric workspaces the signed-in identity can access (id, displayName)."""
    try:
        out = [{"id": w.get("id"), "displayName": w.get("displayName"),
                "capacityId": w.get("capacityId")} for w in fabric.list_workspaces()]
        return _dump(out)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def list_items(workspace: str, item_type: str | None = None) -> str:
    """List items in a workspace (id, type, displayName). Optionally filter by
    item_type, e.g. Notebook, Dataflow, Lakehouse, DataPipeline, SemanticModel."""
    try:
        ws = fabric.resolve_workspace(workspace)
        out = [{"id": it.get("id"), "type": it.get("type"),
                "displayName": it.get("displayName"), "description": it.get("description")}
               for it in fabric.list_items(ws, item_type)]
        return _dump(out)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def list_folders(workspace: str) -> str:
    """List the folders in a workspace (id, displayName, parentFolderId).
    Use a folder's displayName or id as the `folder` argument when creating items."""
    try:
        ws = fabric.resolve_workspace(workspace)
        out = [{"id": f.get("id"), "displayName": f.get("displayName"),
                "parentFolderId": f.get("parentFolderId")} for f in fabric.list_folders(ws)]
        return _dump(out)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def get_item(workspace: str, item: str, item_type: str | None = None) -> str:
    """Return metadata for a single item (by display name or GUID)."""
    try:
        ws = fabric.resolve_workspace(workspace)
        item_id = fabric.resolve_item(ws, item, item_type)
        return _dump(fabric.request("GET", f"/workspaces/{ws}/items/{item_id}"))
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def get_item_definition(workspace: str, item: str, item_type: str | None = None,
                        fmt: str | None = None) -> str:
    """Return the decoded definition parts of any item (advanced/generic).
    `fmt` is the optional definition format (e.g. 'ipynb' for notebooks)."""
    try:
        ws = fabric.resolve_workspace(workspace)
        item_id = fabric.resolve_item(ws, item, item_type)
        params = {"format": fmt} if fmt else None
        data = fabric.request("POST", f"/workspaces/{ws}/items/{item_id}/getDefinition", params=params)
        return _dump(_decode_parts(data))
    except Exception as exc:
        return _err(exc)


def _decode_parts(definition_response: dict) -> dict:
    parts = definition_response.get("definition", {}).get("parts", [])
    decoded = {}
    for p in parts:
        path = p.get("path")
        if path == ".platform":
            continue
        try:
            decoded[path] = fabric.unb64(p.get("payload", ""))
        except Exception:
            decoded[path] = "<could not decode>"
    return decoded


# --------------------------------------------------------------------------- #
# notebooks
# --------------------------------------------------------------------------- #
@mcp.tool()
def create_notebook(workspace: str, name: str, source: str | None = None,
                    ipynb: str | None = None, language: str = "python",
                    description: str | None = None, folder: str | None = None) -> str:
    """Create a Fabric notebook. Provide EITHER `source` (code as a string, wrapped
    into one cell) OR `ipynb` (full notebook JSON as a string). language is one of
    python/pyspark/sparksql/scala/sparkr. Optional `folder` places it in a workspace
    folder (display name or id). Requires a writable server."""
    deny = _deny()
    if deny:
        return deny
    try:
        ws = fabric.resolve_workspace(workspace)
        if ipynb:
            nb = json.loads(ipynb)
        else:
            nb = fabric.ipynb_from_source(source or "", language)
        body = {"displayName": name, "type": "Notebook",
                "definition": {"format": "ipynb", "parts": [
                    {"path": "artifact.content.ipynb", "payload": fabric.b64(nb),
                     "payloadType": "InlineBase64"}]}}
        if description:
            body["description"] = description
        if folder:
            body["folderId"] = fabric.resolve_folder(ws, folder)
        fabric.request("POST", f"/workspaces/{ws}/items", json_body=body)
        nb_id = fabric.resolve_item(ws, name, "Notebook")
        return _dump({"workspaceId": ws, "notebookId": nb_id, "displayName": name,
                      "folderId": body.get("folderId")})
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def get_notebook(workspace: str, notebook: str) -> str:
    """Return a notebook's source code (extracted from its ipynb definition)."""
    try:
        ws = fabric.resolve_workspace(workspace)
        nb_id = fabric.resolve_item(ws, notebook, "Notebook")
        data = fabric.request("POST", f"/workspaces/{ws}/items/{nb_id}/getDefinition",
                              params={"format": "ipynb"})
        parts = _decode_parts(data)
        nb_json = parts.get("artifact.content.ipynb") or parts.get("notebook-content.ipynb")
        source = ""
        if nb_json:
            try:
                source = fabric.source_from_ipynb(json.loads(nb_json))
            except Exception:
                source = nb_json
        return _dump({"workspaceId": ws, "notebookId": nb_id, "source": source})
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def update_notebook(workspace: str, notebook: str, source: str | None = None,
                    ipynb: str | None = None, language: str = "python") -> str:
    """Replace a notebook's content. Provide EITHER `source` or `ipynb`.
    Requires a writable server."""
    deny = _deny()
    if deny:
        return deny
    try:
        ws = fabric.resolve_workspace(workspace)
        nb_id = fabric.resolve_item(ws, notebook, "Notebook")
        nb = json.loads(ipynb) if ipynb else fabric.ipynb_from_source(source or "", language)
        body = {"definition": {"format": "ipynb", "parts": [
            {"path": "artifact.content.ipynb", "payload": fabric.b64(nb),
             "payloadType": "InlineBase64"}]}}
        fabric.request("POST", f"/workspaces/{ws}/items/{nb_id}/updateDefinition", json_body=body)
        return _dump({"workspaceId": ws, "notebookId": nb_id, "updated": True})
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def run_notebook(workspace: str, notebook: str, parameters_json: str | None = None) -> str:
    """Run a notebook on demand. Optional `parameters_json` is a JSON object of
    notebook parameters in Fabric form, e.g. {"p1": {"value": "x", "type": "string"}}.
    Returns a jobInstanceId to monitor with get_job. Requires a writable server."""
    deny = _deny()
    if deny:
        return deny
    try:
        ws = fabric.resolve_workspace(workspace)
        nb_id = fabric.resolve_item(ws, notebook, "Notebook")
        exec_data = {}
        if parameters_json:
            exec_data["parameters"] = json.loads(parameters_json)
        return _dump(fabric.start_job(ws, nb_id, "RunNotebook", exec_data))
    except Exception as exc:
        return _err(exc)


# --------------------------------------------------------------------------- #
# dataflows (Dataflow Gen2)
# --------------------------------------------------------------------------- #
@mcp.tool()
def create_dataflow(workspace: str, name: str, mashup_document: str,
                    description: str | None = None) -> str:
    """Create a Dataflow Gen2 from a Power Query M document (a `section Section1; ...`
    mashup string). Requires a writable server."""
    deny = _deny()
    if deny:
        return deny
    try:
        ws = fabric.resolve_workspace(workspace)
        content = fabric.dataflow_content(mashup_document)
        body = {"displayName": name, "type": "Dataflow",
                "definition": {"parts": [
                    {"path": "dataflow-content.json", "payload": fabric.b64(content),
                     "payloadType": "InlineBase64"}]}}
        if description:
            body["description"] = description
        fabric.request("POST", f"/workspaces/{ws}/items", json_body=body)
        df_id = fabric.resolve_item(ws, name, "Dataflow")
        return _dump({"workspaceId": ws, "dataflowId": df_id, "displayName": name})
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def get_dataflow(workspace: str, dataflow: str) -> str:
    """Return a dataflow's decoded definition parts (mashup.pq = the M query,
    queryMetadata.json = query options)."""
    try:
        ws = fabric.resolve_workspace(workspace)
        df_id = fabric.resolve_item(ws, dataflow, "Dataflow")
        data = fabric.request("POST", f"/workspaces/{ws}/items/{df_id}/getDefinition")
        return _dump({"workspaceId": ws, "dataflowId": df_id, "parts": _decode_parts(data)})
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def update_dataflow(workspace: str, dataflow: str, mashup_document: str) -> str:
    """Replace a dataflow's Power Query M document. Requires a writable server."""
    deny = _deny()
    if deny:
        return deny
    try:
        ws = fabric.resolve_workspace(workspace)
        df_id = fabric.resolve_item(ws, dataflow, "Dataflow")
        content = fabric.dataflow_content(mashup_document)
        body = {"definition": {"parts": [
            {"path": "dataflow-content.json", "payload": fabric.b64(content),
             "payloadType": "InlineBase64"}]}}
        fabric.request("POST", f"/workspaces/{ws}/items/{df_id}/updateDefinition", json_body=body)
        return _dump({"workspaceId": ws, "dataflowId": df_id, "updated": True})
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def refresh_dataflow(workspace: str, dataflow: str) -> str:
    """Trigger an on-demand dataflow refresh (jobType=Refresh). Returns a
    jobInstanceId. NOTE: Microsoft currently documents that Dataflow Gen2 run
    APIs can be invoked but the run may not complete successfully. Requires a
    writable server."""
    deny = _deny()
    if deny:
        return deny
    try:
        ws = fabric.resolve_workspace(workspace)
        df_id = fabric.resolve_item(ws, dataflow, "Dataflow")
        exec_data = {"DataflowName": dataflow}
        return _dump(fabric.start_job(ws, df_id, "Refresh", exec_data))
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def publish_dataflow(workspace: str, dataflow: str) -> str:
    """Publish a dataflow's saved definition (jobType=Publish) so it becomes
    runnable. Returns a jobInstanceId. Requires a writable server."""
    deny = _deny()
    if deny:
        return deny
    try:
        ws = fabric.resolve_workspace(workspace)
        df_id = fabric.resolve_item(ws, dataflow, "Dataflow")
        return _dump(fabric.start_job(ws, df_id, "Publish", {"DataflowName": dataflow}))
    except Exception as exc:
        return _err(exc)


# --------------------------------------------------------------------------- #
# job monitoring (notebooks + dataflows)
# --------------------------------------------------------------------------- #
@mcp.tool()
def get_job(workspace: str, item: str, job_instance_id: str,
            item_type: str | None = None) -> str:
    """Get the status of a job instance (notebook run / dataflow refresh):
    status, start/end time, failure reason."""
    try:
        ws = fabric.resolve_workspace(workspace)
        item_id = fabric.resolve_item(ws, item, item_type)
        return _dump(fabric.get_job(ws, item_id, job_instance_id))
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def get_item_schedules(workspace: str, item: str, job_type: str = "Pipeline",
                       item_type: str | None = None) -> str:
    """List the schedules configured on an item. `job_type` is the Fabric job
    type to inspect: 'Pipeline' for Data pipelines (default), 'RunNotebook' for
    notebooks, 'Refresh' for dataflows. Returns each schedule's id, enabled flag,
    and recurrence configuration (Cron/Daily/Weekly, start/end, timezone). An
    empty list means the item has no schedule for that job type (runs on demand
    or via a parent pipeline only)."""
    try:
        ws = fabric.resolve_workspace(workspace)
        item_id = fabric.resolve_item(ws, item, item_type)
        schedules = fabric.list_item_schedules(ws, item_id, job_type)
        out = [{"id": s.get("id"), "enabled": s.get("enabled"),
                "createdDateTime": s.get("createdDateTime"),
                "configuration": s.get("configuration"),
                "owner": (s.get("owner") or {}).get("id")}
               for s in schedules]
        return _dump({"workspaceId": ws, "itemId": item_id, "jobType": job_type,
                      "scheduleCount": len(out), "schedules": out})
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def cancel_job(workspace: str, item: str, job_instance_id: str,
               item_type: str | None = None) -> str:
    """Cancel a running job instance. Requires a writable server."""
    deny = _deny()
    if deny:
        return deny
    try:
        ws = fabric.resolve_workspace(workspace)
        item_id = fabric.resolve_item(ws, item, item_type)
        return _dump(fabric.cancel_job(ws, item_id, job_instance_id))
    except Exception as exc:
        return _err(exc)


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #
@mcp.tool()
def move_item(workspace: str, item: str, target_folder: str | None = None,
              item_type: str | None = None) -> str:
    """Move an item to a folder within the same workspace. `target_folder` is a
    folder display name or id; omit it to move the item to the workspace root.
    Child items move with their parent. Requires a writable server."""
    deny = _deny()
    if deny:
        return deny
    try:
        ws = fabric.resolve_workspace(workspace)
        item_id = fabric.resolve_item(ws, item, item_type)
        body = {}
        if target_folder:
            body["targetFolderId"] = fabric.resolve_folder(ws, target_folder)
        return _dump(fabric.request("POST", f"/workspaces/{ws}/items/{item_id}/move",
                                    json_body=body))
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def delete_item(workspace: str, item: str, item_type: str | None = None) -> str:
    """Delete an item (notebook, dataflow, ...). Requires a writable server."""
    deny = _deny()
    if deny:
        return deny
    try:
        ws = fabric.resolve_workspace(workspace)
        item_id = fabric.resolve_item(ws, item, item_type)
        fabric.request("DELETE", f"/workspaces/{ws}/items/{item_id}")
        return _dump({"workspaceId": ws, "itemId": item_id, "deleted": True})
    except Exception as exc:
        return _err(exc)


if __name__ == "__main__":
    mcp.run()
