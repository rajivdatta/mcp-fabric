"""Read-only SQL access to Fabric Lakehouses, Warehouses and Mirrored databases.

Added 2026-10-08. Each of those items exposes a TDS endpoint (the lakehouse's
"SQL analytics endpoint", the warehouse itself, a mirror's SQL endpoint). This
module finds the endpoint through the Fabric REST API, signs in with the SAME
credential the rest of the server uses (scope https://database.windows.net), and
runs queries through pyodbc + ODBC Driver 18.

READ-ONLY, three layers deep - any one of them is enough on its own:
  1. check_read_only(): one statement, starting SELECT/WITH, no DML/DDL/EXEC/INTO
     keyword outside string literals and comments;
  2. the connection is opened with ApplicationIntent=ReadOnly;
  3. autocommit is OFF and every query is ROLLED BACK afterwards.
A lakehouse SQL endpoint is read-only by design anyway; a Warehouse is not,
which is why the checks exist.

Also: OneLake directory listing (list_onelake), which sees shortcuts and new
tables before the SQL endpoint's metadata sync does.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import re
import struct
import threading
import time
import uuid

import requests

import fabric

SQL_SCOPE = "https://database.windows.net/.default"
STORAGE_SCOPE = "https://storage.azure.com/.default"
ONELAKE = "https://onelake.dfs.fabric.microsoft.com"
DRIVER = "ODBC Driver 18 for SQL Server"
MAX_ROWS_CAP = 5000
QUERY_TIMEOUT = 180          # seconds
_SQL_COPT_SS_ACCESS_TOKEN = 1256

# item type -> (REST collection, how to find the TDS server in its properties)
_SQL_ITEMS = {
    "Lakehouse": ("lakehouses", lambda p: (p.get("sqlEndpointProperties") or {}).get("connectionString")),
    "Warehouse": ("warehouses", lambda p: p.get("connectionString")),
    "MirroredDatabase": ("mirroredDatabases",
                         lambda p: (p.get("sqlEndpointProperties") or {}).get("connectionString")),
}

_lock = threading.Lock()
_conns: dict = {}            # (server, database) -> (connection, token expiry epoch)
_endpoint_cache: dict = {}   # (ws_id, item) -> endpoint dict


# --------------------------------------------------------------------------- #
# endpoint resolution
# --------------------------------------------------------------------------- #
def resolve_endpoint(workspace: str, item: str, item_type: str | None = None) -> dict:
    """{workspace_id, item_id, item_type, name, server, database, sql_endpoint_id}.

    `item` is a display name or GUID. Without item_type, Lakehouse beats
    Warehouse beats MirroredDatabase - a lakehouse and its SQL endpoint share a
    display name, and the lakehouse is what people mean.
    """
    ws = fabric.resolve_workspace(workspace)
    key = (ws, item, item_type)
    if key in _endpoint_cache:
        return _endpoint_cache[key]
    types = [item_type] if item_type else list(_SQL_ITEMS)
    for t in types:
        if t not in _SQL_ITEMS:
            raise ValueError(f"item_type must be one of {list(_SQL_ITEMS)}, got {t!r}")
        found = [i for i in fabric.list_items(ws, t)
                 if i.get("id") == item or i.get("displayName") == item]
        if not found:
            continue
        it = found[0]
        coll, server_of = _SQL_ITEMS[t]
        props = (fabric.request("GET", f"/workspaces/{ws}/{coll}/{it['id']}") or {}).get("properties") or {}
        server = server_of(props)
        if not server:
            raise ValueError(f"{t} {it['displayName']!r} has no SQL endpoint yet "
                             "(still provisioning, or not supported for this item).")
        ep = {"workspace_id": ws, "item_id": it["id"], "item_type": t,
              "name": it["displayName"], "server": server, "database": it["displayName"],
              "sql_endpoint_id": (props.get("sqlEndpointProperties") or {}).get("id"),
              "default_schema": props.get("defaultSchema")}
        _endpoint_cache[key] = ep
        return ep
    raise ValueError(f"No Lakehouse / Warehouse / MirroredDatabase named {item!r} in "
                     f"workspace {workspace!r}.")


# --------------------------------------------------------------------------- #
# connections
# --------------------------------------------------------------------------- #
def _connect(server: str, database: str):
    import pyodbc
    tok = fabric._credential().get_token(SQL_SCOPE)
    raw = tok.token.encode("utf-16-le")
    attrs = {_SQL_COPT_SS_ACCESS_TOKEN: struct.pack(f"<I{len(raw)}s", len(raw), raw)}
    cs = (f"Driver={{{DRIVER}}};Server={server},1433;Database={database};"
          "Encrypt=yes;TrustServerCertificate=no;ApplicationIntent=ReadOnly;"
          "Connection Timeout=60")
    cn = pyodbc.connect(cs, attrs_before=attrs, autocommit=False)
    cn.timeout = QUERY_TIMEOUT
    return cn, tok.expires_on


def _connection(ep: dict):
    k = (ep["server"], ep["database"])
    with _lock:
        cn, exp = _conns.get(k, (None, 0))
        if cn is None or exp - time.time() < 300:          # token about to lapse
            if cn is not None:
                try:
                    cn.close()
                except Exception:
                    pass
            cn, exp = _connect(*k)
            _conns[k] = (cn, exp)
        return cn


def _drop(ep: dict):
    with _lock:
        cn, _ = _conns.pop((ep["server"], ep["database"]), (None, 0))
    if cn is not None:
        try:
            cn.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# read-only guard
# --------------------------------------------------------------------------- #
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|exec|execute|grant|"
    r"revoke|deny|into|bulk|copy|openrowset|opendatasource|openquery|backup|restore|"
    r"dbcc|kill|shutdown|reconfigure|use|declare|set|waitfor|sp_\w+|xp_\w+)\b", re.I)


def _strip(sql: str) -> str:
    """Remove comments and the CONTENTS of string literals / bracketed names."""
    s = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    s = re.sub(r"--[^\n]*", " ", s)
    s = re.sub(r"'(?:[^']|'')*'", "''", s)
    s = re.sub(r"\[[^\]]*\]", "[x]", s)
    s = re.sub(r'"[^"]*"', '"x"', s)
    return s


def check_read_only(sql: str) -> str:
    """Return the cleaned statement, or raise ValueError explaining the refusal."""
    s = _strip(sql).strip()
    s = s.rstrip(";").strip()
    if not s:
        raise ValueError("empty query")
    if ";" in s:
        raise ValueError("one statement per call - remove the ';'")
    if not re.match(r"(?is)^(select|with)\b", s):
        raise ValueError("read-only: the query must start with SELECT or WITH")
    m = _FORBIDDEN.search(s)
    if m:
        raise ValueError(f"read-only: keyword {m.group(1).upper()!r} is not allowed "
                         "(SELECT ... INTO, DML, DDL and EXEC are all refused)")
    return sql.strip().rstrip(";")


def _jsonable(v):
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        f = float(v)                       # a number when lossless, else the exact string
        return f if decimal.Decimal(repr(f)) == v else str(v)
    if isinstance(v, (bytes, bytearray)):
        return "0x" + v.hex()[:64] + ("..." if len(v) > 32 else "")
    if isinstance(v, uuid.UUID):
        return str(v)
    return v


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def query(workspace: str, item: str, sql: str, max_rows: int = 200,
          item_type: str | None = None) -> dict:
    sql = check_read_only(sql)
    max_rows = max(1, min(int(max_rows), MAX_ROWS_CAP))
    ep = resolve_endpoint(workspace, item, item_type)
    t0 = time.time()
    for attempt in (1, 2):
        cn = _connection(ep)
        cur = cn.cursor()
        try:
            cur.execute(sql)
            cols = [d[0] for d in (cur.description or [])]
            rows = cur.fetchmany(max_rows + 1) if cols else []
            truncated = len(rows) > max_rows
            rows = [[_jsonable(v) for v in r] for r in rows[:max_rows]]
            break
        except Exception as exc:
            msg = str(exc)
            # a dropped / expired session: reconnect once
            if attempt == 1 and any(k in msg for k in ("08S01", "08001", "Communication link",
                                                       "token", "Login failed", "closed")):
                _drop(ep)
                continue
            raise
        finally:
            try:
                cn.rollback()              # layer 3: nothing this tool runs is ever committed
            except Exception:
                pass
            cur.close()
    return {"endpoint": f"{ep['item_type']} {ep['name']}", "server": ep["server"],
            "columns": cols, "rows": rows, "row_count": len(rows), "truncated": truncated,
            "max_rows": max_rows, "elapsed_s": round(time.time() - t0, 2)}


def list_tables(workspace: str, item: str, schema: str | None = None,
                item_type: str | None = None) -> dict:
    sql = ("SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
           "WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA', 'queryinsights')")
    if schema:
        sql += " AND TABLE_SCHEMA = '" + schema.replace("'", "''") + "'"
    sql += " ORDER BY TABLE_SCHEMA, TABLE_NAME"
    return query(workspace, item, sql, MAX_ROWS_CAP, item_type)


def describe_table(workspace: str, item: str, table: str, row_count: bool = True,
                   item_type: str | None = None) -> dict:
    """`table` is 'schema.table' or 'table' (default schema dbo)."""
    sch, _, tbl = table.rpartition(".")
    sch = (sch or "dbo").strip("[]")
    tbl = tbl.strip("[]")
    q = lambda s: s.replace("'", "''")
    cols = query(workspace, item,
                 "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, "
                 "NUMERIC_SCALE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "
                 f"WHERE TABLE_SCHEMA = '{q(sch)}' AND TABLE_NAME = '{q(tbl)}' "
                 "ORDER BY ORDINAL_POSITION", MAX_ROWS_CAP, item_type)
    if not cols["rows"]:
        raise ValueError(f"{sch}.{tbl} not found on the SQL endpoint. Names are "
                         "case-sensitive here, and a table written in the last few minutes "
                         "may need refresh_sql_endpoint first.")
    out = {"table": f"{sch}.{tbl}", "endpoint": cols["endpoint"],
           "columns": [dict(zip(cols["columns"], r)) for r in cols["rows"]]}
    if row_count:
        n = query(workspace, item,
                  f"SELECT COUNT_BIG(*) AS n FROM [{sch.replace(']', ']]')}].[{tbl.replace(']', ']]')}]",
                  1, item_type)
        out["row_count"] = n["rows"][0][0]
    return out


def refresh_endpoint(workspace: str, lakehouse: str) -> dict:
    """Ask a lakehouse SQL endpoint to re-sync its metadata with OneLake (new tables,
    path-written tables, new shortcuts). Metadata only - changes no data."""
    ep = resolve_endpoint(workspace, lakehouse, "Lakehouse")
    if not ep["sql_endpoint_id"]:
        raise ValueError("this lakehouse has no SQL endpoint id")
    res = fabric.request("POST", f"/workspaces/{ep['workspace_id']}/sqlEndpoints/"
                                 f"{ep['sql_endpoint_id']}/refreshMetadata",
                         params={"preview": "true"}, json_body={})
    return {"lakehouse": ep["name"], "sql_endpoint_id": ep["sql_endpoint_id"], "result": res}


def list_onelake(workspace: str, item: str, path: str = "Tables",
                 item_type: str | None = None) -> dict:
    """Immediate children of a OneLake folder inside a Lakehouse/Warehouse/Mirror,
    e.g. path='Tables' (schemas), 'Tables/jde_prod' (tables), 'Files/raw'."""
    ep = resolve_endpoint(workspace, item, item_type)
    tok = fabric._credential().get_token(STORAGE_SCOPE).token
    r = requests.get(f"{ONELAKE}/{ep['workspace_id']}", timeout=120,
                     headers={"Authorization": f"Bearer {tok}"},
                     params={"resource": "filesystem", "recursive": "false",
                             "directory": f"{ep['item_id']}/{path.strip('/')}"})
    if r.status_code == 404:
        raise ValueError(f"{path!r} does not exist in {ep['name']} (OneLake paths are "
                         "case-sensitive: schema folders keep their case, table folders "
                         "are lower-case)")
    r.raise_for_status()
    kids = [{"name": p["name"].split("/")[-1],
             "is_directory": str(p.get("isDirectory", "false")).lower() == "true",
             "last_modified": p.get("lastModified")}
            for p in r.json().get("paths", [])]
    return {"item": ep["name"], "path": path, "children": sorted(kids, key=lambda k: k["name"])}
