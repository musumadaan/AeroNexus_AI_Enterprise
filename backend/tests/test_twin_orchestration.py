"""Digital twin, LangGraph mission control, workflow engine, usage metering and evaluation tracking."""
import pytest
from fastapi.testclient import TestClient

from aeronexus.config import MODELS
from aeronexus.security import Forbidden, Principal

need_cmapss = pytest.mark.skipif(not (MODELS / "pdm_FD001.joblib").exists(), reason="run fetch_public_data + pdm training")
need_all = pytest.mark.skipif(not ((MODELS / "pdm_FD001.joblib").exists() and (MODELS / "safety_ntsb.joblib").exists()),
                              reason="public models not trained")

APPROVER = Principal("qa.approver", "approver", "demo")
ENGINEER = Principal("qa.engineer", "engineer", "demo")
VIEWER = Principal("qa.viewer", "viewer", "demo")


# ---------------------------------------------------------------------------------------------- twin
@need_cmapss
def test_twin_frames_are_causal_and_intervals_contain_prediction():
    from aeronexus.twin import telemetry

    t = telemetry("FD001", 20)
    fr = t["frames"]
    assert t["available"] and len(fr) > 100
    assert all(f["low"] <= f["rul"] <= f["high"] for f in fr)
    assert [f["cycle"] for f in fr] == sorted(f["cycle"] for f in fr)
    assert fr[-1]["true_rul"] >= 0 and not t["simulated"]


@need_cmapss
def test_component_drift_grows_with_wear():
    from aeronexus.twin import telemetry

    fr = telemetry("FD001", 20)["frames"]  # this engine ends close to failure
    mean = lambda f: sum(v for v in f["components"].values() if v is not None) / 7
    assert mean(fr[0]) - mean(fr[-1]) > 20


@need_cmapss
def test_injected_faults_are_detected_and_marked_simulated():
    from aeronexus.twin import telemetry

    t = telemetry("FD001", 20, [{"type": "stuck", "sensor": "s4", "start": 150}, {"type": "spike", "sensor": "s11", "start": 100,
                                                                                    "magnitude": 8}])
    kinds = {(e["kind"], e["sensor"]) for e in t["events"]}
    assert ("stuck", "s4") in kinds and ("spike", "s11") in kinds and t["simulated"]
    with pytest.raises(ValueError):
        telemetry("FD001", 20, [{"type": "melt", "sensor": "s4", "start": 3}])


@need_cmapss
def test_healthy_fleet_has_few_false_spike_alarms():
    from aeronexus.twin import telemetry

    spikes = sum(e["kind"] == "spike" for u in range(1, 31) for e in telemetry("FD001", u)["events"])
    assert spikes <= 3


# ---------------------------------------------------------------------------------------------- engine
def _wf(nodes, edges):
    return {"name": "t", "nodes": [{"id": i, "type": t, "config": c} for i, t, c in nodes],
            "edges": [{"source": s, "target": d, **({"branch": b} if b else {})} for s, d, b in edges]}


def test_validation_blocks_unapproved_writes_cycles_and_extra_triggers():
    from aeronexus.orchestration.engine import validate

    no_gate = _wf([("t", "trigger.manual", {}), ("m", "notify.email", {"to": "a@b.c"})], [("t", "m", None)])
    assert not validate(no_gate)["valid"] and "approval" in validate(no_gate)["errors"][0]
    gated = _wf([("t", "trigger.manual", {}), ("a", "human.approval", {}), ("m", "notify.email", {"to": "a@b.c"})],
                [("t", "a", None), ("a", "m", None)])
    assert validate(gated)["valid"]
    loop = _wf([("t", "trigger.manual", {}), ("x", "transform.map", {}), ("y", "transform.map", {})],
               [("t", "x", None), ("x", "y", None), ("y", "x", None)])
    assert any("Loop" in e for e in validate(loop)["errors"])
    two = _wf([("t", "trigger.manual", {}), ("u", "trigger.new_drawing", {})], [("t", "u", None)])
    assert not validate(two)["valid"]
    unlabelled = _wf([("t", "trigger.manual", {}), ("c", "logic.condition", {"expression": "1 < 2"}), ("x", "transform.map", {})],
                     [("t", "c", None), ("c", "x", None)])
    assert any("true or false" in e for e in validate(unlabelled)["errors"])


def test_condition_language_has_no_calls_or_attribute_escape():
    from aeronexus.orchestration.library import safe_eval

    assert safe_eval("rul.low < 30 and drawing.severity in ['critical']", {"rul": {"low": 12}, "drawing": {"severity": "critical"}})
    assert safe_eval("missing.value > 3", {}) is False
    for bad in ("__import__('os')", "len(x)", "(lambda: 1)()"):
        with pytest.raises((ValueError, SyntaxError)):
            safe_eval(bad, {})


