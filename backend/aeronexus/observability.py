"""Lightweight tracing + latency metrics.

Every pipeline stage runs inside `span(...)`. Spans are kept in a bounded in-memory ring buffer
(swap for OpenTelemetry in prod: the span API mirrors OTel's start/end/attributes shape).
`latency_report()` gives p50/p95/p99 per stage so the UI can show where time goes."""
from __future__ import annotations

import contextlib
import contextvars
import statistics
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Iterator

_current_trace: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)


@dataclass
class Span:
    trace_id: str
    name: str
    start: float
    end: float | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def ms(self) -> float:
        return ((self.end or time.perf_counter()) - self.start) * 1000


class Tracer:
    def __init__(self, max_spans: int = 20_000) -> None:
        self._spans: deque[Span] = deque(maxlen=max_spans)
        self._lock = threading.Lock()
        self.counters: dict[str, int] = defaultdict(int)

    @contextlib.contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Span]:
        trace_id = _current_trace.get()
        token = None
        if trace_id is None:
            trace_id = uuid.uuid4().hex[:16]
            token = _current_trace.set(trace_id)
        s = Span(trace_id=trace_id, name=name, start=time.perf_counter(), attrs=dict(attrs))
        try:
            yield s
        except Exception as exc:  # record and re-raise
            s.error = f"{type(exc).__name__}: {exc}"
            self.counters[f"{name}.error"] += 1
            raise
        finally:
            s.end = time.perf_counter()
            with self._lock:
                self._spans.append(s)
            if token is not None:
                _current_trace.reset(token)

    def incr(self, key: str, n: int = 1) -> None:
        self.counters[key] += n

    def spans(self, trace_id: str | None = None) -> list[Span]:
        with self._lock:
            data = list(self._spans)
        return [s for s in data if trace_id is None or s.trace_id == trace_id]

    def latency_report(self) -> dict[str, dict[str, float]]:
        by_name: dict[str, list[float]] = defaultdict(list)
        for s in self.spans():
            by_name[s.name].append(s.ms)
        out: dict[str, dict[str, float]] = {}
        for name, xs in sorted(by_name.items()):
            xs.sort()
            out[name] = {
                "count": float(len(xs)),
                "p50": _pct(xs, 50),
                "p95": _pct(xs, 95),
                "p99": _pct(xs, 99),
                "mean": statistics.fmean(xs),
            }
        return out

    def reset(self) -> None:
        with self._lock:
            self._spans.clear()
        self.counters.clear()


def _pct(sorted_xs: list[float], p: float) -> float:
    if not sorted_xs:
        return 0.0
    k = (len(sorted_xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (k - lo)


tracer = Tracer()
