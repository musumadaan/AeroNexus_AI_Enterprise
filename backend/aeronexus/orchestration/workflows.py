"""Workflow builder back end: templates, saved workflows, live runs, test batches, replay and comparison.

Modes
  live    real connectors (in-memory ERP/CRM/CMMS of this deployment), real approval checkpoints.
  test    sandbox connectors and an in-memory case store; approvals are taken by a simulated reviewer so a
          whole batch can run unattended. Nothing a test run does reaches the live systems.
  replay  re-executes a recorded run's exact definition, trigger payload and fault settings in the sandbox
          and compares every step's output with the original.

Test batches draw trigger payloads from real or synthetic history (C-MAPSS engines, NTSB narratives from the
held-out years, revision pairs, golden drawings) and score the workflow against ground truth: escalation
accuracy, extraction accuracy, failure rate, retries, latency and an estimate of hands-on time saved.
"""
from __future__ import annotations

import os
from pathlib import Path

import copy
import json
import statistics
import threading
import time
import uuid
from typing import Any

from ..agents import AgentRuntime
from ..cases import CaseStore
from ..config import DATA
from ..security import Principal
from ..services import services
from .engine import compile_workflow, start, validate
from .library import CATALOG, Ctx
from .runs import RunRecord, jsonable, store

WF_DIR = Path(os.environ["AERONEXUS_WORKFLOWS_DIR"]) if os.environ.get("AERONEXUS_WORKFLOWS_DIR") else DATA / "workflows"


def _n(id_: str, type_: str, x: int, y: int, label: str | None = None, **config: Any) -> dict[str, Any]:
    return {"id": id_, "type": type_, "label": label or CATALOG[type_]["label"], "position": {"x": x, "y": y}, "config": config}


def _e(s: str, t: str, branch: str | None = None) -> dict[str, Any]:
    d = {"id": f"{s}-{t}", "source": s, "target": t}
    if branch:
        d["branch"] = branch
    return d


