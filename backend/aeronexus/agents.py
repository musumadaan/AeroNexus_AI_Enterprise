"""Agent tools, ERP/CRM adapters, approval policy, human-in-the-loop queue and audit log.

Safety model (the part reviewers look for in an "agentic" demo):
  * Every tool declares a risk tier: READ < DRAFT < WRITE < EXTERNAL.
    READ and DRAFT run automatically. WRITE (posts to ERP/CRM) and EXTERNAL (emails, calls to
    customers) are *never* executed by the agent directly — they become approval requests in the
    HITL queue and run only after a named human approves.
  * Idempotency keys: the same action approved twice executes once (no duplicate quotes / POs).
  * Arguments are validated against the tool schema before anything runs.
  * Outbound calls/messages always carry an AI disclosure line and are logged.
  * Append-only audit log of who/what/when/why for every proposal, approval and execution.

Adapters: InMemoryERP / InMemoryCRM (default; seeded from data/) and ERPNextAdapter, a thin
REST client for a real ERPNext/Frappe instance (docker image is free & open source).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from typing import Any, Callable

import httpx

from .config import DATA
from .observability import tracer

AI_DISCLOSURE = "This message was prepared by an AI assistant on behalf of our estimating team and reviewed by a person."


class Risk(IntEnum):
    READ = 0
    DRAFT = 1
    WRITE = 2
    EXTERNAL = 3


# ---------------------------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------------------------
class InMemoryERP:
    def __init__(self, seed: dict[str, Any] | None = None) -> None:
        seed = seed or json.loads((DATA / "erp_seed.json").read_text())
        self.items: dict[str, dict[str, Any]] = {i["code"]: i for i in seed["items"]}
        self.orders: list[dict[str, Any]] = seed.get("open_orders", [])
        self.purchase_orders: dict[str, dict[str, Any]] = {p["po"]: p for p in seed.get("purchase_orders", [])}
        self.receipts: dict[str, dict[str, Any]] = {r["po"]: r for r in seed.get("goods_receipts", [])}
        self.quotes: dict[str, dict[str, Any]] = {}
        self.boms: dict[str, dict[str, Any]] = {}
        self.suppliers: dict[str, dict[str, Any]] = {x["id"]: x for x in seed.get("suppliers", [])}
        self.work_orders: list[dict[str, Any]] = seed.get("work_orders", [])
        self.material_pos: list[dict[str, Any]] = seed.get("material_pos", [])
        self.where_used_map: dict[str, list[str]] = seed.get("where_used", {})
        self.finished_goods: dict[str, int] = seed.get("finished_goods", {})
        self.ecos: dict[str, dict[str, Any]] = {}
        self.maintenance_orders: dict[str, dict[str, Any]] = {}

    # --- change-impact reads ------------------------------------------------------------------
    def where_used(self, part_number: str) -> list[str]:
        return self.where_used_map.get(part_number, [])

    def work_orders_for(self, part_number: str) -> list[dict[str, Any]]:
        return [w for w in self.work_orders if w["part_number"] == part_number]

    def material_pos_for(self, part_number: str) -> list[dict[str, Any]]:
        return [p for p in self.material_pos if p["for_part"] == part_number and p["status"] == "open"]

    def supplier_for(self, material: str) -> dict[str, Any] | None:
        return next((s for s in self.suppliers.values() if material.upper() in [m.upper() for m in s["materials"]]), None)

    def create_eco(self, case_id: str, part_number: str, summary: str, cost_impact: float, effective: str) -> dict[str, Any]:
        eco = {"eco_id": f"ECO-{len(self.ecos) + 4001}", "case_id": case_id, "part_number": part_number, "summary": summary,
               "cost_impact": cost_impact, "effective": effective, "status": "released"}
        self.ecos[eco["eco_id"]] = eco
        return eco

    def create_maintenance_order(self, asset: str, task: str, reason: str, due_within_cycles: float) -> dict[str, Any]:
        mo = {"mo_id": f"MWO-{len(self.maintenance_orders) + 9001}", "asset": asset, "task": task, "reason": reason,
              "due_within_cycles": due_within_cycles, "status": "open"}
        self.maintenance_orders[mo["mo_id"]] = mo
        return mo

    def find_item(self, material: str = "", thickness: str = "") -> list[dict[str, Any]]:
        m, t = " ".join(material.upper().split()), thickness.lower().replace(" ", "")
        out = []
        for it in self.items.values():
            im = it["material"].upper()
            mat = 3 if m and m == im else (1 if m and set(m.split()) - {"ASTM", "CSA"} <= set(im.split()) else 0)
            if not mat:
                continue  # never price against a different material grade
            thick = 2 if t and t == it.get("thickness", "").lower().replace(" ", "") else 0
            out.append({**it, "match_score": mat + thick})
        return sorted(out, key=lambda x: -x["match_score"])[:5]

    def open_orders_for(self, part_number: str) -> list[dict[str, Any]]:
        return [o for o in self.orders if o["part_number"].upper() == part_number.upper()]

    def create_bom(self, part_number: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
        bom = {"bom_id": f"BOM-{part_number}-{len(self.boms) + 1}", "part_number": part_number, "lines": lines, "status": "draft"}
        self.boms[bom["bom_id"]] = bom
        return bom

    def create_quote(self, customer: str, part_number: str, amount: float, lines: list[dict[str, Any]]) -> dict[str, Any]:
        q = {"quote_id": f"QT-{len(self.quotes) + 1001}", "customer": customer, "part_number": part_number,
             "amount": round(amount, 2), "lines": lines, "status": "submitted"}
        self.quotes[q["quote_id"]] = q
        return q


class ERPNextAdapter:  # pragma: no cover - needs a running ERPNext
    """Minimal Frappe REST client. `base_url` like http://localhost:8000, token "key:secret"."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base, self.h = base_url.rstrip("/"), {"Authorization": f"token {token}"}

    def find_item(self, material: str = "", thickness: str = "") -> list[dict[str, Any]]:
        r = httpx.get(f"{self.base}/api/resource/Item", headers=self.h,
                      params={"filters": json.dumps([["description", "like", f"%{material}%"]]), "fields": '["*"]'})
        r.raise_for_status()
        return r.json()["data"]

    def create_quote(self, customer: str, part_number: str, amount: float, lines: list[dict[str, Any]]) -> dict[str, Any]:
        body = {"party_name": customer, "items": [{"item_code": l["code"], "qty": l["qty"], "rate": l["rate"]} for l in lines]}
        r = httpx.post(f"{self.base}/api/resource/Quotation", headers=self.h, json=body)
        r.raise_for_status()
        return r.json()["data"]


