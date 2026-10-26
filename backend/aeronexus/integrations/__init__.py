"""Enterprise-integration layer (optional, feature-flagged, backward compatible).

Two hooks connect it to the existing app; nothing else in the core imports this package:
  * `register_tools(runtime)` is called at the end of AgentRuntime's default tool registration, so live and sandbox
    runtimes both get the mock SAP tools (sandboxes get their own SAP instance; tests never touch the live one).
  * `attach(runtime)` is called once for the live runtime in services.py. It subscribes to the runtime's audit events and
    fans them out to the tamper-evident audit log and Google Sheets tracking.
With AERONEXUS_FF_INTEGRATIONS=0 both hooks return immediately and the app behaves exactly as before.
"""
from __future__ import annotations

from typing import Any

from .flags import integrations_enabled, sap_enabled


def register_tools(runtime: Any) -> None:
    if sap_enabled():
        from .sap_mock import register

        register(runtime)
    if integrations_enabled():
        from .salesforce import register as register_salesforce  # off unless AERONEXUS_FF_SALESFORCE=mock|live

        register_salesforce(runtime)
    if integrations_enabled():
        from . import nodes  # noqa: F401  (adds workflow-builder steps + the procurement template)


def attach(runtime: Any) -> None:
    if not integrations_enabled() or getattr(runtime, "_integrations_attached", False):
        return
    from .audit import audit_log
    from .sheets import tracker

    def on_event(actor: str, action: str, detail: dict[str, Any]) -> None:
        if getattr(runtime, "tracking_paused", False):
            return
        if action == "executed" and not str(detail.get("tool", "")).startswith(("sap.", "sf.", "crm.log_activity")):
            return  # routine ERP/CRM reads stay in the runtime's own audit trail
        item = runtime.queue.get(detail.get("approval_id", "")) if detail.get("approval_id") else None
        tool = detail.get("tool") or (item.tool if item else "")
        enriched = {**detail, "tool": tool, "module": getattr(item, "module", "") or detail.get("module", ""),
                    "case_id": getattr(item, "case_id", "")}
        if action in ("proposed", "approved_and_executed", "rejected", "executed", "workflow_approval", "approval_note"):
            audit_log().append(action, actor, **enriched)
        if action in ("proposed", "approved_and_executed", "rejected"):
            args = item.args if item else {}
            amount = None
            if item and isinstance(item.result, dict):
                amount = item.result.get("NetAmount") or item.result.get("cost_impact")
            if tool.startswith("sap.create"):
                summary = f"{args.get('material', '')} x {args.get('quantity', '')}: {args.get('reason', '')}"
            else:
                summary = args.get("subject") or args.get("summary") or args.get("task") or (item.reason if item else "")
            ref = ""
            if item and isinstance(item.result, dict):
                ref = str(item.result.get("PurchaseRequisition") or item.result.get("PurchaseOrder") or item.result.get("eco_id")
                          or item.result.get("id") or "")
            tracker.track(action, tool=tool, approval_id=detail.get("approval_id", ""), actor=actor,
                          status=getattr(item, "status", ""), module=enriched["module"], reference=ref or enriched["case_id"],
                          summary=str(summary or ""), amount=amount)

    runtime.listeners.append(on_event)
    runtime._integrations_attached = True