TEMPLATES: list[dict[str, Any]] = [
    {"id": "tpl-eco", "name": "Engineering change → ECO release", "trigger": "change_request",
     "description": "Diff the revisions, find affected records, verify the cost, and release the ECO after approval.",
     "nodes": [_n("trigger", "trigger.change_request", 0, 160),
               _n("drawing", "agent.drawing", 228, 160),
               _n("cost", "calc.change_cost", 457, 160, source="drawing"),
               _n("material", "logic.condition", 686, 160, "Needs change board?",
                  expression="drawing.severity in ['critical', 'high', 'medium']"),
               _n("approve", "human.approval", 915, 60, "Change board approval",
                  title="Release ECO for {{drawing.part_number}} ({{drawing.severity}}, {{cost.total}} CAD)",
                  require_distinct_approver=False),
               _n("eco", "api.erp_write", 1144, 0, "Release ECO in ERP", tool="erp.create_eco",
                  args={"case_id": "{{trigger.pair_id}}", "part_number": "{{drawing.part_number}}",
                        "summary": "Approved via workflow", "cost_impact": "{{cost.total}}", "effective": "today"}),
               _n("notify", "notify.email", 1144, 130, "Notify customer", to="{{drawing.impact.customer_emails}}",
                  subject="Engineering change notice – {{drawing.part_number}}",
                  body="Part {{drawing.part_number}} has an approved engineering change. Lead time {{cost.lead_time_days}} days."),
               _n("log", "db.save_case", 915, 280, "Record minor change", kind="change_order",
                  title="Minor change {{drawing.part_number}}", status="verified")],
     "edges": [_e("trigger", "drawing"), _e("drawing", "cost"), _e("cost", "material"), _e("material", "approve", "true"),
               _e("material", "log", "false"), _e("approve", "eco"), _e("approve", "notify")]},
    {"id": "tpl-pdm", "name": "Sensor anomaly → inspection work order", "trigger": "sensor_anomaly",
     "description": "Remaining life and anomaly detection in parallel, an LLM agent recommends, a technician approves.",
     "nodes": [_n("trigger", "trigger.sensor_anomaly", 0, 160),
               _n("rul", "model.rul", 228, 60), _n("anomaly", "model.anomaly", 228, 260),
               _n("decide", "agent.llm", 457, 160, "Maintenance agent",
                  prompt="Given the remaining-life estimate and the anomaly events, should engineering open an inspection?",
                  choices="escalate,monitor"),
               _n("gate", "logic.condition", 686, 160, "Inspection needed?",
                  expression="decide.choice == 'escalate'"),
               _n("approve", "human.approval", 915, 60, "Technician approval",
                  title="Open inspection for {{trigger.subset}}-ENG-{{trigger.unit}} (RUL {{rul.low}}–{{rul.high}} cycles)"),
               _n("wo", "api.cmms_work_order", 1144, 60), _n("monitor", "db.save_case", 915, 280, "Keep monitoring",
                                                             kind="maintenance", status="verified",
                                                             title="Monitor {{trigger.subset}}-ENG-{{trigger.unit}}")],
     "edges": [_e("trigger", "rul"), _e("trigger", "anomaly"), _e("rul", "decide"), _e("anomaly", "decide"),
               _e("decide", "gate"), _e("gate", "approve", "true"), _e("gate", "monitor", "false"), _e("approve", "wo")]},
    {"id": "tpl-safety", "name": "Safety report triage", "trigger": "safety_report",
     "description": "Calibrated triage with abstention; high-priority or uncertain reports go to the safety board.",
     "nodes": [_n("trigger", "trigger.safety_report", 0, 160), _n("triage", "model.safety_triage", 228, 160),
               _n("evidence", "rag.retrieve", 457, 160, "Reporting guidance", query="safety reporting ASRS not verified review", k=3),
               _n("route", "logic.condition", 686, 160, "Board review?", expression="triage.high_priority or triage.abstained"),
               _n("approve", "human.approval", 915, 60, "Safety lead approval", title="Route report to the safety board?"),
               _n("email", "notify.email", 1144, 60, "Email safety board", to="safety-board@aeronexus.example",
                  subject="Safety report for review", body="Triage {{triage.decision}} (p={{triage.probability}})."),
               _n("file", "db.save_case", 915, 280, "File as routine", kind="safety", status="verified", title="Routine safety report")],
     "edges": [_e("trigger", "triage"), _e("triage", "evidence"), _e("evidence", "route"), _e("route", "approve", "true"),
               _e("route", "file", "false"), _e("approve", "email")]},
    {"id": "tpl-quote", "name": "New drawing → extraction and catalog check", "trigger": "new_drawing",
     "description": "OCR and extraction cascade; clean drawings are checked against the ERP catalog, others go to review.",
     "nodes": [_n("trigger", "trigger.new_drawing", 0, 160), _n("ocr", "doc.ocr_extract", 228, 160),
               _n("clean", "logic.condition", 457, 160, "All fields confident?", expression="ocr.needs_review == 0"),
               _n("catalog", "api.erp_read", 686, 60, "Catalog lookup", tool="erp.find_item",
                  args={"material": "{{ocr.fields.material.value}}", "thickness": "{{ocr.fields.thickness.value}}"}),
               _n("ready", "db.save_case", 915, 60, "Ready to quote", kind="quote", status="verified", title="Quote {{ocr.sheet_id}}"),
               _n("review", "db.save_case", 686, 280, "Send to review", kind="quote", status="needs_review",
                  title="Drawing {{ocr.sheet_id}} needs review")],
     "edges": [_e("trigger", "ocr"), _e("ocr", "clean"), _e("clean", "catalog", "true"), _e("clean", "review", "false"),
               _e("catalog", "ready")]},
    {"id": "tpl-notice", "name": "Maintenance alert → evidence-backed notice", "trigger": "maintenance_alert",
     "description": "Combine the model's estimate with the cited maintenance policy and notify planning after approval.",
     "nodes": [_n("trigger", "trigger.maintenance_alert", 0, 160), _n("rul", "model.rul", 228, 160),
               _n("policy", "rag.answer", 457, 160, "Policy check", question="When must an inspection work order be opened?"),
               _n("approve", "human.approval", 686, 160, "Planner approval",
                  title="Send planning notice for {{trigger.subset}}-ENG-{{trigger.unit}}?"),
               _n("email", "notify.email", 915, 160, "Notify planning", to="planning@aeronexus.example",
                  subject="Engine {{trigger.subset}}-{{trigger.unit}}: remaining life {{rul.rul}} cycles",
                  body="90% range {{rul.low}}–{{rul.high}} cycles. Policy: {{policy.answer}}")],
     "edges": [_e("trigger", "rul"), _e("rul", "policy"), _e("policy", "approve"), _e("approve", "email")]},
    {"id": "tpl-resilience", "name": "Resilience drill: retries and fallbacks", "trigger": "manual",
     "description": "A flaky service retries with backoff, then the workflow continues without it.",
     "nodes": [_n("trigger", "trigger.manual", 0, 160),
               _n("flaky", "debug.flaky", 228, 60, "Flaky supplier API", retries=2, on_error="continue"),
               _n("cases", "db.query_cases", 228, 260, "Open review cases", status="needs_review"),
               _n("summary", "transform.map", 457, 160, "Summary", mapping={"open_reviews": "{{cases.count}}",
                                                                           "supplier_api": "{{flaky.status}}"})],
     "edges": [_e("trigger", "flaky"), _e("trigger", "cases"), _e("flaky", "summary"), _e("cases", "summary")]},
]


