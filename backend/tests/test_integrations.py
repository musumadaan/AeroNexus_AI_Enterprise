"""Enterprise-integration layer: mock SAP + human approval, audit chain, Sheets tracking, MCP, Salesforce, Gemini provider,
secrets, and backward compatibility with the layer switched off."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from aeronexus.config import MODELS

ENG = {"x-user": "qa.engineer", "x-role": "engineer", "x-tenant": "demo"}
APP = {"x-user": "qa.approver", "x-role": "approver", "x-tenant": "demo"}
BACKEND = Path(__file__).resolve().parent.parent


@pytest.fixture()
def client():
    from aeronexus.api import app

    return TestClient(app)


def test_sap_requisition_waits_for_a_named_approver(client):
    r = client.post("/api/sap/purchase-requisitions", json={"material": "HPC-BLD-SET", "quantity": 1, "reason": "HPC wear on FD001-20"},
                    headers=ENG).json()
    assert r["status"] == "pending_approval"
    assert client.get("/api/sap/purchase-requisitions").json() == []  # nothing reaches SAP before approval
    assert client.post(f"/api/integrations/approvals/{r['approval_id']}", json={"approve": True}, headers=ENG).status_code == 403
    d = client.post(f"/api/integrations/approvals/{r['approval_id']}", json={"approve": True, "note": "budget ok"}, headers=APP).json()
    assert d["status"] == "executed" and d["decided_by"] == "qa.approver"
    pr = d["result"]["PurchaseRequisition"]
    assert client.get("/api/sap/purchase-requisitions").json()[0]["PurchaseRequisition"] == pr
    assert client.post(f"/api/integrations/approvals/{r['approval_id']}", json={"approve": True}, headers=APP).status_code == 409
    conv = client.post(f"/api/sap/purchase-requisitions/{pr}/convert", headers=ENG).json()
    po = client.post(f"/api/integrations/approvals/{conv['approval_id']}", json={"approve": True}, headers=APP).json()
    assert po["result"]["Status"] == "released" and po["result"]["FromRequisition"] == pr


def test_rejection_and_input_validation(client):
    r = client.post("/api/sap/purchase-requisitions", json={"material": "FAN-BLD-01", "quantity": 2, "reason": "fan erosion"}, headers=ENG).json()
    d = client.post(f"/api/integrations/approvals/{r['approval_id']}", json={"approve": False, "note": "use stock"}, headers=APP).json()
    assert d["status"] == "rejected"
    assert client.post("/api/sap/purchase-requisitions", json={"material": "X", "quantity": -1, "reason": "x"}, headers=ENG).status_code == 422
    assert client.post("/api/sap/purchase-requisitions", json={"material": "FAN-BLD-01", "quantity": 1, "reason": "viewer try"},
                       headers={"x-role": "viewer"}).status_code == 403


def test_audit_chain_detects_tampering_and_redacts_secrets(client):
    from aeronexus.integrations.audit import audit_log

    log = audit_log()
    log.append("test", "qa", api_key="gsk_" + "a" * 30, note="safe")
    r = client.post("/api/sap/purchase-requisitions", json={"material": "T50-PROBE", "quantity": 1, "reason": "probe"}, headers=ENG).json()
    client.post(f"/api/integrations/approvals/{r['approval_id']}", json={"approve": True}, headers=APP)
    v = client.get("/api/integrations/audit", headers=ENG).json()
    assert v["verify"]["valid"] and v["verify"]["records"] >= 3
    assert all("gsk_" not in json.dumps(rec) for rec in log.records())
    lines = log.path.read_text().splitlines()
    rec = json.loads(lines[-1])
    rec["actor"] = "someone-else"
    lines[-1] = json.dumps(rec)
    log.path.write_text("\n".join(lines) + "\n")
    assert log.verify()["valid"] is False


def test_sheets_local_tracking_and_analytics(client):
    from aeronexus.integrations.sheets import tracker

    r = client.post("/api/sap/purchase-requisitions", json={"material": "SEAL-KIT-HPT", "quantity": 1, "reason": "seal wear"}, headers=ENG).json()
    client.post(f"/api/integrations/approvals/{r['approval_id']}", json={"approve": True}, headers=APP)
    tracker.flush(5)
    rows = tracker.recent_local(10)
    assert tracker.mode == "local" and any(x["tool"] == "sap.create_purchase_requisition" and x["event"] == "approved_and_executed" for x in rows)
    a = client.get("/api/integrations/analytics", headers=ENG).json()
    assert a["counts"]["approved"] >= 1 and a["minutes_saved_est"] > 0


def test_status_never_returns_secret_values(client, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-very-secret-value-123")
    s = client.get("/api/integrations/status").text
    assert "very-secret" not in s and '"GEMINI_API_KEY"' in s


def test_secret_files(tmp_path, monkeypatch):
    from aeronexus.integrations.secrets import get_secret, mask

    f = tmp_path / "tok"
    f.write_text("from-file\n")
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    monkeypatch.setenv("DATABRICKS_TOKEN_FILE", str(f))
    assert get_secret("DATABRICKS_TOKEN") == "from-file"
    assert "from-file" not in mask("from-file")


@pytest.mark.skipif(not (MODELS / "pdm_FD001.joblib").exists(), reason="C-MAPSS models not trained")
def test_procurement_workflow_test_and_live():
    from aeronexus.orchestration import engine
    from aeronexus.orchestration.workflows import TEMPLATES, run_workflow
    from aeronexus.security import Principal
    from aeronexus.services import services

    tpl = next(t for t in TEMPLATES if t["id"] == "tpl-procure")
    eng, appr = Principal("qa.engineer", "engineer", "demo"), Principal("qa.approver", "approver", "demo")
    t = run_workflow(tpl, {"subset": "FD001", "unit": 20}, eng, "test", background=False)
    assert t.status == "completed" and t.outputs["pr"]["sandbox"] and t.outputs["log"]["simulated"]
    assert services().runtime.sap.requisitions == {}  # test mode never touches live SAP
    r = run_workflow(tpl, {"subset": "FD001", "unit": 20}, eng, "live", background=False)
    assert r.status == "waiting_approval"
    engine.decide(r, "approve", appr, True, "ok", engine.approval_token(r, "approve", appr), background=False)
    assert r.status == "completed"
    pr = r.outputs["pr"]["purchase_requisition"]
    assert pr in services().runtime.sap.requisitions and r.outputs["log"]["written"]


def test_mcp_server_tools_and_least_privilege(monkeypatch):
    from mcp.client.client import Client

    from aeronexus.api import app
    from aeronexus.integrations import mcp_server as M

    monkeypatch.setenv("AERONEXUS_MCP_ROLE", "approver")  # must be downgraded: MCP clients never approve
    assert M._headers()["x-role"] == "engineer"
    M.use_transport(httpx.ASGITransport(app=app))

    async def go():
        async with Client(M.build_server()) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert {"sap_request_purchase_requisition", "approval_status", "search_knowledge", "engine_health"} <= names
            res = await c.call_tool("sap_request_purchase_requisition", {"material": "BSK-ENG-KIT", "quantity": 1, "reason": "mcp test"})
            body = json.loads(res.content[0].text)
            assert body["status"] == "pending_approval"
            st = json.loads((await c.call_tool("approval_status", {"approval_id": body["approval_id"]})).content[0].text)
            assert st["status"] == "pending"
            hits = (await c.call_tool("search_knowledge", {"query": "inspection work order remaining useful life"})).content
            assert hits and "predictive_maintenance_policy" in hits[0].text

    try:
        asyncio.run(go())
    finally:
        M.use_transport(None)


def test_salesforce_mock_case_needs_approval(monkeypatch):
    monkeypatch.setenv("AERONEXUS_FF_SALESFORCE", "mock")
    from aeronexus.services import reset_services, services

    reset_services()
    rt = services().runtime
    acc = rt.call("sf.find_accounts", {"part_number": "AX1000-B2"})["result"][0]
    r = rt.call("sf.create_case", {"account_id": acc["Id"], "subject": "ECO notice", "description": "thickness change"})
    assert r["status"] == "pending_approval" and rt.salesforce.cases == []
    item = rt.decide(r["approval_id"], "qa.approver", True)
    assert item.status == "executed" and rt.salesforce.cases[0]["Subject"] == "ECO notice"


def test_gemini_provider_parses_usage_json_and_rate_limits(monkeypatch):
    from aeronexus.integrations.providers import GeminiProvider
    from aeronexus.llm import LLMRequest, RateLimited
    from aeronexus.usage import meter

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    calls = []

    def fake_post(url, json=None, headers=None, params=None, timeout=None):
        calls.append((url, json, headers))
        if len(calls) == 1:
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"answer": "ok", "confidence": 0.9}'}]}}],
                                             "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 7}})
        return httpx.Response(429, text='{"error": {"details": [{"retryDelay": "3s"}]}}')

    monkeypatch.setattr(httpx, "post", fake_post)
    p = GeminiProvider()
    out = p.complete(LLMRequest(task="t", prompt="Return JSON", json_schema={"answer": "str"}), "gemini-3.5-flash-lite")
    assert json.loads(out)["answer"] == "ok"
    assert calls[0][1]["generationConfig"]["responseMimeType"] == "application/json" and calls[0][2]["x-goog-api-key"] == "test-key"
    assert meter.take() == (11, 7)
    with pytest.raises(RateLimited) as e:
        p.complete(LLMRequest(task="t", prompt="x"), "gemini-3.5-flash-lite")
    assert e.value.retry_after == 3.0


def test_layer_off_is_backward_compatible():
    """With AERONEXUS_FF_INTEGRATIONS=0 the app exposes exactly the old routes and tools."""
    code = ("import json; from aeronexus.api import app; from aeronexus.services import services;"
            "rt=services().runtime; paths=list(app.openapi()['paths']);"
            "print(json.dumps({'int': any(p.startswith('/api/integrations') or p.startswith('/api/sap') for p in paths),"
            "'sap': any(t.startswith('sap.') for t in rt.tools), 'listeners': len(rt.listeners),"
            "'workflows': any(p.startswith('/api/workflows') for p in paths)}))")
    env = {**os.environ, "AERONEXUS_FF_INTEGRATIONS": "0", "AERONEXUS_STATE_DB": ":memory:"}
    out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    d = json.loads(out.stdout.strip().splitlines()[-1])
    assert d == {"int": False, "sap": False, "listeners": 0, "workflows": True}
