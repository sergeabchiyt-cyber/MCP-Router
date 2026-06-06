import os
import json
import httpx
import libsql_experimental as libsql
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

# ── Config ─────────────────────────────────────────────────────────────────

API_KEY = os.environ["ROUTER_API_KEY"]
TURSO_URL = os.environ["TURSO_DATABASE_URL"]
TURSO_TOKEN = os.environ["TURSO_AUTH_TOKEN"]
HF_TERMINAL_URL = os.environ["HF_TERMINAL_URL"]
HF_TERMINAL_API_KEY = os.environ["HF_TERMINAL_API_KEY"]
PORT = int(os.environ.get("PORT", 8000))

DB_TOTAL_GB = 5.0
DB_WARN_PCT = 80
DB_BLOCK_PCT = 90
DB_HARD_PCT = 95

# ── DB ─────────────────────────────────────────────────────────────────────

def get_conn():
    return libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)

def db_usage() -> dict:
    conn = get_conn()
    result = conn.execute(
        "SELECT page_count * page_size as used_bytes "
        "FROM pragma_page_count(), pragma_page_size()"
    ).fetchone()
    used_bytes = result[0]
    used_gb = used_bytes / (1024**3)
    percent = (used_gb / DB_TOTAL_GB) * 100
    if percent < DB_WARN_PCT:
        status = "healthy"
    elif percent < DB_BLOCK_PCT:
        status = "warning"
    elif percent < DB_HARD_PCT:
        status = "critical"
    else:
        status = "blocked"
    return {
        "used_gb": round(used_gb, 3),
        "total_gb": DB_TOTAL_GB,
        "percent": round(percent, 1),
        "status": status,
    }

# ── MCP ────────────────────────────────────────────────────────────────────

mcp = FastMCP("Hermes Router", host="0.0.0.0", port=PORT)

# ── DB tools ───────────────────────────────────────────────────────────────

@mcp.tool()
def db_write(query: str, params: str = "[]") -> str:
    """Execute a DB write. Checks usage before writing."""
    usage = db_usage()
    if usage["status"] == "blocked":
        return json.dumps({"ok": False, "error": "DB critical — archive cold data to TG-S3 first", "db_usage": usage})
    if usage["status"] == "critical":
        return json.dumps({"ok": False, "error": "DB at 90%+ — archive required before writing", "db_usage": usage})
    try:
        conn = get_conn()
        p = tuple(json.loads(params))
        conn.execute(query, p)
        conn.commit()
        usage = db_usage()
        return json.dumps({"ok": True, "db_usage": usage})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@mcp.tool()
def db_read(query: str, params: str = "[]") -> str:
    """Execute a DB read query. Always returns current DB usage."""
    try:
        conn = get_conn()
        p = tuple(json.loads(params))
        rows = conn.execute(query, p).fetchall()
        usage = db_usage()
        return json.dumps({"ok": True, "rows": rows, "db_usage": usage})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@mcp.tool()
def db_status() -> str:
    """Get current Turso DB usage stats."""
    try:
        return json.dumps(db_usage())
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

# ── Terminal proxy ──────────────────────────────────────────────────────────

@mcp.tool()
def terminal(cmd: str, cwd: str = "/tmp/workspace", timeout: int = 300) -> str:
    """Run a shell command on HF Spaces terminal server."""
    try:
        with httpx.Client(timeout=timeout + 10) as client:
            response = client.post(
                f"{HF_TERMINAL_URL}/api/terminal",
                headers={
                    "X-API-Key": HF_TERMINAL_API_KEY,
                    "Content-Type": "application/json",
                },
                json={"cmd": cmd, "cwd": cwd, "timeout": timeout},
            )
            return response.text
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

# ── Health ──────────────────────────────────────────────────────────────────

@mcp.tool()
def health() -> str:
    """Router health check including DB status."""
    try:
        usage = db_usage()
        return json.dumps({"status": "ok", "db_usage": usage})
    except Exception as e:
        return json.dumps({"status": "degraded", "error": str(e)})

@mcp.custom_route("/health", methods=["GET"])
async def health_http(request: Request) -> JSONResponse:
    try:
        usage = db_usage()
        return JSONResponse({"status": "ok", "db_usage": usage})
    except Exception as e:
        return JSONResponse({"status": "degraded", "error": str(e)}, status_code=200)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")