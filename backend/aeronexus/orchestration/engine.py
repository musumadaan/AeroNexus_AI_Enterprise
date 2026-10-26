"""Compile workflow definitions into LangGraph StateGraphs and drive them.

A definition is `{nodes: [{id, type, config, label?, position?}], edges: [{source, target, branch?}]}`.

Compilation rules
  * START → the single trigger node; leaf nodes → END.
  * A node with several outgoing edges fans out in parallel; a node with several incoming edges is a
    deferred join (`defer=True`), so it runs once after every active branch has finished.
  * `logic.condition` routes by its boolean result along edges labelled `true` / `false`.
  * `human.approval` calls LangGraph `interrupt()`; the run pauses (checkpointed in memory) until a named
    approver resumes it with `Command(resume=...)`. Rejection ends the run there.
  * Each step runs under a LangGraph `RetryPolicy` for transient errors (timeouts, injected faults), with
    exponential backoff. After the last attempt a step either fails the run or, with `on_error: continue`,
    records the error and lets the rest of the workflow carry on.

Security rules enforced by `validate()` before anything runs: one trigger, no cycles, everything reachable,
condition branches labelled, and no write or external step reachable from the trigger without passing an
approval checkpoint.
"""
from __future__ import annotations

import operator
import threading
import time
from collections import defaultdict
from typing import Annotated, Any, Callable, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, RetryPolicy, interrupt

from ..observability import tracer
from ..security import Forbidden, Principal
from . import tracing
from .library import CATALOG, Ctx, render, scope_of
from .runs import RunRecord, TransientError, jsonable, store, summarize

MAX_NODES = 40


def _merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    return {**(a or {}), **(b or {})}


class FlowState(TypedDict, total=False):
    trigger: dict[str, Any]
    results: Annotated[dict[str, Any], _merge]
    errors: Annotated[list[dict[str, Any]], operator.add]
    approval: Annotated[dict[str, Any], _merge]


class NodeFailed(RuntimeError):
    pass


GRAPHS: dict[str, Any] = {}       # run id → compiled graph (kept for resume after an approval)
CONTEXTS: dict[str, Ctx] = {}
_lock = threading.Lock()


