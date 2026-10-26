
"""LLM abstraction with production reliability patterns.

Patterns implemented:
  * Provider-agnostic client (mock | ollama | anthropic | openai-compatible/vLLM).
  * Model cascade: try the small model first; escalate to the large model when
    confidence or schema validation requires it.
  * Response cache keyed on model and normalized prompt, with TTL.
  * Retries with exponential backoff and jitter, per-call timeout, and circuit breaker.
  * Structured output validation with one repair round-trip.
  * Optional Langfuse tracing that records metadata without prompts or completions.

The MockLLM is deterministic. Modules register task handlers on it so the whole
system runs offline in CI and the demo.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import threading
import time
from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .config import settings
from .observability import tracer


@dataclass
class LLMRequest:
    task: str
    prompt: str
    system: str = "You are a precise industrial-document assistant. Answer only from the provided context."
    payload: dict[str, Any] = field(default_factory=dict)
    json_schema: dict[str, str] | None = None
    max_tokens: int = 800
    temperature: float = 0.0


@dataclass
class LLMResponse:
    text: str
    model: str
    latency_ms: float
    cached: bool = False
    parsed: dict[str, Any] | None = None
    confidence: float | None = None
    escalated: bool = False


class LLMError(RuntimeError):
    pass


# ---------------------------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------------------------
class Provider:
    name = "base"

    def complete(self, req: LLMRequest, model: str) -> str:
        raise NotImplementedError


MockHandler = Callable[[LLMRequest, str], str]


class MockLLM(Provider):
    """Deterministic stand-in. The model is passed so tests can simulate a weak small model."""

    name = "mock"
    _handlers: dict[str, MockHandler] = {}

    @classmethod
    def register(cls, task: str) -> Callable[[MockHandler], MockHandler]:
        def deco(fn: MockHandler) -> MockHandler:
            cls._handlers[task] = fn
            return fn
        return deco

    def complete(self, req: LLMRequest, model: str) -> str:
        handler = self._handlers.get(req.task)
        if handler is None:
            return json.dumps({
                "answer": "I don't have enough grounded information to answer.",
                "confidence": 0.3,
            })
        return handler(req, model)


class OllamaProvider(Provider):
    name = "ollama"

    def complete(self, req: LLMRequest, model: str) -> str:
        body = {
            "model": model,
            "system": req.system,
            "prompt": req.prompt,
            "stream": False,
            "options": {
                "temperature": req.temperature,
                "num_predict": req.max_tokens,
            },
        }
        if req.json_schema:
            body["format"] = "json"
        response = httpx.post(
            f"{settings.ollama_url}/api/generate",
            json=body,
            timeout=settings.request_timeout_s,
        )
        response.raise_for_status()
        return response.json()["response"]


class AnthropicProvider(Provider):
    name = "anthropic"

    def complete(self, req: LLMRequest, model: str) -> str:
        import os

        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError("ANTHROPIC_API_KEY not set")

        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": req.max_tokens,
                "temperature": req.temperature,
                "system": req.system,
                "messages": [{"role": "user", "content": req.prompt}],
            },
            timeout=settings.request_timeout_s,
        )
        response.raise_for_status()
        return "".join(
            block.get("text", "")
            for block in response.json()["content"]
        )


class OpenAICompatProvider(Provider):
    """Works with OpenAI and OpenAI-compatible servers such as vLLM and LM Studio."""

    name = "openai"

    def complete(self, req: LLMRequest, model: str) -> str:
        import os

        headers = {
            "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', 'not-needed')}"
        }
        body: dict[str, Any] = {
            "model": model,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.prompt},
            ],
        }
        if req.json_schema:
            body["response_format"] = {"type": "json_object"}

        response = httpx.post(
            f"{settings.openai_base_url}/chat/completions",
            headers=headers,
            json=body,
            timeout=settings.request_timeout_s,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


class RateLimited(LLMError):
    """A 429 is not a provider outage and should not trip the circuit breaker."""

    def __init__(self, retry_after: float, detail: str = "") -> None:
        super().__init__(
            f"rate limited, retry after {retry_after:.1f}s {detail}".strip()
        )
        self.retry_after = retry_after


class Pacer:
    """Client-side request pacing to reduce free-tier rate-limit errors."""

    def __init__(self, rpm: float) -> None:
        self.interval = 60.0 / rpm if rpm > 0 else 0.0
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self) -> float:
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval
        if delay:
            time.sleep(delay)
        return delay


class GroqProvider(Provider):
    """Groq OpenAI-compatible endpoint with pacing and rate-limit tracking."""

    name = "groq"
    pacer: Pacer | None = None
    last_limits: dict[str, str] = {}

    def __init__(self) -> None:
        if GroqProvider.pacer is None:
            GroqProvider.pacer = Pacer(settings.groq_rpm)

    def complete(self, req: LLMRequest, model: str) -> str:
        import os

        key = os.environ.get("GROQ_API_KEY")
        if not key:
            raise LLMError(
                "GROQ_API_KEY not set — create a key at console.groq.com "
                "and put it in backend/.env"
            )

        body: dict[str, Any] = {
            "model": model,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.prompt},
            ],
        }
        if req.json_schema:
            body["response_format"] = {"type": "json_object"}

        waited = self.pacer.wait() if self.pacer else 0.0
        if waited:
            tracer.incr("llm.groq.paced")

        response = httpx.post(
            f"{settings.groq_base_url}/chat/completions",
            json=body,
            timeout=settings.request_timeout_s,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        GroqProvider.last_limits = {
            key: value
            for key, value in response.headers.items()
            if key.lower().startswith("x-ratelimit")
        }

        if response.status_code == 429:
            tracer.incr("llm.groq.rate_limited")
            try:
                retry_after = float(response.headers.get("retry-after", "2"))
            except ValueError:
                retry_after = 2.0
            raise RateLimited(
                retry_after,
                response.headers.get("x-ratelimit-reset-tokens", ""),
            )

        response.raise_for_status()
        data = response.json()
        usage = data.get("usage") or {}

        if usage.get("prompt_tokens") is not None:
            from .usage import meter
            meter.stash(
                int(usage.get("prompt_tokens", 0)),
                int(usage.get("completion_tokens", 0)),
            )

        return data["choices"][0]["message"].get("content") or ""


PROVIDERS: dict[str, type[Provider]] = {
    "mock": MockLLM,
    "groq": GroqProvider,
    "ollama": OllamaProvider,
    "anthropic": AnthropicProvider,
    "openai": OpenAICompatProvider,
}


# ---------------------------------------------------------------------------------------------
# Reliability primitives
# ---------------------------------------------------------------------------------------------
class TTLCache:
    def __init__(self, maxsize: int = 4096, ttl_s: float = 3600) -> None:
        self.maxsize, self.ttl = maxsize, ttl_s
        self._d: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    @staticmethod
    def key(model: str, req: LLMRequest) -> str:
        normalized = re.sub(
            r"\s+", " ", (req.system + "\n" + req.prompt).strip().lower()
        )
        return hashlib.sha256(
            f"{model}|{req.task}|{normalized}".encode()
        ).hexdigest()

    def get(self, key: str) -> str | None:
        with self._lock:
            item = self._d.get(key)
            if item and time.time() - item[0] < self.ttl:
                self._d.move_to_end(key)
                self.hits += 1
                return item[1]
            self.misses += 1
            return None

    def put(self, key: str, value: str) -> None:
        with self._lock:
            self._d[key] = (time.time(), value)
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)


class CircuitBreaker:
    """closed → open after repeated failures → half-open after reset."""

    def __init__(self, fail_threshold: int, reset_s: float) -> None:
        self.fail_threshold, self.reset_s = fail_threshold, reset_s
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if time.time() - self.opened_at >= self.reset_s:
            return "half-open"
        return "open"

    def allow(self) -> bool:
        return self.state != "open"

    def success(self) -> None:
        self.failures, self.opened_at = 0, None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.fail_threshold:
            self.opened_at = time.time()


def parse_json_loose(text: str) -> dict[str, Any] | None:
    """Accept JSON directly or inside prose/code fences."""
    text = text.strip()
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            result = json.loads(match.group(0))
            return result if isinstance(result, dict) else None
        except json.JSONDecodeError:
            return None
    return None


_TYPES = {
    "str": str,
    "float": (int, float),
    "int": int,
    "bool": bool,
    "list": list,
    "dict": dict,
}


def validate_schema(
    obj: dict[str, Any], schema: dict[str, str]
) -> list[str]:
    errors = []
    for key, expected_type in schema.items():
        optional = expected_type.endswith("?")
        expected_type = expected_type.rstrip("?")

        if key not in obj or obj[key] is None:
            if not optional:
                errors.append(f"missing '{key}'")
            continue

        if not isinstance(obj[key], _TYPES[expected_type]):
            errors.append(f"'{key}' should be {expected_type}")

    return errors


# ---------------------------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------------------------
class LLMRouter:
    def __init__(
        self,
        provider: str | None = None,
        small: str | None = None,
        large: str | None = None,
    ) -> None:
        self.provider_name = provider or settings.llm_provider

        if self.provider_name not in PROVIDERS:
            from .config import PROVIDER_DEFAULTS
            from .integrations.providers import register
            register(PROVIDERS, PROVIDER_DEFAULTS)

        self.provider = PROVIDERS[self.provider_name]()
        self.small = small or settings.small_model
        self.large = large or settings.large_model
        self.cache = TTLCache(ttl_s=settings.cache_ttl_s)
        self.breaker = CircuitBreaker(
            settings.circuit_fail_threshold,
            settings.circuit_reset_s,
        )

    # single model call with cache + retries + breaker + optional Langfuse
    def _call(self, req: LLMRequest, model: str) -> LLMResponse:
        key = self.cache.key(model, req)
        hit = self.cache.get(key)
        from .usage import meter

        if hit is not None:
            tracer.incr("llm.cache_hit")
            meter.record(model, req.task, req.prompt, hit, 0.0, cached=True)
            return LLMResponse(
                text=hit, model=model, latency_ms=0.0, cached=True
            )

        if not self.breaker.allow():
            tracer.incr("llm.circuit_open")
            raise LLMError(
                "circuit open: provider unhealthy, degrading to rules + human review"
            )

        # Import failure must never prevent normal LLM operation.
        try:
            from .integrations.langfuse_tracing import generation
        except Exception:
            generation = None

        last: Exception | None = None

        for attempt in range(settings.max_retries + 1):
            started = time.perf_counter()

            try:
                with tracer.span(
                    "llm.call",
                    model=model,
                    task=req.task,
                    attempt=attempt,
                ):
                    trace_context = (
                        generation(
                            name="llm.call",
                            model=model,
                            provider=self.provider_name,
                            task=req.task,
                            attempt=attempt,
                        )
                        if generation is not None
                        else nullcontext(None)
                    )

                    with trace_context as observation:
                        text = self.provider.complete(req, model)

                        if observation is not None:
                            try:
                                observation.update(
                                    output={"status": "success"},
                                    metadata={
                                        "provider": self.provider_name,
                                        "task": req.task,
                                        "attempt": attempt,
                                    },
                                )
                            except Exception:
                                pass

                self.breaker.success()
                self.cache.put(key, text)
                latency_ms = (time.perf_counter() - started) * 1000
                meter.record(
                    model, req.task, req.system + req.prompt, text, latency_ms
                )
                return LLMResponse(
                    text=text, model=model, latency_ms=latency_ms
                )

            except RateLimited as exc:
                last = exc
                if exc.retry_after > settings.max_rate_limit_wait_s:
                    break
                time.sleep(exc.retry_after)
                continue

            except Exception as exc:
                last = exc
                self.breaker.failure()
                tracer.incr("llm.retry")
                if attempt < settings.max_retries:
                    time.sleep(
                        min(2.0, 0.2 * 2**attempt)
                        * (0.5 + random.random())
                    )

        raise LLMError(f"LLM call failed after retries: {last}")

    def _structured(self, req: LLMRequest, model: str) -> LLMResponse:
        response = self._call(req, model)
        if not req.json_schema:
            return response

        parsed = parse_json_loose(response.text)
        errors = (
            validate_schema(parsed, req.json_schema)
            if parsed is not None
            else ["not JSON"]
        )

        if errors:
            tracer.incr("llm.repair")
            repair = LLMRequest(
                task=req.task,
                payload=req.payload,
                json_schema=req.json_schema,
                system=req.system,
                prompt=(
                    f"{req.prompt}\n\nYour previous output was invalid "
                    f"({'; '.join(errors)}). Return ONLY a JSON object "
                    f"with keys {list(req.json_schema)}."
                ),
            )
            response = self._call(repair, model)
            parsed = parse_json_loose(response.text)
            errors = (
                validate_schema(parsed, req.json_schema)
                if parsed is not None
                else ["not JSON"]
            )
            if errors:
                raise LLMError(f"schema validation failed: {errors}")

        response.parsed = parsed
        confidence = parsed.get("confidence") if parsed else None
        response.confidence = (
            float(confidence)
            if isinstance(confidence, (int, float))
            else None
        )
        return response

    def complete(
        self, req: LLMRequest, cascade: bool = True
    ) -> LLMResponse:
        """Try the small model first; escalate when invalid or under-confident."""
        with tracer.span("llm.complete", task=req.task):
            if not cascade:
                return self._structured(req, self.large)

            try:
                response = self._structured(req, self.small)
                if (
                    response.confidence is None
                    or response.confidence >= settings.escalate_conf
                ):
                    return response
                tracer.incr("llm.escalate.low_conf")

            except LLMError as exc:
                if "circuit open" in str(exc):
                    raise
                tracer.incr("llm.escalate.invalid")

            response = self._structured(req, self.large)
            response.escalated = True
            return response


_router: LLMRouter | None = None
_judge: LLMRouter | None = None


def get_router() -> LLMRouter:
    global _router
    if _router is None:
        _router = LLMRouter()
    return _router


def get_judge_router() -> LLMRouter:
    """Use a separate provider/model family to mitigate self-preference bias."""
    global _judge
    if _judge is None:
        _judge = LLMRouter(
            provider=settings.judge_provider,
            small=settings.judge_model,
            large=settings.judge_model,
        )
    return _judge
