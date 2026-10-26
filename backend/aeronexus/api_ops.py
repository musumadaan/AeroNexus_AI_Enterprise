"""REST + WebSocket API for the digital twin, mission control, the workflow builder and observability.

REST calls carry the same demo identity headers as GraphQL (X-User / X-Role / X-Tenant → Principal; swap for
OIDC in production). WebSockets stream run timelines and simulator telemetry; they are read-only, and every
state change (start, approve, save) goes through an authenticated REST call.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .security import Forbidden, Principal, principal_from_headers

router = APIRouter()


def principal(request: Request) -> Principal:
    return principal_from_headers(request.headers)


def need(p: Principal, perm: str) -> Principal:
    try:
        p.require(perm)
    except Forbidden as exc:
        raise HTTPException(403, str(exc))
    return p


def _err(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, f"not found: {exc}")
    if isinstance(exc, (Forbidden, PermissionError)):
        return HTTPException(403, str(exc))
    return HTTPException(400, str(exc))


# ---------------------------------------------------------------------------------------------
# digital twin + simulator
# ---------------------------------------------------------------------------------------------
class Injection(BaseModel):
    type: str
    sensor: str
    start: int
    magnitude: float = 4.0


class TelemetryReq(BaseModel):
    subset: str = "FD001"
    unit: int = 20
    inject: list[Injection] = Field(default_factory=list)


@router.get("/api/twin/meta")
def twin_meta() -> dict[str, Any]:
    from .twin import components, engines

    meta = components()
    try:
        meta["engines"] = {s: engines(s) for s in ("FD001", "FD002", "FD003", "FD004")}
        meta["available"] = True
    except Exception as exc:  # data not fetched yet
        meta["engines"], meta["available"], meta["message"] = {}, False, str(exc)
    return meta


@router.get("/api/twin/state")
def twin_get(subset: str = "FD001", unit: int = 20, cycle: Optional[int] = None) -> dict[str, Any]:
    from .twin import twin_state

    try:
        return twin_state(subset, unit, cycle)
    except Exception as exc:
        raise _err(exc)


@router.post("/api/twin/telemetry")
def twin_telemetry(req: TelemetryReq) -> dict[str, Any]:
    from .twin import telemetry

    try:
        return telemetry(req.subset, req.unit, [i.model_dump() for i in req.inject])
    except Exception as exc:
        raise _err(exc)


class FireReq(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("/api/triggers/fire")
def fire(req: FireReq, p: Principal = Depends(principal)) -> dict[str, Any]:
    from .orchestration.workflows import fire_trigger

    need(p, "run")
    allowed = {"new_drawing", "sensor_anomaly", "maintenance_alert", "change_request", "safety_report", "manual"}
    if req.type not in allowed:
        raise HTTPException(400, f"trigger must be one of {sorted(allowed)}")
    try:
        return {"started": fire_trigger(req.type, req.payload, p)}
    except Exception as exc:
        raise _err(exc)


@router.websocket("/ws/telemetry")
async def ws_telemetry(ws: WebSocket) -> None:
    """Stream an engine's history cycle by cycle. Client messages: {cmd: play|pause|seek|speed, cycle?, speed?}."""
    from .twin import telemetry

    await ws.accept()
    q = ws.query_params
    try:
        inject = json.loads(q.get("inject") or "[]")
        t = await run_in_threadpool(telemetry, q.get("subset", "FD001"), int(q.get("unit", "20")), inject)
    except Exception as exc:
        await ws.send_json({"type": "error", "message": str(exc)})
        await ws.close()
        return
    if not t.get("available"):
        await ws.send_json({"type": "error", "message": t.get("message")})
        await ws.close()
        return
    frames = t["frames"]
    await ws.send_json({"type": "meta", **{k: v for k, v in t.items() if k != "frames"}, "n": len(frames)})
    i, playing, speed = 0, True, max(0.5, min(40.0, float(q.get("speed", "8"))))
    try:
        while True:
            if playing and i < len(frames):
                await ws.send_json({"type": "frame", "i": i, **frames[i]})
                i += 1
                if i == len(frames):
                    await ws.send_json({"type": "end"})
            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=1.0 / speed if playing and i < len(frames) else 0.5)
                cmd = msg.get("cmd")
                if cmd == "pause":
                    playing = False
                elif cmd == "play":
                    playing, i = True, (0 if i >= len(frames) else i)
                elif cmd == "seek":
                    i = max(0, min(len(frames) - 1, next((k for k, f in enumerate(frames) if f["cycle"] >= int(msg.get("cycle", 0))), 0)))
                    await ws.send_json({"type": "frame", "i": i, **frames[i]})
                elif cmd == "speed":
                    speed = max(0.5, min(40.0, float(msg.get("speed", speed))))
            except asyncio.TimeoutError:
                pass
    except (WebSocketDisconnect, RuntimeError):
        return