# ---------------------------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------------------------
def validate(defn: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    nodes = defn.get("nodes") or []
    edges = defn.get("edges") or []
    ids = [n.get("id") for n in nodes]
    if not nodes:
        return {"valid": False, "errors": ["Add a trigger and at least one step."], "warnings": []}
    if len(nodes) > MAX_NODES:
        errors.append(f"At most {MAX_NODES} steps per workflow.")
    if len(set(ids)) != len(ids):
        errors.append("Step ids must be unique.")
    for n in nodes:
        if not isinstance(n.get("id"), str) or not n["id"].replace("_", "").isalnum() or not n["id"][0].isalpha():
            errors.append(f"Step id '{n.get('id')}' must start with a letter and use letters, digits or _.")
        if n.get("type") not in CATALOG:
            errors.append(f"Unknown step type '{n.get('type')}'.")
    by_id = {n["id"]: n for n in nodes if isinstance(n.get("id"), str)}
    for e in edges:
        if e.get("source") not in by_id or e.get("target") not in by_id:
            errors.append(f"Connection {e.get('source')} → {e.get('target')} points to a missing step.")
    if errors:
        return {"valid": False, "errors": errors, "warnings": warnings}
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    inc: dict[str, int] = defaultdict(int)
    for e in edges:
        out[e["source"]].append(e)
        inc[e["target"]] += 1
    triggers = [n for n in nodes if CATALOG[n["type"]]["category"] == "trigger"]
    if len(triggers) != 1:
        errors.append("A workflow needs exactly one trigger.")
    else:
        t = triggers[0]["id"]
        if inc[t]:
            errors.append("Nothing can connect into the trigger.")
        # reachability + cycle check (DFS)
        seen: set[str] = set()
        state: dict[str, int] = {}

        def dfs(u: str) -> None:
            state[u] = 1
            seen.add(u)
            for e in out[u]:
                v = e["target"]
                if state.get(v) == 1:
                    errors.append(f"Loop detected at {u} → {v}; workflows must be acyclic.")
                elif v not in state:
                    dfs(v)
            state[u] = 2

        dfs(t)
        for n in nodes:
            if n["id"] not in seen:
                errors.append(f"'{n.get('label') or n['id']}' is not connected to the trigger.")
        # approval before any write/external step
        reach: set[str] = set()
        stack = [t]
        while stack:
            u = stack.pop()
            if u in reach:
                continue
            reach.add(u)
            if by_id[u]["type"] == "human.approval":
                continue
            stack.extend(e["target"] for e in out[u])
        for n in nodes:
            if CATALOG[n["type"]]["risk"] in ("write", "external") and n["id"] in reach:
                errors.append(f"'{n.get('label') or n['id']}' writes to another system but can run without an approval. "
                              f"Put a Human approval step before it.")
    for n in nodes:
        if n["type"] == "logic.condition":
            branches = {e.get("branch") for e in out[n["id"]]}
            if not out[n["id"]] or not branches <= {"true", "false"}:
                errors.append(f"Label each connection out of condition '{n.get('label') or n['id']}' as true or false.")
        if n["type"] == "human.approval" and not out[n["id"]]:
            warnings.append(f"Approval '{n.get('label') or n['id']}' has nothing after it.")
        if n["type"] in ("logic.condition",) and n.get("config", {}).get("expression"):
            try:
                import ast

                ast.parse(n["config"]["expression"], mode="eval")
            except SyntaxError:
                errors.append(f"Condition '{n.get('label') or n['id']}' has an invalid expression.")
    if not any(CATALOG[n["type"]]["category"] == "human" for n in nodes):
        warnings.append("No human approval step: this workflow can only read, analyse and save cases.")
    return {"valid": not errors, "errors": errors, "warnings": warnings}


# ---------------------------------------------------------------------------------------------
# instrumented node
# ---------------------------------------------------------------------------------------------
def instrument(run: RunRecord, ctx: Ctx, node_id: str, ntype: str, cfg: dict[str, Any], label: str,
               fn: Callable[[dict[str, Any], dict[str, Any], Ctx], dict[str, Any]], max_attempts: int) -> Callable:
    attempts = {"n": 0}
    injected = int((run.faults or {}).get(node_id, 0) or 0)
    permanent = node_id in ((run.faults or {}).get("_permanent") or [])
    on_error = cfg.get("on_error", "fail")

    def node(state: FlowState) -> dict[str, Any]:
        attempts["n"] += 1
        n = attempts["n"]
        t0 = time.perf_counter()
        store.node_update(run, node_id, status="running", attempts=n, started=time.time(), type=ntype, label=label)
        store.emit(run, "node_started", node_id, f"{label} started" + (f" (attempt {n})" if n > 1 else ""))
        scope = scope_of(state)
        rendered = render({k: v for k, v in cfg.items() if k not in ("on_error", "retries", "manual_minutes")}, scope)
        tracing.node_started(run.id, node_id, ntype, rendered)
        ctx.extra["node"] = node_id
        try:
            if n <= injected:
                raise TransientError(f"injected fault: simulated timeout calling {label}")
            if permanent:
                raise NodeFailed(f"injected permanent failure in {label}")
            with tracer.span(f"flow.{ntype}"):
                out = fn(rendered, state, _NodeCtx(ctx, node_id))
        except Exception as exc:  # noqa: BLE001 - classified below
            ms = (time.perf_counter() - t0) * 1000
            transient = isinstance(exc, TransientError)
            msg = f"{type(exc).__name__}: {exc}"
            if transient and n < max_attempts:
                store.node_update(run, node_id, status="retrying", last_error=msg, ms=round(ms, 1))
                store.emit(run, "retry", node_id, f"Attempt {n} failed ({exc}); retrying with backoff", level="warn")
                tracer.incr("flow.retry")
                tracing.node_finished(run.id, node_id, error=msg)
                raise
            store.node_update(run, node_id, status="failed", error=msg, ms=round(ms, 1), ended=time.time())
            store.emit(run, "node_failed", node_id, f"{label} failed after {n} attempt(s): {exc}", level="error")
            tracer.incr("flow.node_failed")
            tracing.node_finished(run.id, node_id, error=msg)
            if on_error == "continue":
                store.emit(run, "fallback", node_id, f"Continuing without {label} (on_error = continue)", level="warn")
                return {"results": {node_id: {"status": "failed", "error": msg}}, "errors": [{"node": node_id, "error": msg}]}
            raise NodeFailed(msg) from exc
        ms = (time.perf_counter() - t0) * 1000
        out = jsonable(out)  # plain JSON types only: checkpoints and replays must serialise
        store.node_update(run, node_id, status="completed", ms=round(ms, 1), ended=time.time(), output=summarize(out, 400))
        store.emit(run, "node_completed", node_id, f"{label} completed in {ms:.0f} ms", data=summarize(out))
        tracing.node_finished(run.id, node_id, output=summarize(out))
        with store.lock:
            run.outputs[node_id] = out
        return {"results": {node_id: out}}

    return node


class _NodeCtx:
    """Ctx proxy that stamps events with the current node id."""

    def __init__(self, ctx: Ctx, node: str) -> None:
        self._ctx, self._node = ctx, node

    def __getattr__(self, k: str) -> Any:
        return getattr(self._ctx, k)

    def emit(self, type_: str, node: str | None, message: str, level: str = "info", data: Any = None) -> None:
        store.emit(self._ctx.run, type_, node or self._node, message, level, data)


def approval_node(run: RunRecord, ctx: Ctx, node_id: str, cfg: dict[str, Any], label: str) -> Callable:
    def node(state: FlowState) -> dict[str, Any]:
        scope = scope_of(state)
        title = render(cfg.get("title") or "Approve the proposed actions", scope)
        src = state.get("results", {}).get(cfg.get("proposals_from") or "synthesizer") or {}
        proposals = cfg.get("proposals") or (src.get("proposals") if isinstance(src, dict) else None) or []
        first = node_id not in run.approvals or run.approvals[node_id]["status"] != "pending"
        a = store.issue_approval(run, node_id, {"title": title, "approver_role": "approver",
                                                "require_distinct_approver": bool(cfg.get("require_distinct_approver")),
                                                "timeout_min": float(cfg.get("timeout_min") or 1440), "proposals": proposals,
                                                "context": {k: summarize(v, 300) for k, v in scope.items() if k not in ("approval",)}})
        store.node_update(run, node_id, status="waiting", type="human.approval", label=label, started=time.time(), attempts=1)
        if first:
            store.emit(run, "approval_required", node_id, f"Waiting for a named approver: {title}", level="warn")
        if ctx.mode != "live":
            decision = {"approve": True, "by": "simulated-reviewer", "note": "test mode: simulated approval"}
            store.emit(run, "approval_simulated", node_id, "Test mode: a simulated reviewer approved (sandbox only)")
        else:
            run.status = "waiting_approval"
            decision = interrupt({"node": node_id, "title": title})
        ok = bool(decision.get("approve"))
        a.update({"status": "approved" if ok else "rejected", "decided_by": decision.get("by"), "note": decision.get("note", ""),
                  "decided": time.time()})
        a.pop("token", None)
        run.status = "running"
        store.node_update(run, node_id, status="completed" if ok else "rejected", ended=time.time(),
                          output={"approved": ok, "by": decision.get("by")})
        store.emit(run, "approved" if ok else "rejected", node_id,
                   f"{'Approved' if ok else 'Rejected'} by {decision.get('by')}" + (f": {decision.get('note')}" if decision.get("note") else ""),
                   level="info" if ok else "warn")
        out = {"approved": ok, "by": decision.get("by"), "note": decision.get("note", "")}
        run.outputs[node_id] = out
        return {"results": {node_id: out}, "approval": {"approved": ok, "by": decision.get("by"), "node": node_id}}

    return node


# ---------------------------------------------------------------------------------------------
# compile + drive
# ---------------------------------------------------------------------------------------------
def compile_workflow(defn: dict[str, Any], run: RunRecord, ctx: Ctx) -> Any:
    v = validate(defn)
    if not v["valid"]:
        raise ValueError("; ".join(v["errors"]))
    nodes = {n["id"]: n for n in defn["nodes"]}
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    inc: dict[str, int] = defaultdict(int)
    for e in defn.get("edges", []):
        out[e["source"]].append(e)
        inc[e["target"]] += 1
    g = StateGraph(FlowState)
    for nid, n in nodes.items():
        meta = CATALOG[n["type"]]
        cfg = {**{f["key"]: f["default"] for f in meta["fields"]}, **(n.get("config") or {})}
        label = n.get("label") or meta["label"]
        store.node_update(run, nid, status="pending", type=n["type"], label=label, attempts=0)
        if n["type"] == "human.approval":
            fn = approval_node(run, ctx, nid, cfg, label)
            g.add_node(nid, fn, defer=inc[nid] > 1)
            continue
        retries = int(cfg.get("retries", 2))
        fn = instrument(run, ctx, nid, n["type"], cfg, label, meta["fn"], max_attempts=retries + 1)
        g.add_node(nid, fn, defer=inc[nid] > 1,
                   retry_policy=RetryPolicy(max_attempts=retries + 1, initial_interval=0.1, backoff_factor=2.0, jitter=False,
                                            retry_on=TransientError))
    trig = next(n["id"] for n in defn["nodes"] if CATALOG[n["type"]]["category"] == "trigger")
    g.add_edge(START, trig)
    for nid, n in nodes.items():
        edges = out[nid]
        if not edges:
            g.add_edge(nid, END)
        elif n["type"] == "logic.condition":
            targets = {b: [e["target"] for e in edges if e.get("branch") == b] for b in ("true", "false")}

            def route(state: FlowState, _nid: str = nid, _t: dict[str, list[str]] = targets) -> list[str]:
                res = bool((state.get("results", {}).get(_nid) or {}).get("result"))
                return _t["true" if res else "false"] or [END]

            g.add_conditional_edges(nid, route, sorted({t for ts in targets.values() for t in ts}) + [END])
        elif n["type"] == "human.approval":
            tg = [e["target"] for e in edges]

            def aroute(state: FlowState, _nid: str = nid, _t: list[str] = tg) -> list[str]:
                return _t if (state.get("results", {}).get(_nid) or {}).get("approved") else [END]

            g.add_conditional_edges(nid, aroute, tg + [END])
        else:
            for e in edges:
                g.add_edge(nid, e["target"])
    return g.compile(checkpointer=InMemorySaver())


def drive(run: RunRecord, graph: Any, payload: Any) -> None:
    """Run (or resume) until completion, failure or the next approval checkpoint."""
    config = {"configurable": {"thread_id": run.id}, "recursion_limit": 120}
    try:
        for chunk in graph.stream(payload, config, stream_mode="updates"):
            if "__interrupt__" in chunk:
                run.status = "waiting_approval"
                store.emit(run, "paused", None, "Paused at an approval checkpoint", level="warn")
                store.save(run)
                return
        state = graph.get_state(config).values
        for nid, info in run.nodes.items():
            if info.get("status") == "pending":
                store.node_update(run, nid, status="skipped")
        rejected = any(a["status"] == "rejected" for a in run.approvals.values())
        _finalize_metrics(run, state)
        store.finish(run, "rejected" if rejected else "completed")
        tracing.run_finished(run.id, run.status, summarize(state.get("results", {})))
    except Exception as exc:  # noqa: BLE001
        cause = exc.__cause__ or exc
        for nid, info in run.nodes.items():
            if info.get("status") in ("pending", "running", "retrying"):
                store.node_update(run, nid, status="skipped" if info.get("status") == "pending" else "failed")
        _finalize_metrics(run, {})
        store.finish(run, "failed", f"{type(cause).__name__}: {cause}" if not isinstance(exc, NodeFailed) else str(exc))
        tracing.run_finished(run.id, "failed")
    finally:
        if run.status in ("completed", "failed", "rejected"):
            with _lock:
                GRAPHS.pop(run.id, None)
                CONTEXTS.pop(run.id, None)


def _finalize_metrics(run: RunRecord, state: dict[str, Any]) -> None:
    nodes = run.nodes.values()
    run.metrics = {
        "steps_total": len(run.nodes), "steps_completed": sum(n.get("status") == "completed" for n in nodes),
        "steps_failed": sum(n.get("status") == "failed" for n in nodes),
        "steps_skipped": sum(n.get("status") == "skipped" for n in nodes),
        "retries": sum(max(0, int(n.get("attempts") or 0) - 1) for n in nodes),
        "fallbacks": sum(e["type"] == "fallback" for e in run.events),
        "tool_calls": sum(e["type"] == "tool_call" for e in run.events),
        "approvals": len(run.approvals),
        "duration_ms": round(((run.ended or time.time()) - run.started) * 1000, 1),
        "errors": len(state.get("errors", []) or []),
    }


def start(run: RunRecord, graph: Any, ctx: Ctx, initial: dict[str, Any], background: bool = True) -> None:
    with _lock:
        GRAPHS[run.id] = graph
        CONTEXTS[run.id] = ctx
    tracing.run_started(run.id, run.name, run.inputs)
    if background:
        threading.Thread(target=drive, args=(run, graph, initial), daemon=True).start()
    else:
        drive(run, graph, initial)


def approval_token(run: RunRecord, node: str, who: Principal) -> str | None:
    a = run.approvals.get(node)
    if a and a["status"] == "pending" and who.can("approve"):
        return a.get("token")
    return None


def decide(run: RunRecord, node: str, who: Principal, approve: bool, note: str, token: str, background: bool = True) -> RunRecord:
    """Secure resume: role check, named human, one-time token, optional separation of duties, expiry."""
    who.require("approve")
    a = run.approvals.get(node)
    if a is None or a["status"] != "pending":
        raise ValueError("no pending approval at this step (it may already have been decided)")
    if not token or token != a.get("token"):
        raise Forbidden("invalid or already-used approval token")
    if a.get("require_distinct_approver") and who.user.lower() == (run.created_by or "").lower():
        raise Forbidden("separation of duties: the person who started this run cannot approve it")
    expired = time.time() - a["requested"] > float(a.get("timeout_min", 1440)) * 60
    with _lock:
        graph = GRAPHS.get(run.id)
    if graph is None:
        raise ValueError("this run is no longer resumable (the server restarted); start it again")
    a["token"] = None  # one-time
    if expired:
        approve, note = False, "approval window expired"
    from ..services import services

    services().runtime._log(who.user, "workflow_approval", run=run.id, node=node, approve=approve, note=note)
    cmd = Command(resume={"approve": approve, "by": who.user, "note": note[:500]})
    run.status = "running"
    if background:
        threading.Thread(target=drive, args=(run, graph, cmd), daemon=True).start()
    else:
        drive(run, graph, cmd)
    return run


def graph_shape(graph: Any) -> dict[str, Any]:
    g = graph.get_graph()
    return {"nodes": [{"id": n} for n in g.nodes], "edges": [{"source": e.source, "target": e.target, "conditional": e.conditional}
                                                             for e in g.edges]}
