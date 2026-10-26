"""Node library: every capability an agent or a workflow step can use, with its catalog entry.

A node is `fn(cfg, state, ctx) -> dict`. `cfg` has already had its {{templates}} rendered against the run
state, so a step can reference earlier outputs ("{{trigger.unit}}", "{{rul.low}}"). Risk tiers follow the
agent runtime: read and internal steps run freely; write and external steps need an approval upstream
(the builder refuses to save a workflow that could reach them without one).

`manual_minutes` is the assumed hands-on time a person spends on the same step. It feeds the "time saved"
estimate in test runs, is labelled as an assumption in the UI and can be edited per node.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..agents import AgentRuntime
from ..calc import calculate, verify_receipt
from ..llm import LLMRequest, MockLLM, get_router
from ..services import services
from .runs import RunRecord, TransientError, store

# ---------------------------------------------------------------------------------------------
# context, templating, safe expressions
# ---------------------------------------------------------------------------------------------


@dataclass
class Ctx:
    run: RunRecord
    runtime: AgentRuntime
    cases: Any
    user: str
    mode: str  # live | test | replay
    extra: dict[str, Any] = field(default_factory=dict)

    def emit(self, type_: str, node: str | None, message: str, level: str = "info", data: Any = None) -> None:
        store.emit(self.run, type_, node, message, level, data)


TEMPLATE = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")


def scope_of(state: dict[str, Any]) -> dict[str, Any]:
    return {"trigger": state.get("trigger", {}), "approval": state.get("approval", {}), **state.get("results", {})}


def lookup(scope: dict[str, Any], path: str) -> Any:
    cur: Any = scope
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def render(value: Any, scope: dict[str, Any]) -> Any:
    if isinstance(value, str):
        m = TEMPLATE.fullmatch(value.strip())
        if m:  # a lone template keeps the referenced value's type
            return lookup(scope, m.group(1))
        return TEMPLATE.sub(lambda mm: "" if lookup(scope, mm.group(1)) is None else str(lookup(scope, mm.group(1))), value)
    if isinstance(value, dict):
        return {k: render(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, scope) for v in value]
    return value


_ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.Compare, ast.Eq, ast.NotEq,
            ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot, ast.BinOp, ast.Add, ast.Sub, ast.Mult,
            ast.Div, ast.Name, ast.Attribute, ast.Constant, ast.Load, ast.List, ast.Tuple, ast.Subscript)


def safe_eval(expr: str, scope: dict[str, Any]) -> Any:
    """Tiny expression language for conditions: comparisons, and/or/not, + - * /, dotted paths. No calls."""
    tree = ast.parse(expr, mode="eval")
    for n in ast.walk(tree):
        if not isinstance(n, _ALLOWED):
            raise ValueError(f"not allowed in a condition: {type(n).__name__}")

    def ev(n: ast.AST) -> Any:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            return {"true": True, "false": False, "none": None}.get(n.id, scope.get(n.id))
        if isinstance(n, ast.Attribute):
            base = ev(n.value)
            return base.get(n.attr) if isinstance(base, dict) else None
        if isinstance(n, ast.Subscript):
            base, key = ev(n.value), ev(n.slice)
            try:
                return base[key]
            except (KeyError, IndexError, TypeError):
                return None
        if isinstance(n, (ast.List, ast.Tuple)):
            return [ev(e) for e in n.elts]
        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            return (not v) if isinstance(n.op, ast.Not) else -v
        if isinstance(n, ast.BoolOp):
            vals = [ev(v) for v in n.values]
            return all(vals) if isinstance(n.op, ast.And) else any(vals)
        if isinstance(n, ast.BinOp):
            a, b = ev(n.left), ev(n.right)
            return {ast.Add: lambda: a + b, ast.Sub: lambda: a - b, ast.Mult: lambda: a * b, ast.Div: lambda: a / b}[type(n.op)]()
        if isinstance(n, ast.Compare):
            left = ev(n.left)
            for op, comp in zip(n.ops, n.comparators):
                right = ev(comp)
                try:
                    ok = {ast.Eq: lambda: left == right, ast.NotEq: lambda: left != right, ast.Lt: lambda: left < right,
                          ast.LtE: lambda: left <= right, ast.Gt: lambda: left > right, ast.GtE: lambda: left >= right,
                          ast.In: lambda: left in right, ast.NotIn: lambda: left not in right,
                          ast.Is: lambda: left is right, ast.IsNot: lambda: left is not right}[type(op)]()
                except TypeError:  # comparing None with a number → condition is false, not a crash
                    ok = False
                if not ok:
                    return False
                left = right
            return True
        raise ValueError(type(n).__name__)

    return ev(tree)


def _audit_delta(ctx: Ctx, node: str, before: int) -> list[dict[str, Any]]:
    """Report the ERP/CRM tool calls a step made (from the runtime audit log) as timeline events."""
    calls = []
    for a in ctx.runtime.audit[before:]:
        tool = a.detail.get("tool")
        if tool:
            calls.append({"tool": tool, "action": a.action, "args": a.detail.get("args")})
            ctx.emit("tool_call", node, f"{a.action}: {tool}", data={"args": a.detail.get("args")})
    return calls


# ---------------------------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------------------------
TRIGGER_DEFAULTS = {
    "trigger.manual": {},
    "trigger.new_drawing": {"sheet_id": "G0001"},
    "trigger.sensor_anomaly": {"subset": "FD001", "unit": 20},
    "trigger.maintenance_alert": {"subset": "FD001", "unit": 34, "message": "Scheduled review: low remaining-life estimate"},
    "trigger.change_request": {"pair_id": "CH000"},
    "trigger.safety_report": {"text": "The pilot's failure to maintain adequate airspeed during the turn to final, which resulted "
                                      "in an aerodynamic stall at low altitude."},
}


def cap_trigger(kind: str) -> Callable[..., dict[str, Any]]:
    def run(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
        payload = {**TRIGGER_DEFAULTS.get(kind, {}), **{k: v for k, v in (state.get("trigger") or {}).items() if v not in (None, "")}}
        if "unit" in payload:
            payload["unit"] = int(payload["unit"])
        return {"type": kind.split(".", 1)[1], **payload}
    return run


def cap_ocr(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    sheet = services().sheet(str(cfg.get("sheet_id") or "G0001"))
    r = services().pipeline.run(sheet)
    fields = {k: {"value": v.value, "confidence": round(v.confidence, 3), "status": v.status, "source": getattr(v, "source", None)}
              for k, v in r.fields.items()}
    for k, v in fields.items():
        if v["status"] != "auto_accepted":
            ctx.emit("decision", None, f"{k}: {v['status'].replace('_', ' ')} (confidence {v['confidence']})")
    truth = sheet.meta.get("truth", {})
    return {"sheet_id": sheet.id, "fields": fields, "warnings": r.warnings,
            "auto_accepted": sum(v["status"] == "auto_accepted" for v in fields.values()),
            "needs_review": sum(v["status"] != "auto_accepted" for v in fields.values()),
            "_truth": {k: truth.get(k) for k in fields} if truth else None}


def cap_drawing(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    from ..extraction.document import Sheet
    from ..modules import change_order as co

    pair_id = str(cfg.get("pair_id") or "CH000")
    pair = next((p for p in services().change_pairs if p["id"] == pair_id), None)
    if pair is None:
        raise ValueError(f"unknown revision pair {pair_id}")
    a, b = Sheet.from_dict(pair["before"]), Sheet.from_dict(pair["after"])
    before = len(services().runtime.audit)
    data: dict[str, Any] = {}
    for name, step in (("extract", co.step_extract(a, b)), ("diff", co.step_diff(a, b)), ("validate", co.step_validate),
                       ("impact", co.step_impact(pair["part_number"])), ("cost", co.step_cost), ("classify", co.step_classify)):
        data[name] = step(data)
        ctx.emit("step", None, f"drawing analysis · {name} done")
    tool_calls = []
    for ev in services().runtime.audit[before:]:
        if ev.detail.get("tool"):
            tool_calls.append(ev.detail["tool"])
            ctx.emit("tool_call", None, f"read {ev.detail['tool']}", data={"args": ev.detail.get("args")})
    imp, cost = data["impact"], data["cost"]
    changes = [{k: c.get(k) for k in ("field", "before", "after", "delta_mm", "delta_pct", "confidence", "uncertain")}
               for c in data["diff"]["changes"] if c["field"] != "revision"]
    return {"pair_id": pair_id, "part_number": pair["part_number"], "changes": changes, "findings": data["validate"]["findings"],
            "severity": data["classify"]["severity"], "in_production": imp["in_production"],
            "impact": {"assemblies": imp["assemblies"], "open_orders": [o["order_id"] for o in imp["open_orders"]],
                       "work_orders": [w["wo"] for w in imp["work_orders"]], "material_pos": [p["po"] for p in imp["material_pos"]],
                       "customers": [x["name"] for x in imp["accounts"]],
                       "customer_emails": [x["email"] for x in imp["accounts"]],
                       "new_supplier": (imp["new_supplier"] or {}).get("name")},
            "cost": {"available": cost.get("available", False), "total": (cost.get("cost") or {}).get("total"),
                     "breakdown": cost.get("cost"), "unit_before": cost.get("unit_before"), "unit_after": cost.get("unit_after"),
                     "lead_time_days": cost.get("lead_time_days"), "receipts": cost.get("receipts", []) + [data["classify"]["receipt"]]},
            "tool_calls": tool_calls, "_truth": pair.get("truth")}


def cap_cost(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    src = cfg.get("source") or next((k for k, v in state.get("results", {}).items() if isinstance(v, dict) and "cost" in v
                                     and isinstance(v["cost"], dict) and "receipts" in v["cost"]), None)
    d = state.get("results", {}).get(src) if src else None
    if not d or not d.get("cost", {}).get("available"):
        return {"available": False, "reason": "no drawing analysis with a computable cost upstream"}
    receipts = d["cost"]["receipts"]
    verified = [verify_receipt(r) for r in receipts]
    for r, ok in zip(receipts, verified):
        ctx.emit("calculation", None, f"{r['formula']} → {r['receipt_hash'][:10]} {'re-computed exactly' if ok else 'DID NOT reproduce'}",
                 level="info" if ok else "error", data={"inputs": r["inputs"], "output": r["output"]})
    if not all(verified):
        raise RuntimeError("a calculation receipt did not reproduce; refusing to report the cost")
    b = d["cost"]["breakdown"] or {}
    return {"available": True, "total": d["cost"]["total"], "currency": "CAD", "breakdown": b,
            "lead_time_days": d["cost"]["lead_time_days"], "receipts_verified": len(receipts),
            "trace": [{"formula": r["formula"], "inputs": r["inputs"], "output": r["output"], "rules": r["rules"],
                       "receipt": r["receipt_hash"]} for r in receipts]}


def cap_rul(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    from ..twin import twin_state

    subset, unit = str(cfg.get("subset") or "FD001"), int(cfg.get("unit") or 20)
    t = twin_state(subset, unit, int(cfg["cycle"]) if cfg.get("cycle") not in (None, "") else None)
    if not t.get("available"):
        raise RuntimeError(t.get("message", "models not trained"))
    wo, rec = calculate("pdm.work_order_needed", rul_low=t["rul"]["low"], health=t["health"], health_threshold=t["health_threshold"])
    ctx.emit("decision", None, f"Rule pdm.work_order → {'inspection needed' if wo['needed'] else 'no inspection'}"
             + (f": {'; '.join(wo['reasons'])}" if wo["reasons"] else ""))
    return {"subset": subset, "unit": unit, "cycle": t["cycle"], "rul": t["rul"]["pred"], "low": t["rul"]["low"],
            "high": t["rul"]["high"], "health": t["health"], "threshold": t["health_threshold"],
            "work_order_needed": wo["needed"], "reasons": wo["reasons"], "receipt": rec.receipt_hash,
            "poor_components": [c["label"] for c in t["components"] if c["status"] == "poor"],
            "_truth": {"true_rul": t["rul"]["true"]}}


def cap_anomaly(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    from ..twin import telemetry

    subset, unit = str(cfg.get("subset") or "FD001"), int(cfg.get("unit") or 20)
    t = telemetry(subset, unit)
    ev = t["events"]
    for e in ev[:6]:
        ctx.emit("decision", None, f"cycle {e['cycle']}: {e['message']}", level="warn" if e["severity"] == "alert" else "info")
    alerts = [e for e in ev if e["severity"] == "alert"]
    return {"subset": subset, "unit": unit, "events": ev, "n_events": len(ev), "n_alerts": len(alerts),
            "first_alert_cycle": alerts[0]["cycle"] if alerts else None, "anomalous": bool(alerts)}


def cap_safety(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    from ..modules.safety import similar, triage

    text = str(cfg.get("text") or "")[:4000]
    if len(text) < 20:
        raise ValueError("safety triage needs a narrative of at least 20 characters")
    t = triage(text)
    if not t.get("available"):
        raise RuntimeError(t.get("message", "safety model not trained"))
    sim = similar(text[:2000], k=3)
    ctx.emit("decision", None, f"Triage: {t['decision'].replace('_', ' ')} (p={t['probability']}, threshold {t['threshold']:.2f})")
    return {"decision": t["decision"], "probability": t["probability"], "threshold": t["threshold"],
            "high_priority": t["decision"] == "high_priority", "abstained": t["decision"] == "abstain",
            "similar": [{"id": s["id"], "title": s.get("title"), "year": s.get("year"), "fatal": s.get("fatal"),
                         "text": (s.get("text") or "")[:220]} for s in sim], "note": t["note"]}


def cap_retrieve(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    q = str(cfg.get("query") or "")[:500]
    if not q.strip():
        raise ValueError("retrieval needs a query")
    hits = services().index.search(q, k=int(cfg.get("k") or 4))
    out = [{"id": h.chunk.id, "title": h.chunk.meta.get("title"), "section": h.chunk.meta.get("section"),
            "text": h.chunk.text[:300], "score": round(h.score, 4), "quarantined": h.quarantined} for h in hits]
    ctx.emit("retrieval", None, f"{len(out)} passages for “{q[:60]}”", data={"ids": [o["id"] for o in out]})
    return {"query": q, "hits": out}


def cap_answer(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    from ..generation import grounded_answer

    q = str(cfg.get("question") or "")[:500]
    hits = services().index.search(q, k=5)
    ga = grounded_answer(q, hits, require_coverage=True)
    ctx.emit("decision", None, f"Grounding: {ga.report.decision} (groundedness {ga.report.groundedness:.2f})")
    return {"question": q, "answer": ga.text, "decision": ga.report.decision, "groundedness": round(ga.report.groundedness, 3),
            "citations": [s.get("id") for s in ga.sources][:5], "model": ga.model}


@MockLLM.register("agent_decide")
def _mock_decide(req: LLMRequest, model: str) -> str:
    choices = req.payload.get("choices") or ["escalate", "monitor"]
    text = (req.prompt + json.dumps(req.payload.get("context", {}))).lower()
    # deterministic stand-in for the LLM: escalate only on the strong signals a planner would act on
    risky = any(w in text for w in ("\"severity\": \"critical\"", "\"severity\": \"high\"", "\"high_priority\": true",
                                    "\"work_order_needed\": true"))
    pick = next((c for c in choices if ("escal" in c.lower() or "review" in c.lower()) == risky), choices[0 if risky else -1])
    return json.dumps({"choice": pick, "reason": "risk signals present in the inputs" if risky else "no risk signals in the inputs",
                       "confidence": 0.86})


def cap_llm(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    choices = [c.strip() for c in str(cfg.get("choices") or "escalate,monitor").split(",") if c.strip()][:6]
    context = {k: v for k, v in scope_of(state).items() if k != "approval"}
    ctx_json = json.dumps(context, default=str)[:3500]
    prompt = (f"{cfg.get('prompt') or 'Decide the next action.'}\n\nChoose exactly one of: {', '.join(choices)}.\n"
              f"Context (JSON): {ctx_json}\nReturn JSON with keys choice, reason, confidence (0-1).")
    resp = get_router().complete(LLMRequest(task="agent_decide", prompt=prompt, payload={"choices": choices, "context": context},
                                            json_schema={"choice": "str", "reason": "str", "confidence": "float"}, max_tokens=200))
    p = resp.parsed or {}
    choice = p.get("choice")
    valid = choice in choices
    if not valid:  # never let free text drive routing
        ctx.emit("guardrail", None, f"Model chose '{choice}', not an allowed option; falling back to '{choices[-1]}' for review",
                 level="warn")
        choice = choices[-1]
    ctx.emit("decision", None, f"LLM agent chose '{choice}': {p.get('reason', '')}", data={"model": resp.model,
                                                                                          "escalated": resp.escalated})
    return {"choice": choice, "reason": p.get("reason"), "confidence": p.get("confidence"), "model": resp.model,
            "valid_choice": valid, "escalated_model": resp.escalated}


READ_TOOLS = ["erp.find_item", "erp.open_orders_for", "erp.where_used", "erp.work_orders_for", "erp.material_pos_for",
              "erp.supplier_for", "crm.account_for_part"]
WRITE_TOOLS = ["erp.create_eco", "erp.create_quote", "erp.create_bom", "crm.create_opportunity", "cmms.create_maintenance_order"]


def _coerce_args(tool: str, args: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    schema = ctx.runtime.tools[tool].schema
    out = {}
    for k, t in schema.items():
        if k not in args or args[k] in (None, ""):
            continue
        v = args[k]
        t = t.rstrip("?")
        if v == "today":
            import datetime as _dt

            v = _dt.date.today().isoformat()
        out[k] = float(v) if t == "float" else int(v) if t == "int" else (json.loads(v) if t in ("list", "dict") and isinstance(v, str) else v)
    return out


def cap_api_read(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    tool = cfg.get("tool")
    if tool not in READ_TOOLS:
        raise ValueError(f"{tool} is not a read-only integration")
    args = _coerce_args(tool, cfg.get("args") or {}, ctx)
    r = ctx.runtime.call(tool, args, actor="agent", module="workflow")
    ctx.emit("tool_call", None, f"read {tool}", data={"args": args})
    res = r["result"]
    return {"tool": tool, "args": args, "result": res, "count": len(res) if isinstance(res, list) else 1}


def _execute_with_approval(tool: str, args: dict[str, Any], state: dict[str, Any], ctx: Ctx, reason: str) -> dict[str, Any]:
    appr = state.get("approval") or {}
    if not appr.get("approved") or not appr.get("by"):
        raise PermissionError(f"{tool} needs an approved human checkpoint upstream")
    r = ctx.runtime.call(tool, args, actor="agent", module="workflow", reason=reason, case_id=ctx.run.id)
    ctx.emit("tool_call", None, f"proposed {tool} → {r['status']}", data={"args": args})
    if r["status"] == "pending_approval":
        item = ctx.runtime.decide(r["approval_id"], appr["by"], True)
        ctx.emit("tool_call", None, f"executed {tool}, approved by {appr['by']}", data={"result": item.result})
        if item.status != "executed":
            raise RuntimeError(f"{tool} failed: {item.result}")
        return {"tool": tool, "status": item.status, "approved_by": appr["by"], "result": item.result, "sandbox": ctx.mode != "live"}
    return {"tool": tool, "status": r["status"], "approval_id": r.get("approval_id"), "sandbox": ctx.mode != "live"}


def cap_api_write(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    tool = cfg.get("tool")
    if tool not in WRITE_TOOLS:
        raise ValueError(f"{tool} is not a write integration")
    return _execute_with_approval(tool, _coerce_args(tool, cfg.get("args") or {}, ctx), state, ctx, str(cfg.get("reason") or "workflow"))


def cap_work_order(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    args = {"asset": str(cfg.get("asset") or "ENGINE"), "task": str(cfg.get("task") or "Borescope inspection"),
            "reason": str(cfg.get("reason") or "workflow"), "due_within_cycles": float(cfg.get("due_within_cycles") or 10)}
    return _execute_with_approval("cmms.create_maintenance_order", args, state, ctx, args["reason"])


def cap_email(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    to = cfg.get("to")
    if isinstance(to, list):
        to = to[0] if to else ""
    to = str(to or "")
    if "@" not in to:
        raise ValueError("email needs a recipient address")
    args = {"to": to, "subject": str(cfg.get("subject") or "AeroNexus notification")[:200], "body": str(cfg.get("body") or "")[:4000]}
    return _execute_with_approval("comms.send_email", args, state, ctx, "workflow notification")


def cap_save_case(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    tenant = ctx.run.tenant
    case = ctx.cases.open(tenant, str(cfg.get("kind") or "workflow"), str(cfg.get("title") or ctx.run.name)[:200], ctx.user or "agent")
    summary = {k: (v if len(json.dumps(v, default=str)) < 2000 else "…") for k, v in state.get("results", {}).items()}
    case = ctx.cases.put(case.id, tenant, "agent", workflow_run=ctx.run.id, results=json.loads(json.dumps(summary, default=str)))
    status = str(cfg.get("status") or "needs_review")
    if status in ("verified", "needs_review", "blocked"):
        case = ctx.cases.set_status(case.id, tenant, status, "agent", f"saved by workflow run {ctx.run.id}")
    ctx.emit("db_write", None, f"case {case.id} saved ({case.status})")
    return {"case_id": case.id, "status": case.status, "sandbox": ctx.mode != "live"}


def cap_query_cases(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    st = cfg.get("status") or None
    rows = ctx.cases.list(ctx.run.tenant, st, cfg.get("kind") or None)
    return {"count": len(rows), "ids": [c.id for c in rows[:20]]}


def cap_condition(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    expr = str(cfg.get("expression") or "true")
    val = bool(safe_eval(expr, scope_of(state)))
    ctx.emit("decision", None, f"Condition `{expr}` → {val}")
    return {"result": val, "expression": expr}


def cap_transform(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    mapping = cfg.get("mapping") or {}
    if isinstance(mapping, str):
        mapping = json.loads(mapping or "{}")
    return render(mapping, scope_of(state))


def cap_flaky(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    raise TransientError("upstream timeout")


# ---------------------------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------------------------
def _f(key: str, label: str, kind: str = "text", default: Any = "", options: list[str] | None = None, help: str = "") -> dict[str, Any]:
    return {"key": key, "label": label, "kind": kind, "default": default, "options": options or [], "help": help}


CATALOG: dict[str, dict[str, Any]] = {
    "trigger.manual": {"label": "Manual start", "category": "trigger", "risk": "none", "fn": cap_trigger("trigger.manual"),
                       "description": "Start the workflow by hand with an optional JSON payload.", "fields": [], "manual_minutes": 0},
    "trigger.new_drawing": {"label": "New engineering drawing", "category": "trigger", "risk": "none",
                            "fn": cap_trigger("trigger.new_drawing"), "description": "Fires when a drawing is uploaded or released.",
                            "fields": [_f("sheet_id", "Drawing", default="G0001")], "manual_minutes": 0},
    "trigger.sensor_anomaly": {"label": "Sensor anomaly", "category": "trigger", "risk": "none",
                               "fn": cap_trigger("trigger.sensor_anomaly"), "description": "Fires when the twin flags an engine.",
                               "fields": [_f("subset", "Fleet", "select", "FD001", ["FD001", "FD002", "FD003", "FD004"]),
                                          _f("unit", "Engine", "number", 20)], "manual_minutes": 0},
    "trigger.maintenance_alert": {"label": "Maintenance alert", "category": "trigger", "risk": "none",
                                  "fn": cap_trigger("trigger.maintenance_alert"), "description": "Fires on a scheduled or CMMS alert.",
                                  "fields": [_f("subset", "Fleet", "select", "FD001", ["FD001", "FD002", "FD003", "FD004"]),
                                             _f("unit", "Engine", "number", 34), _f("message", "Message")], "manual_minutes": 0},
    "trigger.change_request": {"label": "Engineering change request", "category": "trigger", "risk": "none",
                               "fn": cap_trigger("trigger.change_request"), "description": "Fires when a new revision is submitted.",
                               "fields": [_f("pair_id", "Revision pair", default="CH000")], "manual_minutes": 0},
    "trigger.safety_report": {"label": "Safety report received", "category": "trigger", "risk": "none",
                              "fn": cap_trigger("trigger.safety_report"), "description": "Fires when a narrative arrives.",
                              "fields": [_f("text", "Narrative", "textarea")], "manual_minutes": 0},
    "doc.ocr_extract": {"label": "Document OCR + extraction", "category": "document", "risk": "read", "fn": cap_ocr,
                        "description": "Text layer / OCR, then regex → NER → LLM cascade with a hallucination guard.",
                        "fields": [_f("sheet_id", "Drawing", default="{{trigger.sheet_id}}")], "manual_minutes": 12},
    "agent.drawing": {"label": "Drawing analysis agent", "category": "agent", "risk": "read", "fn": cap_drawing,
                      "description": "Diffs two revisions, validates, finds affected ERP/CRM records, costs and classifies.",
                      "fields": [_f("pair_id", "Revision pair", default="{{trigger.pair_id}}")], "manual_minutes": 45},
    "agent.llm": {"label": "LLM decision agent", "category": "agent", "risk": "read", "fn": cap_llm,
                  "description": "Llama picks one of the allowed options from the run context; invalid answers fall back to review.",
                  "fields": [_f("prompt", "Instruction", "textarea", "Should this be escalated to an engineer?"),
                             _f("choices", "Allowed choices (comma-separated)", default="escalate,monitor")], "manual_minutes": 5},
    "model.rul": {"label": "Remaining-life model", "category": "model", "risk": "read", "fn": cap_rul,
                  "description": "C-MAPSS RUL with a 90% conformal interval, health score and the work-order rule.",
                  "fields": [_f("subset", "Fleet", default="{{trigger.subset}}"), _f("unit", "Engine", default="{{trigger.unit}}")],
                  "manual_minutes": 20},
    "model.anomaly": {"label": "Anomaly detection", "category": "model", "risk": "read", "fn": cap_anomaly,
                      "description": "Health-threshold, spike and stuck-sensor detectors over the engine history.",
                      "fields": [_f("subset", "Fleet", default="{{trigger.subset}}"), _f("unit", "Engine", default="{{trigger.unit}}")],
                      "manual_minutes": 15},
    "model.safety_triage": {"label": "Safety triage model", "category": "model", "risk": "read", "fn": cap_safety,
                            "description": "Calibrated NTSB model; abstains when unsure; finds similar past events.",
                            "fields": [_f("text", "Narrative", default="{{trigger.text}}")], "manual_minutes": 15},
    "rag.retrieve": {"label": "RAG retrieval", "category": "knowledge", "risk": "read", "fn": cap_retrieve,
                     "description": "Hybrid BM25 + dense search, revision-aware, untrusted text quarantined.",
                     "fields": [_f("query", "Query", default="inspection work order remaining useful life lower bound"),
                                _f("k", "Passages", "number", 4)], "manual_minutes": 10},
    "rag.answer": {"label": "Grounded answer", "category": "knowledge", "risk": "read", "fn": cap_answer,
                   "description": "Answers with per-sentence verified citations, or abstains.",
                   "fields": [_f("question", "Question", default="When must an inspection work order be opened?")], "manual_minutes": 10},
    "calc.change_cost": {"label": "Traceable cost calculation", "category": "calc", "risk": "read", "fn": cap_cost,
                         "description": "Re-verifies every calculation receipt from the drawing analysis before reporting cost.",
                         "fields": [_f("source", "Source node (blank = auto)")], "manual_minutes": 25},
    "api.erp_read": {"label": "ERP / CRM lookup", "category": "integration", "risk": "read", "fn": cap_api_read,
                     "description": "Read-only call to the ERP or CRM connector.",
                     "fields": [_f("tool", "Operation", "select", "erp.open_orders_for", READ_TOOLS),
                                _f("args", "Arguments (JSON)", "json", {"part_number": "{{trigger.part_number}}"})], "manual_minutes": 6},
    "api.erp_write": {"label": "ERP / CRM update", "category": "integration", "risk": "write", "fn": cap_api_write,
                      "description": "Write to the ERP or CRM. Needs an approval upstream.",
                      "fields": [_f("tool", "Operation", "select", "erp.create_eco", WRITE_TOOLS),
                                 _f("args", "Arguments (JSON)", "json", {}), _f("reason", "Reason")], "manual_minutes": 8},
    "api.cmms_work_order": {"label": "CMMS work order", "category": "integration", "risk": "write", "fn": cap_work_order,
                            "description": "Raise an inspection work order. Needs an approval upstream.",
                            "fields": [_f("asset", "Asset", default="{{trigger.subset}}-ENG-{{trigger.unit}}"),
                                       _f("task", "Task", default="Borescope HPC and fan inspection; review trend"),
                                       _f("reason", "Reason", default="RUL low bound {{rul.low}} cycles"),
                                       _f("due_within_cycles", "Due within (cycles)", "number", 10)], "manual_minutes": 8},
    "notify.email": {"label": "Send email", "category": "integration", "risk": "external", "fn": cap_email,
                     "description": "Outbound email with AI disclosure. Needs an approval upstream.",
                     "fields": [_f("to", "To", default="engineering@aeronexus.example"), _f("subject", "Subject"),
                                _f("body", "Body", "textarea")], "manual_minutes": 5},
    "db.save_case": {"label": "Save case", "category": "data", "risk": "internal", "fn": cap_save_case,
                     "description": "Write the run's results to the case store (append-only event log).",
                     "fields": [_f("kind", "Kind", default="workflow"), _f("title", "Title"),
                                _f("status", "Status", "select", "needs_review", ["verified", "needs_review", "blocked"])],
                     "manual_minutes": 4},
    "db.query_cases": {"label": "Query cases", "category": "data", "risk": "read", "fn": cap_query_cases,
                       "description": "Count or list cases by status.",
                       "fields": [_f("status", "Status", "select", "needs_review", ["", "verified", "needs_review", "blocked",
                                                                                     "approved", "executed"])], "manual_minutes": 2},
    "logic.condition": {"label": "Condition", "category": "logic", "risk": "none", "fn": cap_condition,
                        "description": "Branch on an expression, e.g. rul.low < 30 or drawing.severity == 'critical'.",
                        "fields": [_f("expression", "Expression", default="rul.work_order_needed == true")], "manual_minutes": 1},
    "transform.map": {"label": "Transform", "category": "logic", "risk": "none", "fn": cap_transform,
                      "description": "Reshape data: map output keys to {{templates}}.",
                      "fields": [_f("mapping", "Mapping (JSON)", "json", {"summary": "{{trigger.type}}"})], "manual_minutes": 1},
    "human.approval": {"label": "Human approval", "category": "human", "risk": "none", "fn": None,
                       "description": "Pauses the run until a named approver decides. One-time token, role check, optional "
                                      "separation of duties and timeout.",
                       "fields": [_f("title", "What is being approved", default="Approve the proposed actions"),
                                  _f("require_distinct_approver", "Approver must differ from the run's starter", "bool", False),
                                  _f("timeout_min", "Expire after (minutes)", "number", 1440)], "manual_minutes": 3},
    "debug.flaky": {"label": "Flaky service (test)", "category": "logic", "risk": "none", "fn": cap_flaky,
                    "description": "Always times out. Use it to see retries and error handling.", "fields": [], "manual_minutes": 0},
}


def catalog_public() -> list[dict[str, Any]]:
    return [{"type": k, **{kk: vv for kk, vv in v.items() if kk != "fn"}} for k, v in CATALOG.items()]