# ---------------------------------------------------------------------------------------------
# runs (missions + workflows)
# ---------------------------------------------------------------------------------------------
def _run_view(run: Any, p: Principal) -> dict[str, Any]:
    from .orchestration.engine import approval_token

    d = run.public()
    for node, a in d["approvals"].items():
        tok = approval_token(run, node, p)
        if tok:
            a["approval_token"] = tok
    return d


@router.get("/api/runs")
def runs(kind: Optional[str] = None, limit: int = 40, p: Principal = Depends(principal)) -> list[dict[str, Any]]:
    from .orchestration.runs import store

    need(p, "read")
    return [r.public(full=False) for r in store.list(kind, p.tenant, min(limit, 200))]


@router.get("/api/runs/compare")
def compare_runs(a: str, b: str, p: Principal = Depends(principal)) -> dict[str, Any]:
    from .orchestration.workflows import compare

    need(p, "read")
    try:
        return compare(a, b, p.tenant)
    except Exception as exc:
        raise _err(exc)


@router.get("/api/runs/{run_id}")
def run_detail(run_id: str, p: Principal = Depends(principal)) -> dict[str, Any]:
    from .orchestration.runs import store

    need(p, "read")
    try:
        return _run_view(store.get(run_id, p.tenant), p)
    except Exception as exc:
        raise _err(exc)


class Decision(BaseModel):
    node: str
    approve: bool
    note: str = ""
    token: str = ""


@router.post("/api/runs/{run_id}/approve")
def approve_run(run_id: str, d: Decision, p: Principal = Depends(principal)) -> dict[str, Any]:
    from .orchestration.engine import decide
    from .orchestration.runs import store

    try:
        run = store.get(run_id, p.tenant)
        decide(run, d.node, p, d.approve, d.note, d.token)
        return {"ok": True, "status": run.status}
    except Exception as exc:
        raise _err(exc)


@router.post("/api/runs/{run_id}/replay")
def replay_run(run_id: str, p: Principal = Depends(principal)) -> dict[str, Any]:
    from .orchestration.workflows import replay

    need(p, "run")
    try:
        r = replay(run_id, p)
        return {"run_id": r.id}
    except Exception as exc:
        raise _err(exc)


