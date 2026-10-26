"""One tiny request per configured model to confirm your Groq key works and show quota headroom.

  python scripts/check_groq.py
Uses ~3 requests of free-tier quota. The key is read from backend/.env or the environment and is never printed.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aeronexus.config import settings  # noqa: E402  (loads backend/.env)
from aeronexus.llm import GroqProvider, LLMError, LLMRequest  # noqa: E402

models = [("small", settings.small_model), ("large", settings.large_model), ("judge", settings.judge_model)]
p = GroqProvider()
ok = True
for role, m in models:
    t0 = time.perf_counter()
    try:
        out = p.complete(LLMRequest(task="ping", prompt='Reply with JSON {"ok": true}', json_schema={"ok": "bool"},
                                    max_tokens=20), m)
        print(f"{role:<6} {m:<28} OK  {1000 * (time.perf_counter() - t0):6.0f} ms  reply={out.strip()[:40]!r}")
    except LLMError as e:
        ok = False
        print(f"{role:<6} {m:<28} FAILED: {e}")
    except Exception as e:  # HTTP errors (bad model id, invalid key …)
        ok = False
        print(f"{role:<6} {m:<28} FAILED: {type(e).__name__}: {str(e)[:120]}")
print("quota headers:", {k: v for k, v in GroqProvider.last_limits.items() if "remaining" in k})
sys.exit(0 if ok else 1)