# ---------------------------------------------------------------------------------------------
# saved workflows
# ---------------------------------------------------------------------------------------------
class WorkflowStore:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if WF_DIR.exists():
            for p in WF_DIR.glob("*.json"):
                try:
                    d = json.loads(p.read_text())
                    self.items[d["id"]] = d
                except (ValueError, KeyError):
                    continue
        if not self.items:  # first start: install the templates as editable workflows
            for t in TEMPLATES:
                self.save({**copy.deepcopy(t), "id": t["id"].replace("tpl-", "wf-"), "enabled": t["id"] != "tpl-resilience"},
                          "system")

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            self._load()
            return sorted(self.items.values(), key=lambda d: d.get("name", ""))

    def get(self, wid: str) -> dict[str, Any]:
        with self.lock:
            self._load()
            if wid not in self.items:
                raise KeyError(wid)
            return self.items[wid]

    def save(self, d: dict[str, Any], user: str) -> dict[str, Any]:
        v = validate(d)
        if not v["valid"]:
            raise ValueError("; ".join(v["errors"]))
        wid = d.get("id") or f"wf-{uuid.uuid4().hex[:8]}"
        prev = self.items.get(wid)
        trig = next(n["type"] for n in d["nodes"] if CATALOG[n["type"]]["category"] == "trigger").split(".", 1)[1]
        rec = {"id": wid, "name": str(d.get("name") or "Untitled workflow")[:120], "description": str(d.get("description") or "")[:500],
               "trigger": trig, "enabled": bool(d.get("enabled", True)), "nodes": d["nodes"], "edges": d["edges"],
               "version": (prev or {}).get("version", 0) + 1, "updated_by": user, "updated": time.time(),
               "versions": ((prev or {}).get("versions", []) + ([{"version": prev["version"], "nodes": prev["nodes"],
                                                                   "edges": prev["edges"], "updated_by": prev.get("updated_by")}]
                                                                 if prev else []))[-10:]}
        self.items[wid] = rec
        try:
            WF_DIR.mkdir(parents=True, exist_ok=True)
            (WF_DIR / f"{wid}.json").write_text(json.dumps(rec, default=str))
        except OSError:
            pass
        return rec

    def delete(self, wid: str) -> None:
        with self.lock:
            self.items.pop(wid, None)
        try:
            (WF_DIR / f"{wid}.json").unlink(missing_ok=True)
        except OSError:
            pass