def test_templates_render_types_and_strings():
    from aeronexus.orchestration.library import render

    scope = {"cost": {"total": 703.42}, "trigger": {"unit": 20, "subset": "FD001"}}
    assert render("{{cost.total}}", scope) == 703.42
    assert render("{{trigger.subset}}-ENG-{{trigger.unit}}", scope) == "FD001-ENG-20"


def test_retries_then_fallback_keep_the_workflow_alive():
    from aeronexus.orchestration.workflows import TEMPLATES, run_workflow

    tpl = next(t for t in TEMPLATES if t["id"] == "tpl-resilience")
    r = run_workflow(tpl, {}, ENGINEER, "test", background=False)
    assert r.status == "completed"
    assert r.nodes["flaky"]["status"] == "failed" and r.nodes["flaky"]["attempts"] == 3
    assert sum(e["type"] == "retry" for e in r.events) == 2 and any(e["type"] == "fallback" for e in r.events)
    assert r.outputs["summary"]["supplier_api"] == "failed"


def test_injected_transient_fault_recovers_within_retry_budget():
    from aeronexus.orchestration.workflows import TEMPLATES, run_workflow

    tpl = next(t for t in TEMPLATES if t["id"] == "tpl-quote")
    r = run_workflow(tpl, {"sheet_id": "G0001"}, ENGINEER, "test", faults={"ocr": 2}, background=False)
    assert r.status == "completed" and r.nodes["ocr"]["attempts"] == 3


def test_live_run_pauses_and_only_a_named_approver_with_the_token_resumes():
    from aeronexus.orchestration import engine
    from aeronexus.orchestration.workflows import TEMPLATES, run_workflow
    from aeronexus.services import services

    tpl = next(t for t in TEMPLATES if t["id"] == "tpl-eco")
    r = run_workflow(tpl, {"pair_id": "CH000"}, ENGINEER, "live", background=False)
    assert r.status == "waiting_approval" and r.nodes["eco"]["status"] == "pending"
    assert engine.approval_token(r, "approve", ENGINEER) is None  # engineers never see the token
    tok = engine.approval_token(r, "approve", APPROVER)
    with pytest.raises(Forbidden):
        engine.decide(r, "approve", VIEWER, True, "", tok, background=False)
    with pytest.raises(Forbidden):
        engine.decide(r, "approve", APPROVER, True, "", "forged", background=False)
    engine.decide(r, "approve", APPROVER, True, "checked cost trace", tok, background=False)
    assert r.status == "completed" and r.nodes["eco"]["status"] == "completed"
    assert r.outputs["eco"]["approved_by"] == "qa.approver"
    assert any(o["channel"] == "email" for o in services().runtime.outbox)  # customer notice went out after approval
    with pytest.raises(ValueError):  # tokens are one-time
        engine.decide(r, "approve", APPROVER, True, "", tok, background=False)


def test_separation_of_duties_and_rejection():
    from aeronexus.orchestration import engine
    from aeronexus.orchestration.workflows import run_workflow

    d = _wf([("t", "trigger.manual", {}), ("a", "human.approval", {"require_distinct_approver": True}),
             ("m", "notify.email", {"to": "x@y.z", "subject": "s", "body": "b"})], [("t", "a", None), ("a", "m", None)])
    r = run_workflow(d, {}, APPROVER, "live", background=False)
    tok = engine.approval_token(r, "a", APPROVER)
    with pytest.raises(Forbidden):
        engine.decide(r, "a", APPROVER, True, "", tok, background=False)
    other = Principal("second.approver", "approver", "demo")
    engine.decide(r, "a", other, False, "not now", tok, background=False)
    assert r.status == "rejected" and r.nodes["m"]["status"] == "skipped"


def test_test_mode_never_touches_live_connectors():
    from aeronexus.orchestration.workflows import TEMPLATES, run_workflow
    from aeronexus.services import services

    before = len(services().runtime.outbox)
    tpl = next(t for t in TEMPLATES if t["id"] == "tpl-eco")
    r = run_workflow(tpl, {"pair_id": "CH000"}, ENGINEER, "test", background=False)
    assert r.status == "completed" and r.outputs["eco"]["sandbox"] is True
    assert len(services().runtime.outbox) == before


def test_replay_reproduces_every_step():
    from aeronexus.orchestration.workflows import TEMPLATES, compare, replay, run_workflow

    tpl = next(t for t in TEMPLATES if t["id"] == "tpl-eco")
    r = run_workflow(tpl, {"pair_id": "CH001"}, ENGINEER, "test", background=False)
    r2 = replay(r.id, ENGINEER, background=False)
    c = compare(r.id, r2.id, "demo")
    assert c["identical"], c["rows"]


def test_batch_metrics_score_escalation_against_truth():
    from aeronexus.orchestration.workflows import TEMPLATES, run_test_batch

    tpl = next(t for t in TEMPLATES if t["id"] == "tpl-eco")
    b = run_test_batch(tpl, ENGINEER, n=8, background=False)
    m = b.metrics
    assert b.status == "completed" and m["runs"] == 8 and m["failure_rate"] == 0
    assert m["escalation"]["accuracy"] == 1.0 and m["time"]["saved_minutes"] > 0