class InMemoryCRM:
    def __init__(self, seed: dict[str, Any] | None = None) -> None:
        seed = seed or json.loads((DATA / "crm_seed.json").read_text())
        self.accounts: dict[str, dict[str, Any]] = {a["id"]: a for a in seed["accounts"]}
        self.opportunities: list[dict[str, Any]] = seed.get("opportunities", [])
        self.activities: list[dict[str, Any]] = []

    def account_for_part(self, part_number: str) -> list[dict[str, Any]]:
        return [a for a in self.accounts.values() if part_number.upper() in [p.upper() for p in a.get("parts", [])]]

    def log_activity(self, account_id: str, kind: str, body: str) -> dict[str, Any]:
        act = {"id": f"ACT-{len(self.activities) + 1}", "account_id": account_id, "kind": kind, "body": body, "ts": time.time()}
        self.activities.append(act)
        return act

    def create_opportunity(self, account_id: str, title: str, amount: float) -> dict[str, Any]:
        opp = {"id": f"OPP-{len(self.opportunities) + 1}", "account_id": account_id, "title": title, "amount": amount, "stage": "Quoted"}
        self.opportunities.append(opp)
        return opp


# ---------------------------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------------------------
@dataclass
class Tool:
    name: str
    risk: Risk
    schema: dict[str, str]
    fn: Callable[..., Any]
    description: str = ""