workflows = WorkflowStore()


# ---------------------------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------------------------
def _ctx(run: RunRecord, who: Principal, mode: str, sandbox: tuple[AgentRuntime, CaseStore] | None = None) -> Ctx:
    if mode == "live":
        return Ctx(run=run, runtime=services().runtime, cases=services().cases, user=who.user, mode=mode)
    rt, cs = sandbox or (AgentRuntime(), CaseStore(":memory:"))
    return Ctx(run=run, runtime=rt, cases=cs, user=who.user, mode=mode)


def run_workflow(defn: dict[str, Any], payload: dict[str, Any], who: Principal, mode: str = "live",
                 faults: dict[str, Any] | None = None, background: bool = True, replay_of: str | None = None,
                 sandbox: tuple[AgentRuntime, CaseStore] | None = None) -> RunRecord:
    if mode not in ("live", "test", "replay"):
        raise ValueError("mode must be live, test or replay")
    v = validate(defn)
    if not v["valid"]:
        raise ValueError("; ".join(v["errors"]))
    snap = {k: defn.get(k) for k in ("id", "name", "version", "nodes", "edges")}
    run = store.new("workflow", str(defn.get("name") or "Workflow"), mode, who.user or "anonymous", who.tenant, payload or {},
                    definition=snap, faults=faults or {}, replay_of=replay_of)
    ctx = _ctx(run, who, mode, sandbox)
    graph = compile_workflow(snap, run, ctx)
    start(run, graph, ctx, {"trigger": payload or {}, "results": {}, "errors": [], "approval": {}}, background=background)
    return run


def fire_trigger(kind: str, payload: dict[str, Any], who: Principal) -> list[dict[str, Any]]:
    started = []
    for wf in workflows.list():
        if wf.get("enabled") and wf.get("trigger") == kind:
            r = run_workflow(wf, payload, who, "live")
            started.append({"workflow": wf["id"], "name": wf["name"], "run_id": r.id})
    return started


# ---------------------------------------------------------------------------------------------
# test batches
# ---------------------------------------------------------------------------------------------
def dataset_for(trigger: str, n: int, seed: int = 7) -> list[dict[str, Any]]:
    import random

    rng = random.Random(seed)
    if trigger in ("sensor_anomaly", "maintenance_alert"):
        labelled = trigger == "sensor_anomaly"  # an alert-driven notice is always sent; nothing to score
        from ..twin import _true_rul, engines

        units = engines("FD001")
        rng.shuffle(units)
        tr = _true_rul("FD001")
        return [{"payload": {"subset": "FD001", "unit": u}, "expected_escalation": bool(tr[u - 1] <= 30) if labelled else None,
                 "source": "NASA C-MAPSS FD001 test engines (public; simulated engines)"} for u in units[:n]]
    if trigger == "change_request":
        pairs = services().change_pairs[:]
        rng.shuffle(pairs)
        return [{"payload": {"pair_id": p["id"]}, "expected_escalation": p["truth"].get("severity") in ("critical", "high", "medium"),
                 "source": "synthetic revision pairs (golden set)"} for p in pairs[:n]]
    if trigger == "safety_report":
        from ..safety.ntsb import load_events

        df = load_events()
        te = df[df.year >= 2017]
        pos, neg = te[te.fatal == 1], te[te.fatal == 0]
        k = max(1, round(n * 0.3))  # enrich the rare class so a small batch still measures recall
        sample = list(zip(pos.sample(min(k, len(pos)), random_state=seed)["text"], [True] * k)) + \
            list(zip(neg.sample(min(n - k, len(neg)), random_state=seed)["text"], [False] * (n - k)))
        rng.shuffle(sample)
        return [{"payload": {"text": t}, "expected_escalation": f, "source": "NTSB 2017–22 held-out narratives (public; 30% fatal)"}
                for t, f in sample]
    if trigger == "new_drawing":
        sheets = services().golden_sheets[:]
        rng.shuffle(sheets)
        return [{"payload": {"sheet_id": s.id}, "expected_escalation": None, "source": "synthetic golden drawings"} for s in sheets[:n]]
    return [{"payload": {}, "expected_escalation": None, "source": "manual"} for _ in range(n)]


