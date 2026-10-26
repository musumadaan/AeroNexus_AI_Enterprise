"""Tamper-evident integration audit log + productivity analytics.

Every proposal, approval, rejection and executed tool call from the live agent runtime is appended to a JSONL file in
which each record carries the SHA-256 of the previous one (a hash chain). `verify()` recomputes the chain, so an edited
or deleted line is detected. Secrets never reach the log: keys that look like credentials are redacted.

Analytics are computed from the same records: actions automated, approvals requested / approved / rejected, approval
turnaround, calls per connector, and an estimate of hands-on minutes saved (assumed minutes per action type, labelled as
an assumption in the UI).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import threading
import time
from pathlib import Path
from typing import Any

from ..config import DATA

SECRET_KEYS = re.compile(r"(token|secret|password|passwd|api[_-]?key|private[_-]?key|authorization|credential)", re.I)
# assumed hands-on minutes a person would spend doing the same action by hand (shown as an assumption)
MANUAL_MINUTES = {"sap.create_purchase_requisition": 12, "sap.convert_to_purchase_order": 8, "cmms.create_maintenance_order": 8,
                  "erp.create_eco": 15, "comms.send_email": 5, "erp.create_quote": 10, "crm.create_opportunity": 6, "read": 3}


def _path() -> Path:
    return Path(os.environ["AERONEXUS_AUDIT_PATH"]) if os.environ.get("AERONEXUS_AUDIT_PATH") else DATA / "integrations" / "audit.jsonl"


def redact(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: ("[redacted]" if SECRET_KEYS.search(str(k)) else redact(v)) for k, v in x.items()}
    if isinstance(x, list):
        return [redact(v) for v in x]
    if isinstance(x, str) and re.search(r"(gsk_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|ya29\.[\w-]{20,})", x):
        return "[redacted]"
    return x


class AuditLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _path()
        self.lock = threading.Lock()
        self._last = self._tail_hash()

    def _tail_hash(self) -> str:
        if not self.path.exists():
            return "0" * 64
        last = "0" * 64
        with self.path.open() as f:
            for line in f:
                if line.strip():
                    try:
                        last = json.loads(line)["hash"]
                    except (ValueError, KeyError):
                        continue
        return last

    def append(self, event: str, actor: str, **detail: Any) -> dict[str, Any]:
        with self.lock:
            body = {"ts": round(time.time(), 3), "event": event, "actor": actor, "detail": redact(json.loads(json.dumps(detail, default=str)))}
            payload = json.dumps(body, sort_keys=True)
            h = hashlib.sha256((self._last + payload).encode()).hexdigest()
            rec = {**body, "prev": self._last, "hash": h}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            self._last = h
            return rec

    def records(self, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]
        return rows[-limit:] if limit else rows

    def verify(self) -> dict[str, Any]:
        prev = "0" * 64
        for i, r in enumerate(self.records()):
            body = {k: r[k] for k in ("ts", "event", "actor", "detail")}
            h = hashlib.sha256((prev + json.dumps(body, sort_keys=True)).encode()).hexdigest()
            if r.get("prev") != prev or r.get("hash") != h:
                return {"valid": False, "broken_at": i, "records": i}
            prev = h
        return {"valid": True, "records": len(self.records()), "head": prev[:16]}


_log: AuditLog | None = None


def audit_log() -> AuditLog:
    global _log
    if _log is None or _log.path != _path():
        _log = AuditLog()
    return _log


def analytics() -> dict[str, Any]:
    recs = audit_log().records()
    proposed: dict[str, float] = {}
    turnaround: list[float] = []
    by_tool: dict[str, dict[str, int]] = {}
    counts = {"proposed": 0, "approved": 0, "rejected": 0, "executed_reads": 0, "executed_writes": 0, "failed": 0}
    saved = 0.0
    by_day: dict[str, dict[str, int]] = {}
    for r in recs:
        d, ev = r["detail"], r["event"]
        tool = d.get("tool", "?")
        t = by_tool.setdefault(tool, {"calls": 0, "approved": 0, "rejected": 0})
        day = time.strftime("%Y-%m-%d", time.localtime(r["ts"]))
        bd = by_day.setdefault(day, {"automated": 0, "approvals": 0})
        if ev == "proposed":
            counts["proposed"] += 1
            proposed[d.get("approval_id", "")] = r["ts"]
        elif ev == "approved_and_executed":
            counts["approved"] += 1
            counts["executed_writes"] += d.get("status") == "executed"
            counts["failed"] += d.get("status") == "failed"
            t["approved"] += 1
            t["calls"] += 1
            bd["approvals"] += 1
            bd["automated"] += 1
            if d.get("approval_id") in proposed:
                turnaround.append(r["ts"] - proposed[d["approval_id"]])
            saved += MANUAL_MINUTES.get(tool, 5) - 1  # minus about a minute of review
        elif ev == "rejected":
            counts["rejected"] += 1
            t["rejected"] += 1
            bd["approvals"] += 1
            if d.get("approval_id") in proposed:
                turnaround.append(r["ts"] - proposed[d["approval_id"]])
        elif ev == "executed":
            counts["executed_reads"] += 1
            t["calls"] += 1
            bd["automated"] += 1
            saved += MANUAL_MINUTES["read"] * 0.5
    decided = counts["approved"] + counts["rejected"]
    return {
        "counts": counts, "pending": max(0, counts["proposed"] - decided),
        "approval_rate": round(counts["approved"] / decided, 4) if decided else None,
        "turnaround_s": {"median": round(statistics.median(turnaround), 1) if turnaround else None,
                         "max": round(max(turnaround), 1) if turnaround else None},
        "by_tool": [{"tool": k, **v} for k, v in sorted(by_tool.items(), key=lambda kv: -kv[1]["calls"]) if k != "?"],
        "by_day": [{"day": k, **v} for k, v in sorted(by_day.items())][-14:],
        "minutes_saved_est": round(saved, 1),
        "assumption": "Minutes saved use assumed hands-on minutes per action type, minus about one minute of human review.",
        "records": len(recs),
    }
