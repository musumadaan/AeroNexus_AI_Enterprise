"""Mission control: a LangGraph supervisor that coordinates five specialist agents.

    START → planner ─┬─ drawing_agent → cost_agent ─┐
                     ├─ pdm_agent ──────────────────┤
                     ├─ safety_agent ───────────────┼→ synthesizer (deferred join) → approval_gate → executor → END
                     └─ evidence_agent ─────────────┘                      (only if actions are proposed)

* The planner asks Llama which agents the mission needs (structured JSON, validated against the allowed
  list); if the model is unavailable or answers badly it falls back to deterministic routing by inputs.
* Agents run in parallel; each has a retry policy and an error policy. A failed specialist does not sink the
  mission: the synthesizer reports what is missing and lowers its confidence.
* The synthesizer writes the brief from agent outputs only, cites evidence ids, and turns findings into
  proposed actions. Every action is a write to another system, so the approval gate pauses the graph until a
  named approver decides. The executor then runs exactly the approved actions through the agent runtime.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from ..agents import AgentRuntime
from ..cases import CaseStore
from ..llm import LLMError, LLMRequest, MockLLM, get_router
from ..security import Principal
from ..services import services
from .engine import FlowState, approval_node, graph_shape, instrument, start
from .library import Ctx, _execute_with_approval, cap_cost, cap_drawing, cap_retrieve, cap_rul, cap_safety
from .runs import RunRecord, TransientError, store

AGENTS = {
    "drawing_agent": "Drawing analysis: compare revisions, find affected records",
    "pdm_agent": "Predictive maintenance: remaining life, health, anomalies",
    "safety_agent": "Safety triage of an incident narrative",
    "evidence_agent": "Evidence retrieval from procedures and standards",
}
LABELS = {"planner": "Planner (supervisor)", "drawing_agent": "Drawing analysis agent", "pdm_agent": "Predictive maintenance agent",
          "safety_agent": "Safety triage agent", "evidence_agent": "Evidence retrieval agent", "cost_agent": "Cost estimation agent",
          "synthesizer": "Synthesizer", "approval_gate": "Human approval gate", "executor": "Executor"}

PRESETS = [
    {"id": "full", "title": "Engine #20 trend + plate revision + stall report",
     "description": "All five agents: an engine nearing its limit, a thickness change in production and an incident narrative.",
     "inputs": {"pair_id": "CH000", "subset": "FD001", "unit": 20,
                "narrative": "The pilot's failure to maintain adequate airspeed during the turn to final, which resulted in an "
                             "aerodynamic stall at low altitude.",
                "question": "When must an inspection work order be opened?"}},
    {"id": "maintenance", "title": "Fleet engine review", "description": "Maintenance and evidence agents only.",
     "inputs": {"subset": "FD001", "unit": 34, "question": "When must an inspection work order be opened?"}},
    {"id": "change", "title": "Engineering change impact", "description": "Drawing and cost agents with evidence.",
     "inputs": {"pair_id": "CH003", "question": "When is an engineering change order issued?"}},
    {"id": "faults", "title": "Resilience drill (injected faults)",
     "description": "Same as the full mission, but the safety agent times out twice and evidence retrieval fails for good.",
     "inputs": {"pair_id": "CH000", "subset": "FD001", "unit": 20,
                "narrative": "Loss of engine power due to fuel exhaustion during cruise, followed by a forced landing.",
                "question": "When must an inspection work order be opened?"},
     "faults": {"safety_agent": 2, "_permanent": ["evidence_agent"]}},
]


def _rule_plan(inputs: dict[str, Any]) -> list[str]:
    out = []
    if inputs.get("pair_id"):
        out.append("drawing_agent")
    if inputs.get("subset") and inputs.get("unit"):
        out.append("pdm_agent")
    if len(str(inputs.get("narrative") or "")) >= 20:
        out.append("safety_agent")
    if inputs.get("question") or out:
        out.append("evidence_agent")
    return out


@MockLLM.register("mission_plan")
def _mock_plan(req: LLMRequest, model: str) -> str:
    agents = _rule_plan(req.payload.get("inputs", {}))
    return json.dumps({"agents": agents, "rationale": "Each agent is chosen because its input is present.", "confidence": 0.9})


def cap_planner(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    inputs = state.get("trigger", {})
    prompt = ("You are the supervisor of an aerospace engineering agent team. Pick which specialist agents this mission needs.\n"
              + "\n".join(f"- {k}: {v}" for k, v in AGENTS.items())
              + f"\n\nMission inputs (JSON): {json.dumps({k: v for k, v in inputs.items() if k != 'faults'})}\n"
                "Return JSON with keys agents (list of agent ids), rationale (one sentence), confidence (0-1).")
    source = "llm"
    try:
        resp = get_router().complete(LLMRequest(task="mission_plan", prompt=prompt, payload={"inputs": inputs},
                                                json_schema={"agents": "list", "rationale": "str", "confidence": "float"},
                                                max_tokens=200))
        p = resp.parsed or {}
        agents = [a for a in p.get("agents", []) if a in AGENTS]
        needed = set(_rule_plan(inputs))
        if set(agents) - needed:
            ctx.emit("guardrail", None, f"Dropped agents with no input: {sorted(set(agents) - needed)}", level="warn")
            agents = [a for a in agents if a in needed]
        if not agents:
            raise LLMError("empty plan")
        rationale, model = p.get("rationale", ""), resp.model
    except LLMError as exc:
        agents, rationale, model, source = _rule_plan(inputs), f"Rule-based routing ({exc})", "rules", "rules"
    ctx.emit("decision", None, f"Plan: {', '.join(agents) or 'nothing to do'} — {rationale}", data={"model": model})
    return {"agents": agents, "rationale": rationale, "model": model, "source": source}


def cap_pdm_agent(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    from .library import cap_anomaly

    r = cap_rul(cfg, state, ctx)
    a = cap_anomaly(cfg, state, ctx)
    return {**r, "anomalies": a["events"][:8], "n_alerts": a["n_alerts"], "first_alert_cycle": a["first_alert_cycle"]}


def cap_evidence(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    from .library import cap_answer

    q = state.get("trigger", {}).get("question") or "inspection work order remaining useful life lower bound"
    r = cap_retrieve({"query": q, "k": 4}, state, ctx)
    ans = cap_answer({"question": q}, state, ctx)
    return {"query": q, "hits": r["hits"], "answer": ans["answer"], "decision": ans["decision"],
            "groundedness": ans["groundedness"], "citations": ans["citations"]}


def cap_synth(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    res = state.get("results", {})
    plan = (res.get("planner") or {}).get("agents", [])
    lines, proposals, missing = [], [], []
    risk = "low"

    def bump(level: str) -> None:
        nonlocal risk
        order = ["low", "medium", "high", "critical"]
        risk = max(risk, level, key=order.index)

    for a in plan + (["cost_agent"] if "drawing_agent" in plan else []):
        out = res.get(a)
        if not out or out.get("status") == "failed":
            missing.append(LABELS[a])
    d = res.get("drawing_agent")
    if d and d.get("status") != "failed":
        ch = "; ".join(f"{c['field']} {c['before']} → {c['after']}" for c in d["changes"]) or "no field changes"
        lines.append(f"Part {d['part_number']}: {ch}. Severity {d['severity']}"
                     + (" (part is in production)" if d["in_production"] else "") + ".")
        bump({"critical": "critical", "high": "high", "medium": "medium"}.get(d["severity"], "low"))
        c = res.get("cost_agent") or {}
        if c.get("available"):
            lines.append(f"Cost impact {c['total']:,.2f} CAD, lead time {c['lead_time_days']} days; "
                         f"{c['receipts_verified']} calculation receipts re-verified.")
        if d["severity"] in ("critical", "high", "medium"):
            proposals.append({"id": "eco", "tool": "erp.create_eco", "summary": f"Release the ECO for {d['part_number']}",
                              "args": {"case_id": ctx.run.id, "part_number": d["part_number"],
                                       "summary": ch[:300], "cost_impact": float(c.get("total") or 0.0),
                                       "effective": dt.date.today().isoformat()}})
            for email in d["impact"].get("customer_emails", [])[:3]:
                proposals.append({"id": f"notify-{email}", "tool": "comms.send_email", "summary": f"Notify {email} of the change",
                                  "args": {"to": email, "subject": f"Engineering change notice – {d['part_number']}",
                                           "body": f"Part {d['part_number']} has an approved engineering change: {ch}."}})
    p = res.get("pdm_agent")
    if p and p.get("status") != "failed":
        lines.append(f"Engine {p['subset']}-{p['unit']}: predicted life {p['rul']} cycles (90% range {p['low']}–{p['high']}), "
                     f"health {p['health']:.3f} vs threshold {p['threshold']:.3f}; {p['n_alerts']} anomaly alert(s).")
        if p["work_order_needed"]:
            bump("high")
            proposals.append({"id": "wo", "tool": "cmms.create_maintenance_order",
                              "summary": f"Raise an inspection work order for {p['subset']}-ENG-{p['unit']:03d}",
                              "args": {"asset": f"{p['subset']}-ENG-{p['unit']:03d}", "task": "Borescope HPC and fan inspection; review trend",
                                       "reason": "; ".join(p["reasons"]) or "rule pdm.work_order",
                                       "due_within_cycles": float(max(1.0, p["low"]))}})
    s = res.get("safety_agent")
    if s and s.get("status") != "failed":
        lines.append(f"Safety narrative triaged as {s['decision'].replace('_', ' ')} (p={s['probability']}); "
                     f"{len(s['similar'])} similar NTSB events found.")
        if s["high_priority"] or s["abstained"]:
            bump("medium")
            proposals.append({"id": "safety", "tool": "comms.send_email", "summary": "Route the report to the safety review board",
                              "args": {"to": "safety-board@aeronexus.example", "subject": "Safety report for review",
                                       "body": f"Triage: {s['decision']} (p={s['probability']}). Similar events: "
                                               + ", ".join(x['id'] for x in s['similar'])}})
    e = res.get("evidence_agent")
    if e and e.get("status") != "failed":
        lines.append(f"Procedure check ({e['decision']}): {e['answer']}")
    confidence = round(max(0.2, 0.95 - 0.2 * len(missing)), 2)
    if missing:
        lines.append(f"Missing inputs: {', '.join(missing)} failed; confidence lowered and those findings need a manual check.")
    brief = " ".join(lines) or "No agent produced findings."
    ctx.emit("decision", None, f"Overall risk {risk}; {len(proposals)} action(s) proposed; confidence {confidence}")
    return {"risk": risk, "brief": brief, "lines": lines, "proposals": proposals, "missing": missing, "confidence": confidence}


def cap_executor(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    props = (state.get("results", {}).get("synthesizer") or {}).get("proposals", [])
    done = []
    for p in props:
        r = _execute_with_approval(p["tool"], p["args"], state, ctx, p["summary"])
        done.append({"id": p["id"], "tool": p["tool"], "status": r["status"]})
    return {"executed": done, "count": len(done)}


def mission_graph(run: RunRecord, ctx: Ctx) -> Any:
    g = StateGraph(FlowState)
    spec = {"planner": cap_planner, "drawing_agent": cap_drawing, "pdm_agent": cap_pdm_agent, "safety_agent": cap_safety,
            "evidence_agent": cap_evidence, "cost_agent": cap_cost, "synthesizer": cap_synth, "executor": cap_executor}
    cfgs = {"drawing_agent": {"pair_id": "{{trigger.pair_id}}", "on_error": "continue"},
            "pdm_agent": {"subset": "{{trigger.subset}}", "unit": "{{trigger.unit}}", "on_error": "continue"},
            "safety_agent": {"text": "{{trigger.narrative}}", "on_error": "continue"},
            "evidence_agent": {"on_error": "continue"}, "cost_agent": {"source": "drawing_agent", "on_error": "continue"}}
    for nid, fn in spec.items():
        cfg = {"retries": 2, **cfgs.get(nid, {})}
        store.node_update(run, nid, status="pending", type=f"mission.{nid}", label=LABELS[nid], attempts=0)
        g.add_node(nid, instrument(run, ctx, nid, f"agent.{nid}", cfg, LABELS[nid], fn, max_attempts=3),
                   defer=nid == "synthesizer",
                   retry_policy=RetryPolicy(max_attempts=3, initial_interval=0.1, backoff_factor=2.0, jitter=False,
                                            retry_on=TransientError))
    store.node_update(run, "approval_gate", status="pending", type="human.approval", label=LABELS["approval_gate"], attempts=0)
    g.add_node("approval_gate", approval_node(run, ctx, "approval_gate",
                                              {"title": "Approve the actions proposed by the mission", "timeout_min": 1440,
                                               "require_distinct_approver": False}, LABELS["approval_gate"]))
    g.add_edge(START, "planner")
    g.add_conditional_edges("planner", lambda s: (s.get("results", {}).get("planner") or {}).get("agents") or ["synthesizer"],
                            list(AGENTS) + ["synthesizer"])
    g.add_edge("drawing_agent", "cost_agent")
    for a in ("cost_agent", "pdm_agent", "safety_agent", "evidence_agent"):
        g.add_edge(a, "synthesizer")
    g.add_conditional_edges("synthesizer", lambda s: "approval_gate" if (s.get("results", {}).get("synthesizer") or {}).get("proposals")
                            else END, ["approval_gate", END])
    g.add_conditional_edges("approval_gate", lambda s: "executor" if (s.get("results", {}).get("approval_gate") or {}).get("approved")
                            else END, ["executor", END])
    g.add_edge("executor", END)
    return g.compile(checkpointer=InMemorySaver())


def start_mission(inputs: dict[str, Any], who: Principal, faults: dict[str, Any] | None = None, mode: str = "live",
                  background: bool = True) -> RunRecord:
    clean = {k: inputs.get(k) for k in ("title", "pair_id", "subset", "unit", "narrative", "question") if inputs.get(k) not in (None, "")}
    if "unit" in clean:
        clean["unit"] = int(clean["unit"])
    run = store.new("mission", str(clean.get("title") or "Mission"), mode, who.user or "anonymous", who.tenant, clean,
                    faults=faults or {})
    rt = services().runtime if mode == "live" else AgentRuntime()
    cases = services().cases if mode == "live" else CaseStore(":memory:")
    ctx = Ctx(run=run, runtime=rt, cases=cases, user=who.user, mode=mode)
    graph = mission_graph(run, ctx)
    start(run, graph, ctx, {"trigger": clean, "results": {}, "errors": [], "approval": {}}, background=background)
    return run


def shape() -> dict[str, Any]:
    dummy = RunRecord(id="shape", kind="mission", name="shape", mode="test", created_by="", tenant="", inputs={})
    ctx = Ctx(run=dummy, runtime=services().runtime, cases=None, user="", mode="test")
    s = graph_shape(mission_graph(dummy, ctx))
    for n in s["nodes"]:
        n["label"] = LABELS.get(n["id"], n["id"])
    return s
