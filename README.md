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

**Jobs / lifecycle**
- `get_job` — status of a notebook run / dataflow refresh
- `cancel_job`
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
