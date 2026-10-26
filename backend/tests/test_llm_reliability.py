import json

import pytest

from aeronexus.llm import CircuitBreaker, LLMError, LLMRequest, LLMRouter, MockLLM, Provider, parse_json_loose
from aeronexus.observability import tracer


class Flaky(Provider):
    def __init__(self, fail_times):
        self.fail_times, self.calls = fail_times, 0

    def complete(self, req, model):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError("upstream timeout")
        return json.dumps({"answer": "ok", "confidence": 0.9})


def router_with(provider):
    r = LLMRouter(provider="mock", small="small-3b", large="large-14b")
    r.provider = provider
    return r


def req(prompt="hi"):
    return LLMRequest(task="t", prompt=prompt, json_schema={"answer": "str", "confidence": "float"})


def test_retry_then_success(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    p = Flaky(fail_times=2)
    r = router_with(p)
    out = r.complete(req())
    assert out.parsed["answer"] == "ok" and p.calls == 3


def test_cache_hit_is_free():
    p = Flaky(fail_times=0)
    r = router_with(p)
    r.complete(req("same"))
    out = r.complete(req("same   "))  # whitespace-normalised key
    assert out.cached and p.calls == 1


def test_circuit_breaker_opens_and_fails_fast(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    p = Flaky(fail_times=10_000)
    r = router_with(p)
    r.breaker = CircuitBreaker(fail_threshold=3, reset_s=60)
    with pytest.raises(LLMError):
        r.complete(req("a"), cascade=False)
    calls = p.calls
    with pytest.raises(LLMError, match="circuit open"):
        r.complete(req("b"), cascade=False)
    assert p.calls == calls  # no network call while open


def test_schema_repair_round_trip():
    class Sloppy(Provider):
        n = 0

        def complete(self, req, model):
            Sloppy.n += 1
            return "Sure! here you go" if Sloppy.n == 1 else '```json\n{"answer": "fixed", "confidence": 0.9}\n```'

    r = router_with(Sloppy())
    out = r.complete(req(), cascade=False)
    assert out.parsed["answer"] == "fixed" and tracer.counters["llm.repair"] == 1


def test_cascade_escalates_on_low_confidence():
    @MockLLM.register("conf_test")
    def h(req, model):
        return json.dumps({"answer": model, "confidence": 0.4 if "3b" in model else 0.9})

    r = LLMRouter(provider="mock", small="small-3b", large="large-14b")
    out = r.complete(LLMRequest(task="conf_test", prompt="x", json_schema={"answer": "str", "confidence": "float"}))
    assert out.escalated and out.parsed["answer"] == "large-14b"


def test_parse_json_loose():
    assert parse_json_loose('noise {"a": 1} noise') == {"a": 1}
    assert parse_json_loose("not json") is None
