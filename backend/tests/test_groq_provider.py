"""Groq provider tested offline with a fake HTTP layer (no key or network needed, no quota spent)."""
import json

import httpx
import pytest

import aeronexus.llm as llm
from aeronexus.llm import CircuitBreaker, GroqProvider, LLMError, LLMRequest, LLMRouter, Pacer


class Fake:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        status, body, hdrs = self.responses.pop(0)
        return httpx.Response(status, json=body, headers=hdrs, request=httpx.Request("POST", url))


def ok(content):
    return 200, {"choices": [{"message": {"content": content}}]}, {"x-ratelimit-remaining-tokens": "5000"}


@pytest.fixture
def groq(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_secret")
    monkeypatch.setattr("time.sleep", lambda s: None)
    GroqProvider.pacer = Pacer(0)  # no pacing in tests
    r = LLMRouter(provider="groq", small="llama-3.1-8b-instant", large="llama-3.3-70b-versatile")
    return r


def req():
    return LLMRequest(task="t", prompt="Return JSON", json_schema={"answer": "str", "confidence": "float"})


def test_calls_groq_openai_endpoint_with_json_mode(groq, monkeypatch):
    fake = Fake([ok(json.dumps({"answer": "ok", "confidence": 0.9}))])
    monkeypatch.setattr(llm.httpx, "post", fake)
    out = groq.complete(req())
    c = fake.calls[0]
    assert c["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert c["json"]["model"] == "llama-3.1-8b-instant" and c["json"]["response_format"] == {"type": "json_object"}
    assert c["headers"]["Authorization"] == "Bearer gsk_test_secret"
    assert out.parsed["answer"] == "ok" and GroqProvider.last_limits["x-ratelimit-remaining-tokens"] == "5000"


def test_low_confidence_8b_escalates_to_70b(groq, monkeypatch):
    fake = Fake([ok(json.dumps({"answer": "maybe", "confidence": 0.3})), ok(json.dumps({"answer": "sure", "confidence": 0.9}))])
    monkeypatch.setattr(llm.httpx, "post", fake)
    out = groq.complete(LLMRequest(task="t2", prompt="Return JSON", json_schema={"answer": "str", "confidence": "float"}))
    assert out.escalated and [c["json"]["model"] for c in fake.calls] == ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"]


def test_429_waits_retry_after_and_does_not_trip_breaker(groq, monkeypatch):
    slept = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    groq.breaker = CircuitBreaker(fail_threshold=1, reset_s=60)
    fake = Fake([(429, {"error": "rate"}, {"retry-after": "3"}), ok(json.dumps({"answer": "ok", "confidence": 0.95}))])
    monkeypatch.setattr(llm.httpx, "post", fake)
    out = groq.complete(LLMRequest(task="t3", prompt="Return JSON", json_schema={"answer": "str", "confidence": "float"}), cascade=False)
    assert out.parsed["answer"] == "ok" and 3.0 in slept and groq.breaker.state == "closed"


def test_long_rate_limit_degrades_instead_of_hanging(groq, monkeypatch):
    fake = Fake([(429, {"error": "rate"}, {"retry-after": "3600"})])
    monkeypatch.setattr(llm.httpx, "post", fake)
    with pytest.raises(LLMError):
        groq.complete(LLMRequest(task="t4", prompt="Return JSON", json_schema={"answer": "str", "confidence": "float"}), cascade=False)


def test_missing_key_is_a_clear_error_and_never_leaks(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GROQ_API_KEY not set"):
        GroqProvider().complete(LLMRequest(task="x", prompt="p"), "llama-3.1-8b-instant")


def test_groq_defaults_use_llama_and_a_different_family_judge(monkeypatch):
    from aeronexus.config import PROVIDER_DEFAULTS

    d = PROVIDER_DEFAULTS["groq"]
    assert d["small"].startswith("llama") and d["large"].startswith("llama") and not d["judge"].startswith("llama")


def test_pacer_spaces_requests():
    p = Pacer(rpm=600)  # 0.1 s apart
    p.wait()
    assert p.next_at - p.interval > 0