def _escalated(run: RunRecord) -> bool:
    if run.approvals:
        return True
    return any(v.get("status") == "needs_review" for v in run.outputs.values() if isinstance(v, dict) and "case_id" in v)


def _extraction_accuracy(run: RunRecord) -> float | None:
    for v in run.outputs.values():
        if isinstance(v, dict) and v.get("_truth") and "fields" in v:
            truth = v["_truth"]
            keys = [k for k, t in truth.items() if t is not None]
            ok = sum(str(v["fields"][k]["value"] or "").strip().lower() == str(truth[k]).strip().lower() for k in keys)
            return ok / len(keys) if keys else None
    return None


def run_test_batch(defn: dict[str, Any], who: Principal, n: int = 12, faults: dict[str, Any] | None = None,
                   background: bool = True) -> RunRecord:
    v = validate(defn)
    if not v["valid"]:
        raise ValueError("; ".join(v["errors"]))
    n = max(1, min(int(n), 40))
    trig = next(x["type"] for x in defn["nodes"] if CATALOG[x["type"]]["category"] == "trigger").split(".", 1)[1]
    batch = store.new("test_batch", f"Test: {defn.get('name') or 'workflow'}", "test", who.user, who.tenant,
                      {"n": n, "trigger": trig}, definition={k: defn.get(k) for k in ("id", "name", "version", "nodes", "edges")},
                      faults=faults or {})

    def go() -> None:
        try:
            items = dataset_for(trig, n)
            sandbox = (AgentRuntime(), CaseStore(":memory:"))
            rows = []
            for i, it in enumerate(items):
                r = run_workflow(defn, it["payload"], who, "test", faults, background=False, sandbox=sandbox)
                esc = _escalated(r)
                exp = it["expected_escalation"]
                executed = [nid for nid, s in r.nodes.items() if s.get("status") in ("completed", "failed")]
                manual = sum(float((next(x for x in defn["nodes"] if x["id"] == nid).get("config") or {}).get(
                    "manual_minutes", CATALOG[r.nodes[nid]["type"]]["manual_minutes"])) for nid in executed if nid in r.nodes)
                review = sum(CATALOG["human.approval"]["manual_minutes"] for _ in r.approvals)
                row = {"run_id": r.id, "payload": it["payload"], "status": r.status, "ms": r.metrics.get("duration_ms"),
                       "retries": r.metrics.get("retries", 0), "escalated": esc, "expected": exp,
                       "correct": None if exp is None else esc == exp, "extraction_accuracy": _extraction_accuracy(r),
                       "failed_step": next((nid for nid, s in r.nodes.items() if s.get("status") == "failed"), None),
                       "manual_minutes": manual, "review_minutes": review, "source": it["source"]}
                rows.append(row)
                store.emit(batch, "test_item", None, f"{i + 1}/{len(items)} {r.status}"
                           + ("" if exp is None else f" · escalated {esc}, expected {exp}"), data=row)
            batch.outputs["rows"] = rows
            batch.metrics = _batch_metrics(rows)
            store.finish(batch, "completed")
        except Exception as exc:  # noqa: BLE001
            store.finish(batch, "failed", f"{type(exc).__name__}: {exc}")

    if background:
        threading.Thread(target=go, daemon=True).start()
    else:
        go()
    return batch