@dataclass
class ApprovalItem:
    id: str
    tool: str
    args: dict[str, Any]
    risk: str
    reason: str
    proposed_by: str
    created: float
    idempotency_key: str
    status: str = "pending"  # pending | approved | rejected | executed | failed
    decided_by: str | None = None
    result: Any = None
    module: str = ""
    case_id: str = ""


@dataclass
class AuditEvent:
    ts: float
    actor: str
    action: str
    detail: dict[str, Any] = field(default_factory=dict)


class ToolValidationError(ValueError):
    pass


class AgentRuntime:
    def __init__(self, erp: Any | None = None, crm: Any | None = None) -> None:
        self.erp = erp or InMemoryERP()
        self.crm = crm or InMemoryCRM()
        self.queue: dict[str, ApprovalItem] = {}
        self.audit: list[AuditEvent] = []
        self.outbox: list[dict[str, Any]] = []
        self._executed: dict[str, Any] = {}
        self._lock = threading.Lock()
        self.tools: dict[str, Tool] = {}
        self.listeners: list[Callable[[str, str, dict[str, Any]], None]] = []  # optional observers (integration layer)
        self._register_defaults()

    def _register_defaults(self) -> None:
        e, c = self.erp, self.crm
        self.register(Tool("erp.find_item", Risk.READ, {"material": "str", "thickness": "str?"}, e.find_item,
                           "Look up catalog items by material/thickness"))
        self.register(Tool("erp.open_orders_for", Risk.READ, {"part_number": "str"}, e.open_orders_for))
        self.register(Tool("crm.account_for_part", Risk.READ, {"part_number": "str"}, c.account_for_part))
        self.register(Tool("erp.where_used", Risk.READ, {"part_number": "str"}, e.where_used))
        self.register(Tool("erp.work_orders_for", Risk.READ, {"part_number": "str"}, e.work_orders_for))
        self.register(Tool("erp.material_pos_for", Risk.READ, {"part_number": "str"}, e.material_pos_for))
        self.register(Tool("erp.supplier_for", Risk.READ, {"material": "str"}, e.supplier_for))
        self.register(Tool("erp.create_eco", Risk.WRITE, {"case_id": "str", "part_number": "str", "summary": "str",
                                                          "cost_impact": "float", "effective": "str"}, e.create_eco))
        self.register(Tool("cmms.create_maintenance_order", Risk.WRITE, {"asset": "str", "task": "str", "reason": "str",
                                                                         "due_within_cycles": "float"}, e.create_maintenance_order,
                           "Maintenance work order in the CMMS (approval required)"))
        self.register(Tool("erp.create_bom", Risk.WRITE, {"part_number": "str", "lines": "list"}, e.create_bom))
        self.register(Tool("erp.create_quote", Risk.WRITE, {"customer": "str", "part_number": "str", "amount": "float", "lines": "list"},
                           e.create_quote))
        self.register(Tool("crm.create_opportunity", Risk.WRITE, {"account_id": "str", "title": "str", "amount": "float"},
                           c.create_opportunity))
        self.register(Tool("crm.log_activity", Risk.DRAFT, {"account_id": "str", "kind": "str", "body": "str"}, c.log_activity))
        self.register(Tool("comms.send_email", Risk.EXTERNAL, {"to": "str", "subject": "str", "body": "str"}, self._send_email))
        self.register(Tool("comms.place_call", Risk.EXTERNAL, {"to": "str", "script": "str"}, self._place_call,
                           "Outbound voice call via telephony provider (Twilio-compatible). Requires approval."))
        from .integrations import register_tools  # optional, feature-flagged connectors (no-op when disabled)

        register_tools(self)

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    # --- external comms (stubbed: write to outbox; wire Twilio/SMTP in prod) ---------------------
    def _send_email(self, to: str, subject: str, body: str) -> dict[str, Any]:
        msg = {"channel": "email", "to": to, "subject": subject, "body": f"{body}\n\n— {AI_DISCLOSURE}"}
        self.outbox.append(msg)
        return {"queued": True, **msg}

    def _place_call(self, to: str, script: str) -> dict[str, Any]:
        msg = {"channel": "voice", "to": to, "script": f"Hello, this is an automated assistant calling for our estimating team. {script}",
               "note": "Recipient can request a human at any time; call is recorded only with consent."}
        self.outbox.append(msg)
        return {"queued": True, **msg}

    # --- validation / policy -----------------------------------------------------------------
    def _validate(self, tool: Tool, args: dict[str, Any]) -> None:
        types = {"str": str, "float": (int, float), "list": list, "int": int, "dict": dict}
        for k, t in tool.schema.items():
            opt = t.endswith("?")
            if k not in args:
                if opt:
                    continue
                raise ToolValidationError(f"{tool.name}: missing arg '{k}'")
            if not isinstance(args[k], types[t.rstrip("?")]):
                raise ToolValidationError(f"{tool.name}: arg '{k}' must be {t}")
        extra = set(args) - set(tool.schema)
        if extra:
            raise ToolValidationError(f"{tool.name}: unexpected args {sorted(extra)}")

    def _log(self, actor: str, action: str, **detail: Any) -> None:
        self.audit.append(AuditEvent(time.time(), actor, action, detail))
        for fn in self.listeners:  # observers must never break a tool call
            try:
                fn(actor, action, detail)
            except Exception:  # noqa: BLE001
                pass

    def call(self, name: str, args: dict[str, Any], actor: str = "agent", reason: str = "", module: str = "",
             case_id: str = "") -> dict[str, Any]:
        """Agent entry point. Low-risk tools run; high-risk tools are queued for approval."""
        tool = self.tools.get(name)
        if tool is None:
            raise ToolValidationError(f"unknown tool {name}")
        self._validate(tool, args)
        if tool.risk <= Risk.DRAFT:
            with tracer.span("agent.tool", tool=name):
                result = tool.fn(**args)
            self._log(actor, "executed", tool=name, args=args)
            return {"status": "executed", "result": result}
        key = hashlib.sha256(f"{name}|{json.dumps(args, sort_keys=True, default=str)}".encode()).hexdigest()[:20]
        with self._lock:
            existing = next((q for q in self.queue.values() if q.idempotency_key == key and q.status in {"pending", "executed", "approved"}), None)
            if existing:
                return {"status": "duplicate", "approval_id": existing.id}
            item = ApprovalItem(uuid.uuid4().hex[:10], name, args, tool.risk.name, reason, actor, time.time(), key, module=module,
                                case_id=case_id)
            self.queue[item.id] = item
        self._log(actor, "proposed", tool=name, approval_id=item.id, reason=reason)
        tracer.incr("agent.approval_requested")
        return {"status": "pending_approval", "approval_id": item.id}

    def decide(self, approval_id: str, approver: str, approve: bool, edits: dict[str, Any] | None = None) -> ApprovalItem:
        item = self.queue[approval_id]
        if item.status != "pending":
            return item
        if not approver or approver == "agent":
            raise PermissionError("approvals must come from a named human")
        item.decided_by = approver
        if not approve:
            item.status = "rejected"
            self._log(approver, "rejected", approval_id=approval_id)
            return item
        args = {**item.args, **(edits or {})}
        tool = self.tools[item.tool]
        self._validate(tool, args)
        with self._lock:
            if item.idempotency_key in self._executed:
                item.status, item.result = "executed", self._executed[item.idempotency_key]
                return item
            try:
                item.result = tool.fn(**args)
                item.status = "executed"
                self._executed[item.idempotency_key] = item.result
            except Exception as exc:
                item.status, item.result = "failed", str(exc)
        self._log(approver, "approved_and_executed", approval_id=approval_id, tool=item.tool, status=item.status)
        return item

    def pending(self) -> list[ApprovalItem]:
        return sorted((q for q in self.queue.values() if q.status == "pending"), key=lambda q: q.created)

    def snapshot(self) -> dict[str, Any]:
        return {
            "queue": [asdict(q) for q in self.queue.values()],
            "audit": [asdict(a) for a in self.audit[-200:]],
            "outbox": self.outbox[-50:],
        }
