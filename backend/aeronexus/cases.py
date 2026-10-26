"""Case management with durable, replayable workflow state.

Every unit of work (a change order, a quote, a work-order proposal, an invoice exception) is a Case.

Statuses (explicit, never inferred from free text):
    verified      all checks passed deterministically; waiting for an approver
    needs_review  at least one finding is uncertain → a person must look at it
    blocked       a step failed or required inputs are missing; the case cannot proceed
    approved      a named approver signed off
    executed      approved actions were carried out in the downstream system
    rejected      the approver declined

State is an append-only event log in SQLite (stdlib, durable across restarts; swap the DSN for
Postgres in prod). `replay(case_id)` rebuilds the case purely from its events and must equal the
stored projection — that is the "workflow replay" guarantee and it is tested.

Workflow steps run through `run_steps`: each step is retried with backoff, its output is recorded
as an event, and a failure after retries moves the case to `blocked` with the error. Re-running a
blocked case resumes after the last successful step instead of starting over.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .observability import tracer

STATUSES = ("verified", "needs_review", "blocked", "approved", "executed", "rejected")
TRANSITIONS: dict[str | None, set[str]] = {
    None: {"verified", "needs_review", "blocked"},
    "needs_review": {"verified", "blocked", "rejected", "needs_review"},
    "verified": {"approved", "rejected", "needs_review"},
    "blocked": {"needs_review", "verified", "blocked"},
    "approved": {"executed", "blocked"},
    "executed": set(),
    "rejected": set(),
}


class TransitionError(ValueError):
    pass


@dataclass
class Case:
    id: str
    tenant: str
    kind: str
    title: str
    status: str | None = None
    assignee: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    steps_done: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    created: float = 0.0
    updated: float = 0.0


def _apply(case: Case | None, ev: dict[str, Any]) -> Case:
    """Pure reducer: (state, event) → state. Replay = fold this over the event log."""
    t, p = ev["type"], ev["payload"]
    if t == "created":
        case = Case(id=ev["case_id"], tenant=p["tenant"], kind=p["kind"], title=p["title"], data={}, created=ev["ts"])
    assert case is not None
    if t == "status":
        case.status = p["to"]
    elif t == "step_done":
        case.steps_done.append(p["step"])
        case.data[p["step"]] = p["output"]
    elif t == "assigned":
        case.assignee = p["to"]
    elif t == "data":
        case.data.update(p)
    case.history.append({"ts": ev["ts"], "type": t, "actor": ev["actor"], "summary": _summary(t, p)})
    case.updated = ev["ts"]
    return case


def _summary(t: str, p: dict[str, Any]) -> str:
    if t == "status":
        return f"{p.get('from') or 'new'} → {p['to']}" + (f": {p['reason']}" if p.get("reason") else "")
    if t == "step_done":
        return f"step {p['step']} completed"
    if t == "step_failed":
        return f"step {p['step']} failed (attempt {p['attempt']}): {p['error']}"
    if t == "assigned":
        return f"assigned to {p['to']}"
    if t == "created":
        return f"{p['kind']} case opened"
    return t


class CaseStore:
    def __init__(self, dsn: str = ":memory:") -> None:
        self.db = sqlite3.connect(dsn, check_same_thread=False)
        self.lock = threading.RLock()
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, tenant TEXT, "
                            "ts REAL, actor TEXT, type TEXT, payload TEXT)")
            self.db.execute("CREATE INDEX IF NOT EXISTS ev_case ON events(case_id)")
        self._cache: dict[str, Case] = {}

    # --- event log -----------------------------------------------------------------------------
    def _append(self, case_id: str, tenant: str, actor: str, typ: str, payload: dict[str, Any]) -> Case:
        ev = {"case_id": case_id, "ts": time.time(), "actor": actor, "type": typ, "payload": payload}
        with self.lock, self.db:
            self.db.execute("INSERT INTO events(case_id, tenant, ts, actor, type, payload) VALUES (?,?,?,?,?,?)",
                            (case_id, tenant, ev["ts"], actor, typ, json.dumps(payload, default=str)))
            self._cache[case_id] = _apply(self._cache.get(case_id), json.loads(json.dumps(ev, default=str)))
        return self._cache[case_id]

    def events(self, case_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT case_id, ts, actor, type, payload FROM events WHERE case_id=? ORDER BY seq", (case_id,))
        return [{"case_id": r[0], "ts": r[1], "actor": r[2], "type": r[3], "payload": json.loads(r[4])} for r in rows]

    def replay(self, case_id: str) -> Case:
        case = None
        for ev in self.events(case_id):
            case = _apply(case, ev)
        if case is None:
            raise KeyError(case_id)
        return case

    # --- API -----------------------------------------------------------------------------------
    def open(self, tenant: str, kind: str, title: str, actor: str) -> Case:
        cid = f"{kind[:3].upper()}-{uuid.uuid4().hex[:8]}"
        return self._append(cid, tenant, actor, "created", {"tenant": tenant, "kind": kind, "title": title})

    def get(self, case_id: str, tenant: str) -> Case:
        case = self._cache.get(case_id) or self.replay(case_id)
        if case.tenant != tenant:  # tenant isolation: another tenant's case does not exist for you
            raise KeyError(case_id)
        return case

    def list(self, tenant: str, status: str | None = None, kind: str | None = None) -> list[Case]:
        ids = [r[0] for r in self.db.execute("SELECT DISTINCT case_id FROM events WHERE tenant=?", (tenant,))]
        out = [self._cache.get(i) or self.replay(i) for i in ids]
        return sorted([c for c in out if (status is None or c.status == status) and (kind is None or c.kind == kind)],
                      key=lambda c: -c.updated)

    def set_status(self, case_id: str, tenant: str, to: str, actor: str, reason: str = "") -> Case:
        case = self.get(case_id, tenant)
        if to not in STATUSES or to not in TRANSITIONS[case.status]:
            raise TransitionError(f"{case.status} → {to} is not allowed")
        tracer.incr(f"case.status.{to}")
        return self._append(case_id, tenant, actor, "status", {"from": case.status, "to": to, "reason": reason})

    def assign(self, case_id: str, tenant: str, to: str, actor: str) -> Case:
        self.get(case_id, tenant)
        return self._append(case_id, tenant, actor, "assigned", {"to": to})

    def put(self, case_id: str, tenant: str, actor: str, **data: Any) -> Case:
        self.get(case_id, tenant)
        return self._append(case_id, tenant, actor, "data", data)

    def run_steps(self, case: Case, steps: list[tuple[str, Callable[[dict[str, Any]], Any]]], actor: str,
                  retries: int = 2, backoff_s: float = 0.05) -> Case:
        """Run workflow steps with retry; resumable. Each step gets the outputs of earlier steps."""
        for name, fn in steps:
            if name in case.steps_done:
                continue  # resume: already done
            for attempt in range(1, retries + 2):
                try:
                    with tracer.span(f"step.{name}"):
                        out = fn(case.data)
                    case = self._append(case.id, case.tenant, actor, "step_done", {"step": name, "output": out})
                    break
                except Exception as exc:  # recorded, retried, then blocked
                    self._append(case.id, case.tenant, actor, "step_failed", {"step": name, "attempt": attempt, "error": str(exc)})
                    if attempt > retries:
                        return self.set_status(case.id, case.tenant, "blocked", actor, f"{name} failed: {exc}")
                    time.sleep(backoff_s * attempt)
        return case


def to_dict(case: Case) -> dict[str, Any]:
    return json.loads(json.dumps(asdict(case), default=str))
