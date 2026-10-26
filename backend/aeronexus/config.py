"""Central configuration. Everything is overridable with environment variables so the same
code runs offline (mock LLM, in-memory stores) for tests/demo and against real providers in prod."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # backend/
DATA = ROOT / "data"
MODELS = ROOT / "models"  # artifacts exported from the Colab notebooks land here


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader (no dependency). Real environment variables always win; the key never gets logged."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


# Sensible free-tier defaults per provider (override with AERONEXUS_SMALL/LARGE/JUDGE_MODEL).
# Groq: Llama 3.1 8B (fast, cheap) → Llama 3.3 70B (escalation). The judge uses a *different* model family
# (gpt-oss, open weights, also free on Groq) to avoid a model grading its own family's answers.
PROVIDER_DEFAULTS = {
    "groq": {"small": "llama-3.1-8b-instant", "large": "llama-3.3-70b-versatile", "judge": "openai/gpt-oss-20b"},
    "ollama": {"small": "llama3.1:8b", "large": "llama3.3:70b", "judge": "qwen2.5:14b-instruct"},
    "mock": {"small": "mock-small-3b", "large": "mock-large-70b", "judge": "mock-judge"},
    # optional Google providers (integration layer): Gemini API free tier via AI Studio, or Vertex AI
    "gemini": {"small": "gemini-3.5-flash-lite", "large": "gemini-3.6-flash", "judge": "gemini-3.5-flash-lite"},
    "vertex": {"small": "gemini-3.5-flash-lite", "large": "gemini-3.6-flash", "judge": "gemini-3.5-flash-lite"},
}


def _model(kind: str, provider_env: str, env_name: str) -> str:
    provider = _env(provider_env, "mock")
    return _env(env_name, PROVIDER_DEFAULTS.get(provider, PROVIDER_DEFAULTS["mock"])[kind])


@dataclass(frozen=True)
class Settings:
    # --- LLM routing -------------------------------------------------------
    # provider: mock | groq (free tier, Llama) | ollama (local) | openai (any OpenAI-compatible server) | anthropic
    llm_provider: str = field(default_factory=lambda: _env("AERONEXUS_LLM_PROVIDER", "mock"))
    small_model: str = field(default_factory=lambda: _model("small", "AERONEXUS_LLM_PROVIDER", "AERONEXUS_SMALL_MODEL"))
    large_model: str = field(default_factory=lambda: _model("large", "AERONEXUS_LLM_PROVIDER", "AERONEXUS_LARGE_MODEL"))
    # The judge must come from a different model family than the generator (self-preference bias).
    judge_provider: str = field(default_factory=lambda: _env("AERONEXUS_JUDGE_PROVIDER", "mock"))
    judge_model: str = field(default_factory=lambda: _model("judge", "AERONEXUS_JUDGE_PROVIDER", "AERONEXUS_JUDGE_MODEL"))
    groq_base_url: str = field(default_factory=lambda: _env("GROQ_BASE_URL", "https://api.groq.com/openai/v1"))
    # client-side pacing so the free tier never sees a burst (Groq free plans are ~30 requests/minute per model)
    groq_rpm: float = float(_env("AERONEXUS_GROQ_RPM", "28"))
    max_rate_limit_wait_s: float = float(_env("AERONEXUS_MAX_RATE_LIMIT_WAIT_S", "30"))
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://localhost:11434"))
    openai_base_url: str = field(default_factory=lambda: _env("OPENAI_BASE_URL", "https://api.openai.com/v1"))

    # --- Latency / reliability budgets ------------------------------------
    request_timeout_s: float = float(_env("AERONEXUS_TIMEOUT_S", "20"))
    max_retries: int = int(_env("AERONEXUS_MAX_RETRIES", "2"))
    circuit_fail_threshold: int = int(_env("AERONEXUS_CIRCUIT_FAILS", "5"))
    circuit_reset_s: float = float(_env("AERONEXUS_CIRCUIT_RESET_S", "30"))
    cache_ttl_s: float = float(_env("AERONEXUS_CACHE_TTL_S", "3600"))
    p95_budget_ms: float = float(_env("AERONEXUS_P95_BUDGET_MS", "2500"))

    # --- Confidence routing (human in the loop) ----------------------------
    auto_accept_conf: float = float(_env("AERONEXUS_AUTO_ACCEPT", "0.90"))
    escalate_conf: float = float(_env("AERONEXUS_ESCALATE", "0.60"))  # below this: send to small→large cascade
    groundedness_min: float = float(_env("AERONEXUS_GROUNDEDNESS_MIN", "0.70"))

    # --- Optional heavyweight components (loaded only if installed) --------
    use_hf_ner: bool = _env("AERONEXUS_USE_HF_NER", "0") == "1"
    use_nli: bool = _env("AERONEXUS_USE_NLI", "0") == "1"
    use_st_embeddings: bool = _env("AERONEXUS_USE_ST", "0") == "1"

    # --- Durable workflow state (SQLite file by default; ':memory:' in tests) ---
    state_db: str = field(default_factory=lambda: _env("AERONEXUS_STATE_DB", str(DATA / "state.db")))


settings = Settings()