def _batch_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    ms = sorted(r["ms"] or 0 for r in rows)
    lab = [r for r in rows if r["expected"] is not None]
    tp = sum(r["escalated"] and r["expected"] for r in lab)
    fp = sum(r["escalated"] and not r["expected"] for r in lab)
    fn = sum((not r["escalated"]) and r["expected"] for r in lab)
    ext = [r["extraction_accuracy"] for r in rows if r["extraction_accuracy"] is not None]
    manual = sum(r["manual_minutes"] for r in rows)
    review = sum(r["review_minutes"] for r in rows)
    machine = sum(ms) / 60000
    return {
        "runs": n, "completed": sum(r["status"] == "completed" for r in rows), "failed": sum(r["status"] == "failed" for r in rows),
        "failure_rate": round(sum(r["status"] == "failed" for r in rows) / n, 4) if n else 0.0,
        "retries": sum(r["retries"] for r in rows), "latency_ms_p50": ms[n // 2] if n else 0,
        "latency_ms_p95": ms[min(n - 1, int(0.95 * n))] if n else 0, "latency_ms_mean": round(statistics.fmean(ms), 1) if n else 0,
        "escalation": None if not lab else {
            "labelled": len(lab), "accuracy": round(sum(r["correct"] for r in lab) / len(lab), 4),
            "precision": round(tp / (tp + fp), 4) if tp + fp else None, "recall": round(tp / (tp + fn), 4) if tp + fn else None,
            "escalated": sum(r["escalated"] for r in rows)},
        "extraction_accuracy": round(statistics.fmean(ext), 4) if ext else None,
        "time": {"manual_minutes_assumed": round(manual, 1), "review_minutes": round(review, 1),
                 "machine_minutes": round(machine, 2), "saved_minutes": round(manual - review - machine, 1),
                 "saved_pct": round(100 * (manual - review - machine) / manual, 1) if manual else None,
                 "note": "Manual minutes are assumptions per step type (editable in each step's settings)."},
        "source": rows[0]["source"] if rows else None,
    }


# ---------------------------------------------------------------------------------------------
# replay + compare
# ---------------------------------------------------------------------------------------------
VOLATILE = {"case_id", "approval_id", "id", "eco_id", "created", "ts", "by", "approved_by", "decided", "sandbox", "run_id",
            "workflow_run", "order_id", "wo", "model"}


def _strip(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _strip(v) for k, v in x.items() if k not in VOLATILE}
    if isinstance(x, list):
        return [_strip(v) for v in x]
    return x


def replay(run_id: str, who: Principal, background: bool = True) -> RunRecord:
    orig = store.get(run_id, who.tenant)
    if orig.kind == "mission":
        from .mission import start_mission

        r = start_mission(orig.inputs, who, faults=orig.faults, mode="replay", background=background)
        r.replay_of = orig.id
        return r
    if not orig.definition:
        raise ValueError("this run has no recorded definition to replay")
    return run_workflow(orig.definition, orig.inputs, who, "replay", orig.faults, background=background, replay_of=orig.id)


def compare(a_id: str, b_id: str, tenant: str) -> dict[str, Any]:
    a, b = store.get(a_id, tenant), store.get(b_id, tenant)
    rows = []
    for nid in sorted(set(a.nodes) | set(b.nodes)):
        na, nb = a.nodes.get(nid, {}), b.nodes.get(nid, {})
        oa, ob = _strip(jsonable(a.outputs.get(nid))), _strip(jsonable(b.outputs.get(nid)))
        changed = []
        if isinstance(oa, dict) and isinstance(ob, dict):
            changed = sorted(k for k in set(oa) | set(ob) if oa.get(k) != ob.get(k))
        rows.append({"node": nid, "label": na.get("label") or nb.get("label") or nid, "status_a": na.get("status"),
                     "status_b": nb.get("status"), "ms_a": na.get("ms"), "ms_b": nb.get("ms"), "same_output": oa == ob,
                     "changed_keys": changed[:12]})
    same = all(r["same_output"] and r["status_a"] == r["status_b"] for r in rows)
    return {"a": a.public(full=False), "b": b.public(full=False), "rows": rows, "identical": same,
            "summary": "Every step produced the same output and status." if same else
            f"{sum(not (r['same_output'] and r['status_a'] == r['status_b']) for r in rows)} step(s) differ."}