@need_all
def test_mission_runs_agents_in_parallel_and_executes_only_after_approval():
    from aeronexus.orchestration import engine
    from aeronexus.orchestration.mission import PRESETS, start_mission

    r = start_mission(PRESETS[0]["inputs"], APPROVER, background=False)
    assert r.status == "waiting_approval"
    assert r.outputs["planner"]["agents"] == ["drawing_agent", "pdm_agent", "safety_agent", "evidence_agent"]
    assert r.nodes["executor"]["status"] == "pending"
    props = r.outputs["synthesizer"]["proposals"]
    assert {p["tool"] for p in props} >= {"erp.create_eco", "cmms.create_maintenance_order"}
    engine.decide(r, "approval_gate", APPROVER, True, "ok", engine.approval_token(r, "approval_gate", APPROVER), background=False)
    assert r.status == "completed" and r.outputs["executor"]["count"] == len(props)


@need_all
def test_mission_fault_drill_retries_and_degrades_gracefully():
    from aeronexus.orchestration.mission import PRESETS, start_mission

    p = next(x for x in PRESETS if x["id"] == "faults")
    r = start_mission(p["inputs"], APPROVER, faults=p["faults"], background=False)
    assert r.nodes["safety_agent"]["attempts"] == 3 and r.nodes["safety_agent"]["status"] == "completed"
    assert r.nodes["evidence_agent"]["status"] == "failed"
    syn = r.outputs["synthesizer"]
    assert "Evidence retrieval agent" in syn["missing"] and syn["confidence"] < 0.95


# ---------------------------------------------------------------------------------------------- usage + mlops
def test_usage_meter_prices_tokens():
    from aeronexus.usage import UsageMeter

    m = UsageMeter()
    m.stash(1_000_000, 1_000_000)
    c = m.record("llama-3.3-70b-versatile", "t", "p", "c", 10.0)
    assert c.cost_usd == pytest.approx(0.59 + 0.79) and not c.estimated
    c2 = m.record("llama-3.1-8b-instant", "t", "x" * 400, "y" * 40, 5.0)
    assert c2.estimated and c2.prompt_tokens == 100
    assert m.summary()["total"]["calls"] == 2


def test_eval_tracking_flags_regressions_between_runs():
    from aeronexus import mlops

    rep = {"generated_at": "now", "gate": {"passed": True, "checks": [
        {"metric": "retrieval.recall_at_3", "value": 0.94, "op": "ge", "target": 0.9, "passed": True},
        {"metric": "extraction.calibration.ece", "value": 0.07, "op": "le", "target": 0.12, "passed": True}]}}
    first = mlops.record_eval(rep, "qa", log_mlflow=False)
    rep["gate"]["checks"][0]["value"] = 0.91  # still passes the gate, but worse
    rep["gate"]["checks"][1]["value"] = 0.05  # lower is better
    second = mlops.record_eval(rep, "qa", log_mlflow=False)
    assert second["regression"]["previous"] == first["run_id"]
    assert second["regression"]["regressed"] == ["retrieval.recall_at_3"]
    assert second["regression"]["improved"] == ["extraction.calibration.ece"]
    md = (mlops.HIST / f"{second['run_id']}.md").read_text()
    assert "Reproduce" in md and second["fingerprint"]["code_sha"] in md


# ---------------------------------------------------------------------------------------------- HTTP + WebSocket
def test_rest_and_websocket_run_stream():
    from aeronexus.api import app

    c = TestClient(app)
    H = {"x-user": "qa.engineer", "x-role": "engineer", "x-tenant": "demo"}
    assert any(n["type"] == "human.approval" for n in c.get("/api/workflows/catalog").json()["nodes"])
    tpl = next(t for t in c.get("/api/workflows/templates").json() if t["id"] == "tpl-resilience")
    assert c.post("/api/workflows/run", json={"definition": tpl, "mode": "test"}, headers={"x-role": "viewer"}).status_code == 403
    rid = c.post("/api/workflows/run", json={"definition": tpl, "mode": "test"}, headers=H).json()["run_id"]
    seen = []
    with c.websocket_connect(f"/ws/runs/{rid}") as ws:
        while True:
            m = ws.receive_json()
            seen.append(m["type"])
            if m["type"] in ("done", "error"):
                break
    assert seen[-1] == "done"
    d = c.get(f"/api/runs/{rid}", headers=H).json()
    assert d["status"] == "completed" and d["nodes"]["flaky"]["status"] == "failed"
    bad = {"nodes": [{"id": "t", "type": "trigger.manual"}, {"id": "m", "type": "notify.email"}], "edges": [{"source": "t", "target": "m"}]}
    assert c.put("/api/workflows", json=bad, headers=H).status_code == 400
