"""Run records shared by mission control and the workflow builder.

Every execution (mission, workflow, test batch, replay) is a RunRecord: an append-only event list that the
UI streams over a WebSocket, per-node state (status, attempts, timing, output), approval checkpoints and
final metrics. Finished runs are written to data/runs/<id>.json so they can be replayed and compared.
"""
from __future__ import annotations

import os
from pathlib import Path

import json
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import DATA

RUN_DIR = Path(os.environ["AERONEXUS_RUNS_DIR"]) if os.environ.get("AERONEXUS_RUNS_DIR") else DATA / "runs"
MAX_EVENTS = 4000


class TransientError(RuntimeError):
    """A failure worth retrying (timeouts, 5xx, injected faults). Anything else fails fast."""


def _default(o: Any) -> Any:
    if hasattr(o, "item") and callable(o.item):  # numpy scalars
        try:
            return o.item()
        except (ValueError, TypeError):
            pass
    if hasattr(o, "tolist"):
        return o.tolist()
    return str(o)


def jsonable(x: Any) -> Any:
    return json.loads(json.dumps(x, default=_default))


def summarize(x: Any, limit: int = 600) -> Any:
    """Small preview of a node output for the timeline (full output stays in `outputs`)."""
    s = json.dumps(x, default=_default)
    if len(s) <= limit:
        return json.loads(s)
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            vs = json.dumps(v, default=_default)
            out[k] = json.loads(vs) if len(vs) < 160 else (f"[{len(v)} items]" if isinstance(v, list) else vs[:140] + "…")
            if len(json.dumps(out, default=_default)) > limit:
                break
        return out
    return s[:limit] + "…"


@dataclass
class RunRecord:
    id: str
    kind: str                      # mission | workflow | test_batch
    name: str
    mode: str                      # live | test | replay
    created_by: str
    tenant: str
    inputs: dict[str, Any]
    definition: dict[str, Any] | None = None
    status: str = "running"        # running | waiting_approval | completed | failed | rejected
    started: float = field(default_factory=time.time)
    ended: float | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    approvals: dict[str, dict[str, Any]] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    replay_of: str | None = None
    faults: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def public(self, full: bool = True) -> dict[str, Any]:
        d = asdict(self)
        for a in d["approvals"].values():  # the nonce is handed out once, through `approval_token`
            a.pop("token", None)
        if not full:
            d.pop("events")
            d.pop("outputs")
            d.pop("definition")
        d["duration_ms"] = round(((self.ended or time.time()) - self.started) * 1000, 1)
        return jsonable(d)


class RunStore:
    def __init__(self) -> None:
        self.runs: dict[str, RunRecord] = {}
        self.lock = threading.RLock()
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if RUN_DIR.exists():
            for p in sorted(RUN_DIR.glob("*.json"))[-300:]:
                try:
                    d = json.loads(p.read_text())
                    self.runs.setdefault(d["id"], RunRecord(**{k: v for k, v in d.items() if k in RunRecord.__dataclass_fields__}))
                except (ValueError, TypeError, KeyError):
                    continue

    def new(self, kind: str, name: str, mode: str, created_by: str, tenant: str, inputs: dict[str, Any],
            definition: dict[str, Any] | None = None, faults: dict[str, Any] | None = None, replay_of: str | None = None) -> RunRecord:
        with self.lock:
            self._load()
            r = RunRecord(id=f"{kind[:2]}-{uuid.uuid4().hex[:10]}", kind=kind, name=name, mode=mode, created_by=created_by or "anonymous",
                          tenant=tenant, inputs=jsonable(inputs), definition=jsonable(definition) if definition else None,
                          faults=faults or {}, replay_of=replay_of)
            self.runs[r.id] = r
        self.emit(r, "run_started", message=f"{kind} '{name}' started ({mode})", data={"inputs": summarize(inputs)})
        return r

    def get(self, run_id: str, tenant: str | None = None) -> RunRecord:
        with self.lock:
            self._load()
            r = self.runs.get(run_id)
        if r is None or (tenant is not None and r.tenant != tenant):
            raise KeyError(run_id)
        return r

    def list(self, kind: str | None = None, tenant: str | None = None, limit: int = 50) -> list[RunRecord]:
        with self.lock:
            self._load()
            rs = [r for r in self.runs.values() if (kind is None or r.kind == kind) and (tenant is None or r.tenant == tenant)]
        return sorted(rs, key=lambda r: -r.started)[:limit]

    def emit(self, run: RunRecord, type_: str, node: str | None = None, message: str = "", level: str = "info",
             data: Any = None) -> dict[str, Any]:
        ev = {"seq": len(run.events), "ts": time.time(), "t_ms": round((time.time() - run.started) * 1000, 1), "type": type_,
              "node": node, "message": message, "level": level, "data": jsonable(data) if data is not None else None}
        with self.lock:
            if len(run.events) < MAX_EVENTS:
                run.events.append(ev)
        return ev

    def node_update(self, run: RunRecord, node: str, **kw: Any) -> None:
        with self.lock:
            run.nodes.setdefault(node, {"status": "pending", "attempts": 0})
            run.nodes[node].update(jsonable(kw))

    def issue_approval(self, run: RunRecord, node: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            a = run.approvals.get(node)
            if a and a["status"] == "pending":
                return a
            a = {"node": node, "status": "pending", "requested": time.time(), "token": secrets.token_urlsafe(16), **jsonable(payload)}
            run.approvals[node] = a
            return a

    def finish(self, run: RunRecord, status: str, error: str | None = None) -> None:
        run.status, run.ended, run.error = status, time.time(), error
        self.emit(run, "run_finished", message=f"Run {status}" + (f": {error}" if error else ""),
                  level="error" if status == "failed" else "info", data={"status": status})
        self.save(run)

    def save(self, run: RunRecord) -> None:
        try:
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            d = asdict(run)
            (RUN_DIR / f"{run.id}.json").write_text(json.dumps(d, default=_default))
        except OSError:
            pass


store = RunStore()
