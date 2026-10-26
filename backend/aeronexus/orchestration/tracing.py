"""Optional export of agent and workflow traces to Langfuse or LangSmith.

* Langfuse: set LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY (and LANGFUSE_HOST for self-hosted; the open-source
  server runs locally with docker compose). Each run becomes a trace; each node a span with input, output,
  level and status; LLM calls become generations with token usage.
* LangSmith: set LANGSMITH_API_KEY and LANGSMITH_TRACING=true. LangGraph traces itself to LangSmith; nothing
  else is needed here.

With neither configured, traces stay local (the run timeline, the latency report and the usage meter), and
the observability page says so. Export failures never break a run.
"""
from __future__ import annotations

import os
from typing import Any

_client: Any = None
_spans: dict[tuple[str, str | None], Any] = {}


def status() -> dict[str, Any]:
    lf = bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))
    ls = bool(os.environ.get("LANGSMITH_API_KEY") and os.environ.get("LANGSMITH_TRACING", os.environ.get("LANGCHAIN_TRACING_V2", "")).lower() == "true")
    return {"langfuse": {"enabled": lf, "host": os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com") if lf else None},
            "langsmith": {"enabled": ls, "project": os.environ.get("LANGSMITH_PROJECT", "default") if ls else None},
            "local": True}


def _lf() -> Any:
    global _client
    if _client is None and status()["langfuse"]["enabled"]:
        try:
            from langfuse import Langfuse

            _client = Langfuse()
        except Exception:  # pragma: no cover - export is best effort
            _client = False
    return _client or None


def run_started(run_id: str, name: str, inputs: Any) -> None:
    c = _lf()
    if not c:
        return
    try:
        _spans[(run_id, None)] = c.start_observation(name=name, as_type="agent", input=inputs, metadata={"run_id": run_id})
    except Exception:  # pragma: no cover
        pass


def node_started(run_id: str, node: str, kind: str, inputs: Any) -> None:
    root = _spans.get((run_id, None))
    if root is None:
        return
    try:
        as_type = "tool" if kind.startswith(("api.", "notify.", "db.")) else "retriever" if kind.startswith("rag.") else \
            "guardrail" if kind.startswith("human.") else "agent" if kind.startswith("agent") else "span"
        _spans[(run_id, node)] = root.start_observation(name=node, as_type=as_type, input=inputs)
    except Exception:  # pragma: no cover
        pass


def node_finished(run_id: str, node: str, output: Any = None, error: str | None = None) -> None:
    s = _spans.pop((run_id, node), None)
    if s is None:
        return
    try:
        s.update(output=output, level="ERROR" if error else "DEFAULT", status_message=error)
        s.end()
    except Exception:  # pragma: no cover
        pass


def run_finished(run_id: str, status: str, output: Any = None) -> None:
    s = _spans.pop((run_id, None), None)
    if s is None:
        return
    try:
        s.update(output=output, level="ERROR" if status == "failed" else "DEFAULT", status_message=status)
        s.end()
        _lf().flush()
    except Exception:  # pragma: no cover
        pass