@router.websocket("/ws/runs/{run_id}")
async def ws_run(ws: WebSocket, run_id: str) -> None:
    """Live timeline: events as they happen plus node states; closes when the run ends."""
    from .orchestration.runs import store

    await ws.accept()
    tenant = ws.query_params.get("tenant", "demo")
    sent, last_nodes = 0, None
    try:
        while True:
            try:
                run = store.get(run_id, tenant)
            except KeyError:
                await ws.send_json({"type": "error", "message": "run not found"})
                break
            evs = run.events[sent:]
            sent += len(evs)
            nodes = json.dumps(run.nodes, sort_keys=True, default=str)
            if evs or nodes != last_nodes:
                last_nodes = nodes
                await ws.send_json({"type": "update", "events": evs, "nodes": run.nodes, "status": run.status,
                                    "approvals": {k: {kk: vv for kk, vv in v.items() if kk != "token"} for k, v in run.approvals.items()},
                                    "metrics": run.metrics})
            if run.status in ("completed", "failed", "rejected") and sent >= len(run.events):
                await ws.send_json({"type": "done", "status": run.status})
                break
            await asyncio.sleep(0.12)
    except (WebSocketDisconnect, RuntimeError):
        return
    try:
        await ws.close()
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------------------------
# mission control
# ---------------------------------------------------------------------------------------------
class MissionReq(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    faults: dict[str, Any] = Field(default_factory=dict)


@router.get("/api/mission/presets")
def mission_presets() -> list[dict[str, Any]]:
    from .orchestration.mission import AGENTS, PRESETS

    return [{**p, "agents": AGENTS} for p in PRESETS]


@router.get("/api/mission/graph")
def mission_graph() -> dict[str, Any]:
    from .orchestration.mission import shape

    return shape()


@router.post("/api/mission/start")
def mission_start(req: MissionReq, p: Principal = Depends(principal)) -> dict[str, Any]:
    from .orchestration.mission import start_mission

    need(p, "run")
    faults = {k: (int(v) if k != "_permanent" else [str(x) for x in v][:6]) for k, v in (req.faults or {}).items()}
    try:
        r = start_mission(req.inputs, p, faults=faults)
        return {"run_id": r.id}
    except Exception as exc:
        raise _err(exc)


# ---------------------------------------------------------------------------------------------
# workflow builder
# ---------------------------------------------------------------------------------------------
class WorkflowDef(BaseModel):
    id: Optional[str] = None
    name: str = "Untitled workflow"
    description: str = ""
    enabled: bool = True
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]] = Field(default_factory=list)


class RunReq(BaseModel):
    workflow_id: Optional[str] = None
    definition: Optional[WorkflowDef] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    mode: str = "live"
    faults: dict[str, Any] = Field(default_factory=dict)
    n: int = 12


def _defn(req: RunReq) -> dict[str, Any]:
    from .orchestration.workflows import workflows

    if req.definition is not None:
        return req.definition.model_dump()
    if req.workflow_id:
        return workflows.get(req.workflow_id)
    raise HTTPException(400, "send a workflow_id or a definition")


@router.get("/api/workflows/catalog")
def wf_catalog() -> dict[str, Any]:
    from .orchestration.library import catalog_public

    return {"nodes": catalog_public(), "categories": ["trigger", "agent", "model", "document", "knowledge", "calc",
                                                       "integration", "data", "logic", "human"]}


@router.get("/api/workflows/templates")
def wf_templates() -> list[dict[str, Any]]:
    from .orchestration.workflows import TEMPLATES

    return TEMPLATES


@router.get("/api/workflows")
def wf_list(p: Principal = Depends(principal)) -> list[dict[str, Any]]:
    from .orchestration.workflows import workflows

    need(p, "read")
    return [{k: v for k, v in w.items() if k != "versions"} | {"n_versions": len(w.get("versions", [])) + 1} for w in workflows.list()]


@router.get("/api/workflows/{wid}")
def wf_get(wid: str, p: Principal = Depends(principal)) -> dict[str, Any]:
    from .orchestration.workflows import workflows

    need(p, "read")
    try:
        return workflows.get(wid)
    except Exception as exc:
        raise _err(exc)


@router.put("/api/workflows")
def wf_save(d: WorkflowDef, p: Principal = Depends(principal)) -> dict[str, Any]:
    from .orchestration.workflows import workflows

    need(p, "run")
    try:
        return workflows.save(d.model_dump(), p.user or "anonymous")
    except Exception as exc:
        raise _err(exc)


@router.delete("/api/workflows/{wid}")
def wf_delete(wid: str, p: Principal = Depends(principal)) -> dict[str, Any]:
    from .orchestration.workflows import workflows

    need(p, "approve")
    workflows.delete(wid)
    return {"ok": True}


@router.post("/api/workflows/validate")
def wf_validate(d: WorkflowDef) -> dict[str, Any]:
    from .orchestration.engine import validate

    return validate(d.model_dump())


@router.post("/api/workflows/run")
def wf_run(req: RunReq, p: Principal = Depends(principal)) -> dict[str, Any]:
    from .orchestration.workflows import run_workflow

    need(p, "run")
    try:
        r = run_workflow(_defn(req), req.payload, p, req.mode if req.mode in ("live", "test") else "test", req.faults)
        return {"run_id": r.id}
    except HTTPException:
        raise
    except Exception as exc:
        raise _err(exc)


