# mcp-fabric — Microsoft Fabric MCP server

A [FastMCP](https://github.com/jlowin/fastmcp) server for **developing Fabric
notebooks and Dataflow Gen2** through the Fabric REST API
(`https://api.fabric.microsoft.com/v1`). Tools take a `workspace` (display name
or GUID) and an item name/GUID; call `list_workspaces` first.

Read tools always work; create/update/delete/run tools require
`"writable": true` in `config.json`.

## Tools

**Discovery / read**
- `list_workspaces`
- `list_items` — filter by `item_type` (Notebook, Dataflow, Lakehouse, …)
- `list_folders` — workspace folders; pass a `displayName`/id as `folder` when creating items
- `get_item` — item metadata
- `get_item_definition` — decoded definition parts of any item (generic/advanced)

**Notebooks**
- `create_notebook` — from `source` (code string) or `ipynb` (full notebook JSON)
- `get_notebook` — returns the source extracted from the notebook's ipynb
- `update_notebook` — replace content
- `run_notebook` — run on demand with optional parameters → returns a jobInstanceId

**Dataflow Gen2**
- `create_dataflow` — from a Power Query M `mashup_document`
- `get_dataflow` — decoded parts (`mashup.pq` = the M query, `queryMetadata.json`)
- `update_dataflow` — replace the M document
- `refresh_dataflow` / `publish_dataflow` — on-demand jobs

**Schedules**
- `get_item_schedules` — schedules on an item: id, `enabled`, and recurrence
  (Cron/Daily/Weekly, start/end, timezone). An empty list means the item only
  runs on demand or via a parent pipeline.
- `set_schedule_enabled` — flip one schedule's `enabled` flag by id
- `disable_item_schedules` — flip **every** schedule on an item (`enable=True`
  to re-enable); schedules already in the target state are skipped

> `job_type` selects which schedules you're looking at: `Pipeline` for data
> pipelines (default), `RunNotebook` for notebooks, `Refresh` for dataflows.
> Fabric's Update Schedule API replaces the whole schedule, so both write tools
> read the existing recurrence and echo it back — only the `enabled` flag
> changes.

**Jobs / lifecycle**
- `get_job` — status of a notebook run / dataflow refresh
- `cancel_job`
- `move_item` — move an item into a folder (or to the workspace root); children follow
- `delete_item`

> Definition create/update are long-running operations; the server polls them to
> completion automatically. Notebook/dataflow runs return a `jobInstanceId` you
> monitor with `get_job` (they aren't polled to completion).

## Known limitations (per Microsoft docs)

- **Dataflow Gen2 run APIs**: refresh/publish can be invoked, but Microsoft
  currently notes the run may not complete successfully via API.
- **Service-principal auth is not supported for dataflows** (works for notebooks).

## Auth

Set `"auth"` in `config.json`:

| value | how it signs in |
|-------|-----------------|
| `broker` *(default)* | Windows WAM broker popup (no Azure CLI needed) |
| `azure-cli` | reuse an `az login` token |
| `interactive` | browser sign-in popup |
| `service-principal` | app registration; secret from `client_secret_env` (see `.env.example`) |
| `default` | `DefaultAzureCredential` |

The identity needs an appropriate **workspace role** (Admin/Member/Contributor)
to create and run items. Default token scope is
`https://api.fabric.microsoft.com/.default`.

Two extra `config.json` keys tune `broker` sign-in when the machine has more
than one work account signed in:

| key | default | effect |
|-----|---------|--------|
| `use_default_broker_account` | `true` | Silently reuse the Windows default account. Set `false` to always get the account picker. |
| `login_hint` | *(unset)* | Pre-select this UPN/email, so the broker picks the right identity instead of whichever is default. |

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy config.example.json config.json   # then edit if needed (broker auth works as-is)
.\.venv\Scripts\python.exe server.py    # smoke test (Ctrl+C to stop)
```

## Register with an MCP client

See `examples/mcp.json`:

```json
{
  "mcpServers": {
    "fabric": {
      "command": "C:\\path\\to\\mcp-fabric\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\mcp-fabric\\server.py"],
      "env": {}
    }
  }
}
```

## Use with Claude Desktop

[Claude Desktop](https://claude.ai/download) reads its MCP servers from
`claude_desktop_config.json`. Open it from **Settings → Developer → Edit Config**
(this creates the file if it doesn't exist), or edit it directly:

- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

Add this server under `mcpServers`, using **absolute paths** to the venv's
Python and `server.py`:

```json
{
  "mcpServers": {
    "fabric": {
      "command": "C:\\path\\to\\mcp-fabric\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\mcp-fabric\\server.py"],
      "env": {}
    }
  }
}
```

On macOS the paths are POSIX, e.g. `"command": "/Users/you/mcp-fabric/.venv/bin/python"`.
Save the file and **fully quit and reopen Claude Desktop** (use *Quit* from the
tray/menu-bar icon — closing the window isn't enough). The server's tools then
appear in the tools (🔌) menu of a new chat.

## License

MIT — see [LICENSE](LICENSE).
