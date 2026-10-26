"""AeroNexus MCP server: exposes the enterprise integration layer as Model Context Protocol tools.

Any MCP client (Claude Desktop, Claude Code, Cursor, an in-house agent) can search the knowledge base, read the mock SAP
procurement data, check engine health and *request* purchase requisitions. Requests are never executed by the MCP client:
they land in the AeroNexus approval queue and wait for a named human approver in the Mission Control dashboard.

The server is a thin proxy over the AeroNexus REST API, so it works the same against localhost or a Railway deployment,
and every call goes through the same RBAC, audit log and Sheets tracking as the web app.

    python -m aeronexus.integrations.mcp_server                    # stdio (Claude Desktop / Claude Code)
    python -m aeronexus.integrations.mcp_server --http --port 8765 # streamable HTTP

Environment:
    AERONEXUS_API_URL   default http://localhost:8000
    AERONEXUS_MCP_USER  identity sent to the API (default "mcp-agent")
    AERONEXUS_MCP_ROLE  viewer | engineer (default engineer). Approver/admin are refused: MCP clients can't approve.
    AERONEXUS_FF_MCP    set 0 to refuse to start

Works with the official `mcp` Python SDK v2 (MCPServer) and v1 (FastMCP).
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import httpx

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]

INSTRUCTIONS = ("AeroNexus AI: aerospace engineering data and procurement. Read tools are safe. "
                "sap_request_purchase_requisition only creates a pending approval; a human approves it in AeroNexus.")

_transport: httpx.AsyncBaseTransport | None = None  # tests inject an ASGI transport to call the app in-process


def use_transport(t: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = t


def _headers() -> dict[str, str]:
    role = os.environ.get("AERONEXUS_MCP_ROLE", "engineer").lower()
    if role not in ("viewer", "engineer"):
        role = "engineer"  # least privilege: an MCP client is an agent and can never hold approval rights
    return {"x-user": os.environ.get("AERONEXUS_MCP_USER", "mcp-agent")[:64], "x-role": role,
            "x-tenant": os.environ.get("AERONEXUS_TENANT", "demo")}


async def _call(method: str, path: str, **kw: Any) -> Any:
    base = os.environ.get("AERONEXUS_API_URL", "http://localhost:8000").rstrip("/")
    async with httpx.AsyncClient(base_url=base if _transport is None else "http://aeronexus", transport=_transport,
                                 headers=_headers(), timeout=30) as c:
        r = await c.request(method, path, **kw)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        return {"error": f"HTTP {r.status_code}", "detail": str(detail)[:500]}
    return r.json()


def _trim(rows: Any, n: int = 25) -> Any:
    return rows[:n] if isinstance(rows, list) else rows


def build_server() -> Any:
    mcp = _Server("aeronexus", instructions=INSTRUCTIONS)

    @mcp.tool()
    async def search_knowledge(query: str, k: int = 4) -> Any:
        """Hybrid (BM25 + dense) search over AeroNexus procedures, standards and policies. Returns cited passages."""
        return await _call("GET", "/api/integrations/search", params={"q": query[:300], "k": max(1, min(k, 8))})

    @mcp.tool()
    async def sap_list_materials(query: str = "") -> Any:
        """SAP material master (mock): material number, name, unit, price, fixed supplier."""
        return _trim(await _call("GET", "/api/sap/materials", params={"query": query}))

    @mcp.tool()
    async def sap_list_suppliers() -> Any:
        """SAP suppliers (mock) with lead time and on-time rate."""
        return await _call("GET", "/api/sap/suppliers")

    @mcp.tool()
    async def sap_list_purchase_orders(status: str = "") -> Any:
        """SAP purchase orders (mock). status: '' | open | released."""
        return _trim(await _call("GET", "/api/sap/purchase-orders", params={"status": status}))

    @mcp.tool()
    async def sap_list_purchase_requisitions(status: str = "") -> Any:
        """SAP purchase requisitions (mock). status: '' | created | converted."""
        return _trim(await _call("GET", "/api/sap/purchase-requisitions", params={"status": status}))

    @mcp.tool()
    async def sap_request_purchase_requisition(material: str, quantity: float, reason: str, delivery_days: int = 14) -> Any:
        """Ask for an SAP purchase requisition. Nothing is created yet: the request waits in the AeroNexus approval queue
        until a named human approves it. Returns the approval id to poll with approval_status."""
        return await _call("POST", "/api/sap/purchase-requisitions",
                           json={"material": material, "quantity": quantity, "reason": reason, "delivery_days": delivery_days})

    @mcp.tool()
    async def approval_status(approval_id: str) -> Any:
        """Status of an approval request: pending, executed (approved), rejected or failed, with the decision maker."""
        q = await _call("GET", "/api/integrations/approvals")
        if isinstance(q, dict) and "error" in q:
            return q
        for a in q.get("pending", []) + q.get("decided", []):
            if a["id"] == approval_id:
                return {k: a.get(k) for k in ("id", "tool", "status", "decided_by", "age_s", "args", "result")}
        return {"error": "not found", "approval_id": approval_id}

    @mcp.tool()
    async def engine_health(subset: str = "FD001", unit: int = 20) -> Any:
        """Digital-twin summary for a NASA C-MAPSS engine: remaining useful life with 90% range, health score and worn modules."""
        s = await _call("GET", "/api/twin/state", params={"subset": subset, "unit": unit})
        if not isinstance(s, dict) or "error" in s or not s.get("available"):
            return s
        return {"engine": f"{subset}-{unit}", "cycle": s["cycle"], "rul": s["rul"], "health": s["health"],
                "health_threshold": s["health_threshold"],
                "modules": [{"module": c["label"], "condition": c["score"], "status": c["status"]} for c in s["components"]],
                "note": "C-MAPSS engines are simulated; component condition is a sensor-drift index."}

    @mcp.tool()
    async def start_procurement_workflow(subset: str = "FD001", unit: int = 20) -> Any:
        """Start the 'Inspection → SAP requisition → Sheets' workflow for an engine. It pauses for human approval."""
        return await _call("POST", "/api/integrations/procurement/demo", json={"subset": subset, "unit": unit})

    @mcp.tool()
    async def automation_analytics() -> Any:
        """Productivity analytics from the tamper-evident audit log: approvals, turnaround, actions per connector."""
        a = await _call("GET", "/api/integrations/analytics")
        if isinstance(a, dict):
            a.pop("tracking_rows", None)
        return a

    @mcp.resource("aeronexus://connectors")
    async def connectors() -> str:
        """Which integrations are enabled (feature flags) and their cost model."""
        s = await _call("GET", "/api/integrations/status")
        return json.dumps(s.get("connectors", s) if isinstance(s, dict) else s, indent=1)

    return mcp


def main() -> None:
    from .flags import mcp_enabled

    if not mcp_enabled():
        raise SystemExit("MCP server disabled (set AERONEXUS_FF_MCP=1 and AERONEXUS_FF_INTEGRATIONS=1)")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    server = build_server()
    if not a.http:
        server.run("stdio")
        return
    try:  # mcp 2.x takes host/port on run()
        server.run("streamable-http", host=a.host, port=a.port)
    except TypeError:  # mcp 1.x reads them from settings
        server.settings.host, server.settings.port = a.host, a.port
        server.run("streamable-http")


if __name__ == "__main__":
    main()
