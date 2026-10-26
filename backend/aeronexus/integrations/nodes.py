"""Workflow-builder steps and a template for the integration layer (registered only when the layer is enabled).

  sap.suggest_parts          read      map the engine's worn modules to maintenance spares in the SAP material master
  sap.purchase_requisition   write     create an SAP purchase requisition: refused by the builder without an approval upstream
  sheets.track               internal  append a tracking row to Google Sheets (live runs only; test runs are simulated)
"""
from __future__ import annotations

from typing import Any

from ..orchestration.library import CATALOG, Ctx, _execute_with_approval, _f
from ..orchestration.workflows import TEMPLATES, _e, _n

# worn module → spare (rule, transparent and editable)
PARTS_RULE = {"High-pressure compressor": ("HPC-BLD-SET", 1), "High-pressure turbine": ("SEAL-KIT-HPT", 1),
              "Low-pressure turbine": ("T50-PROBE", 1), "Fan": ("FAN-BLD-01", 2)}


def cap_suggest_parts(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    src = state.get("results", {}).get(cfg.get("source") or "rul") or {}
    poor = src.get("poor_components") or []
    lines = [{"material": "BSK-ENG-KIT", "quantity": 1, "why": "borescope inspection consumables"}]
    for comp in poor:
        if comp in PARTS_RULE:
            m, q = PARTS_RULE[comp]
            lines.append({"material": m, "quantity": q, "why": f"{comp} condition is poor"})
    sap = getattr(ctx.runtime, "sap", None)
    for ln in lines:
        mat = sap.materials.get(ln["material"]) if sap else None
        ln["name"] = mat["MaterialName"] if mat else ln["material"]
        ln["est_cost_cad"] = round(ln["quantity"] * mat["Price"], 2) if mat else None
    ctx.emit("decision", None, f"Suggested {len(lines)} spare line(s) for {', '.join(poor) or 'a routine inspection'}")
    primary = max(lines, key=lambda x: x.get("est_cost_cad") or 0)
    return {"lines": lines, "primary": primary, "total_est_cad": round(sum(x.get("est_cost_cad") or 0 for x in lines), 2)}


def cap_purchase_requisition(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    args = {"material": str(cfg.get("material") or "BSK-ENG-KIT"), "quantity": float(cfg.get("quantity") or 1),
            "reason": str(cfg.get("reason") or "maintenance demand")[:300], "source": f"workflow {ctx.run.id}",
            "requested_by": ctx.user or "agent"}
    out = _execute_with_approval("sap.create_purchase_requisition", args, state, ctx, args["reason"])
    pr = out.get("result") or {}
    return {**out, "purchase_requisition": pr.get("PurchaseRequisition"), "net_amount": pr.get("NetAmount")}


def cap_sheets_track(cfg: dict[str, Any], state: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    from .sheets import tracker

    row = {"event": str(cfg.get("event") or "workflow_step"), "summary": str(cfg.get("summary") or ctx.run.name)[:300],
           "reference": str(cfg.get("reference") or ctx.run.id)}
    if ctx.mode != "live":
        ctx.emit("db_write", None, "Test run: tracking row simulated (not written)")
        return {"written": False, "simulated": True, **row}
    tracker.track(row["event"], tool="workflow", actor=ctx.user or "agent", status="logged", module="workflow",
                  reference=row["reference"], summary=row["summary"])
    ctx.emit("db_write", None, f"Tracking row queued for Google Sheets ({tracker.mode} mode)")
    return {"written": True, "mode": tracker.mode, **row}


if "sap.purchase_requisition" not in CATALOG:
    CATALOG["sap.suggest_parts"] = {
        "label": "SAP: suggest spares", "category": "integration", "risk": "read", "fn": cap_suggest_parts,
        "description": "Maps the engine's worn modules to spares in the SAP material master (mock).",
        "fields": [_f("source", "Remaining-life step", default="rul")], "manual_minutes": 10}
    CATALOG["sap.purchase_requisition"] = {
        "label": "SAP purchase requisition", "category": "integration", "risk": "write", "fn": cap_purchase_requisition,
        "description": "Creates a purchase requisition in SAP (mock). Needs an approval upstream.",
        "fields": [_f("material", "Material", default="{{parts.primary.material}}"),
                   _f("quantity", "Quantity", "number", "{{parts.primary.quantity}}"),
                   _f("reason", "Reason", default="Inspection for {{trigger.subset}}-ENG-{{trigger.unit}}: RUL {{rul.low}}–{{rul.high}}")],
        "manual_minutes": 12}
    CATALOG["sheets.track"] = {
        "label": "Google Sheets: log row", "category": "data", "risk": "internal", "fn": cap_sheets_track,
        "description": "Appends a tracking row to Google Sheets (local CSV when Sheets is not configured).",
        "fields": [_f("event", "Event", default="procurement"), _f("summary", "Summary", default="{{pr.purchase_requisition}}"),
                   _f("reference", "Reference", default="{{trigger.subset}}-ENG-{{trigger.unit}}")], "manual_minutes": 3}

if not any(t["id"] == "tpl-procure" for t in TEMPLATES):
    TEMPLATES.append({
        "id": "tpl-procure", "name": "Inspection → SAP purchase requisition → Sheets", "trigger": "sensor_anomaly",
        "description": "When an engine needs inspection, suggest spares, get a human approval, raise an SAP requisition and track it in Google Sheets.",
        "nodes": [_n("trigger", "trigger.sensor_anomaly", 0, 160), _n("rul", "model.rul", 230, 160),
                  _n("need", "logic.condition", 460, 160, "Inspection needed?", expression="rul.work_order_needed == true"),
                  _n("parts", "sap.suggest_parts", 690, 60),
                  _n("approve", "human.approval", 920, 60, "Procurement approval",
                     title="Raise SAP requisition for {{parts.primary.name}} (est. {{parts.total_est_cad}} CAD)?"),
                  _n("pr", "sap.purchase_requisition", 1150, 0), _n("log", "sheets.track", 1380, 0),
                  _n("monitor", "db.save_case", 690, 280, "Keep monitoring", kind="maintenance", status="verified",
                     title="Monitor {{trigger.subset}}-ENG-{{trigger.unit}}")],
        "edges": [_e("trigger", "rul"), _e("rul", "need"), _e("need", "parts", "true"), _e("need", "monitor", "false"),
                  _e("parts", "approve"), _e("approve", "pr"), _e("pr", "log")]})