@router.post("/api/workflows/test")
def wf_test(req: RunReq, p: Principal = Depends(principal)) -> dict[str, Any]:
    from .orchestration.workflows import run_test_batch

    need(p, "run")
    try:
        b = run_test_batch(_defn(req), p, req.n, req.faults)
        return {"run_id": b.id}
    except HTTPException:
        raise
    except Exception as exc:
        raise _err(exc)


# ---------------------------------------------------------------------------------------------
# observability
# ---------------------------------------------------------------------------------------------
def _latest() -> dict[str, Any]:
    from .api import _report

    return _report()


@router.get("/api/obs/summary")
def obs_summary(p: Principal = Depends(principal)) -> dict[str, Any]:
    from .mlops import agent_reliability, history, mlflow_runs, tracking_uri
    from .observability import tracer
    from .orchestration.tracing import status
    from .usage import meter

    need(p, "read")
    rep = _latest()
    ex, rt, gr = rep.get("extraction", {}), rep.get("retrieval", {}), rep.get("grounding", {})
    hist = history(30)
    return {
        "generated_at": rep.get("generated_at"),
        "gate": {"passed": rep.get("gate", {}).get("passed"), "checks": rep.get("gate", {}).get("checks", [])},
        "quality": {
            "extraction": {"precision": ex.get("micro", {}).get("precision"), "recall": ex.get("micro", {}).get("recall"),
                           "f1": ex.get("micro", {}).get("f1"), "auto_accept_precision": ex.get("auto_accept_precision"),
                           "automation_rate": ex.get("automation_rate"), "ece": ex.get("calibration", {}).get("ece"),
                           "per_field": ex.get("per_field", {}), "slices": ex.get("slices", {})},
            "retrieval": {"recall_at_3": rt.get("recall_at_3"), "mrr": rt.get("mrr"), "queries": rt.get("queries", [])},
            "citations": {"support_rate": gr.get("mean_groundedness_answered"), "abstention_accuracy": gr.get("abstention_accuracy"),
                          "decisions": gr.get("decisions"), "rows": gr.get("rows", [])},
            "judge": {k: rep.get("judge", {}).get(k) for k in ("judge_human_kappa", "human_human_kappa", "approved_as_gate", "verdict")},
            "pdm": rep.get("pdm"), "safety": {k: v for k, v in (rep.get("safety") or {}).items() if not isinstance(v, (list, dict))},
            "drift": rep.get("drift"),
        },
        "latency": {"eval": rep.get("latency", {}), "live": tracer.latency_report()},
        "usage": meter.summary(),
        "reliability": agent_reliability(),
        "tracing": status(),
        "mlflow": {"tracking_uri": tracking_uri(), "runs": mlflow_runs(15),
                   "ui": "uvx mlflow ui --backend-store-uri sqlite:///backend/data/mlflow.db"},
        "history": [{k: h.get(k) for k in ("run_id", "generated_at", "by", "gate_passed", "metrics", "mlflow_run_id")}
                    | {"regressed": (h.get("regression") or {}).get("regressed", []),
                       "improved": (h.get("regression") or {}).get("improved", [])} for h in hist],
    }


@router.post("/api/obs/regression")
async def obs_regression(p: Principal = Depends(principal)) -> dict[str, Any]:
    """Run the full evaluation suite, gate it, compare with the previous run and log it to MLflow."""
    from .chat import get_chat
    from .evals.runner import run_all
    from .mlops import record_eval

    need(p, "evaluate")

    def go() -> dict[str, Any]:
        rep = run_all()
        get_chat().reload()
        return record_eval(rep, p.user or "admin")

    entry = await run_in_threadpool(go)
    return entry


@router.get("/api/obs/report/{run_id}", response_class=PlainTextResponse)
def obs_report(run_id: str) -> str:
    from .mlops import HIST

    if not run_id.replace("-", "").isalnum():
        raise HTTPException(400, "bad id")
    path = HIST / f"{run_id}.md"
    if not path.exists():
        raise HTTPException(404, "no such report")
    return path.read_text()
