"""Token and cost accounting for every LLM call.

Providers that return usage (Groq / OpenAI-compatible) report real token counts; the offline mock and
providers without usage are estimated at ~4 characters per token and flagged `estimated`.

Cost uses a per-model price table in USD per 1M tokens. Groq's free tier costs nothing; the table holds
Groq's published paid-tier list prices (checked October 2026) so the dashboard can show what the same
traffic would cost on a paid plan. Override with AERONEXUS_PRICES='{"model": [input, output]}'.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from typing import Any

# USD per 1M tokens (input, output). Source: Groq paid-tier list prices as published Oct 2026.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "llama-3.1-8b-instant": (0.05, 0.08),
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "openai/gpt-oss-20b": (0.075, 0.30),
}


def prices() -> dict[str, tuple[float, float]]:
    p = dict(DEFAULT_PRICES)
    raw = os.environ.get("AERONEXUS_PRICES")
    if raw:
        try:
            p.update({k: (float(v[0]), float(v[1])) for k, v in json.loads(raw).items()})
        except (ValueError, TypeError, IndexError):
            pass
    return p


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


@dataclass
class Call:
    ts: float
    model: str
    task: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    cached: bool
    estimated: bool
    cost_usd: float
    run_id: str | None = None


class UsageMeter:
    def __init__(self, keep: int = 5000) -> None:
        self._calls: deque[Call] = deque(maxlen=keep)
        self._lock = threading.Lock()
        self._local = threading.local()

    # the provider stashes real usage here for the call in flight (per thread)
    def stash(self, prompt_tokens: int, completion_tokens: int) -> None:
        self._local.usage = (prompt_tokens, completion_tokens)

    def take(self) -> tuple[int, int] | None:
        u = getattr(self._local, "usage", None)
        self._local.usage = None
        return u

    def record(self, model: str, task: str, prompt: str, completion: str, latency_ms: float, cached: bool = False,
               run_id: str | None = None) -> Call:
        real = self.take()
        pt, ct = real if real else (estimate_tokens(prompt), estimate_tokens(completion))
        pin, pout = prices().get(model, (0.0, 0.0))
        cost = 0.0 if cached else (pt * pin + ct * pout) / 1e6
        c = Call(time.time(), model, task, pt, ct, round(latency_ms, 2), cached, real is None, round(cost, 8), run_id)
        with self._lock:
            self._calls.append(c)
        return c

    def calls(self) -> list[Call]:
        with self._lock:
            return list(self._calls)

    def summary(self) -> dict[str, Any]:
        by: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for c in self.calls():
            m = by[c.model]
            m["calls"] += 1
            m["cached"] += c.cached
            m["prompt_tokens"] += c.prompt_tokens
            m["completion_tokens"] += c.completion_tokens
            m["cost_usd"] += c.cost_usd
            m["latency_ms_total"] += c.latency_ms
            m["estimated"] += c.estimated
        models = {k: {kk: round(vv, 6) for kk, vv in v.items()} for k, v in by.items()}
        tot = {k: round(sum(m[k] for m in models.values()), 6)
               for k in ("calls", "cached", "prompt_tokens", "completion_tokens", "cost_usd")}
        return {"models": models, "total": tot, "prices_usd_per_1m": {k: list(v) for k, v in prices().items()},
                "note": "Groq free tier: actual spend $0. Cost shows Groq paid-tier list prices for the same tokens.",
                "recent": [asdict(c) for c in self.calls()[-40:]][::-1]}

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()


meter = UsageMeter()
