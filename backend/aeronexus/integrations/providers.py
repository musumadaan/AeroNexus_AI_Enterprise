"""Google LLM providers for the existing LLM router (optional; selected with AERONEXUS_LLM_PROVIDER).

  gemini   Gemini API through Google AI Studio. Has a free tier (rate-limited; free-tier prompts may be used by Google to
           improve its products, so don't send confidential data). Needs GEMINI_API_KEY from aistudio.google.com.
  vertex   The same models through Google Vertex AI. Needs a Google Cloud project; billing normally applies (new accounts get
           trial credit). Auth: VERTEX_API_KEY (express mode) or a service account (GOOGLE_SERVICE_ACCOUNT_JSON_B64 /
           GOOGLE_APPLICATION_CREDENTIALS) with VERTEX_PROJECT and VERTEX_LOCATION (default "global").

Both speak the same generateContent format, so one class serves both. Token usage is reported to the usage meter, JSON
mode is used when the router asks for structured output, and 429s become RateLimited so the router waits instead of
tripping its circuit breaker, exactly like the Groq provider.
"""
from __future__ import annotations

import base64
import json
import os
import re
from typing import Any

import httpx

from ..llm import LLMError, LLMRequest, Provider, RateLimited
from .secrets import get_secret

DEFAULTS = {  # free-tier Gemini models as listed on ai.google.dev/pricing (October 2026); override with env vars
    "gemini": {"small": "gemini-3.5-flash-lite", "large": "gemini-3.6-flash", "judge": "gemini-3.5-flash-lite"},
    "vertex": {"small": "gemini-3.5-flash-lite", "large": "gemini-3.6-flash", "judge": "gemini-3.5-flash-lite"},
}


class GoogleGenAIProvider(Provider):
    name = "gemini"
    _creds: Any = None

    def __init__(self, vertex: bool = False) -> None:
        self.vertex = vertex
        self.name = "vertex" if vertex else "gemini"

    # ------------------------------------------------------------------ endpoint + auth
    def _endpoint(self, model: str) -> tuple[str, dict[str, str], dict[str, str]]:
        if not self.vertex:
            key = get_secret("GEMINI_API_KEY")
            if not key:
                raise LLMError("GEMINI_API_KEY not set: create a free key at aistudio.google.com and put it in backend/.env")
            return (f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", {"x-goog-api-key": key}, {})
        key = get_secret("VERTEX_API_KEY")
        if key:  # express mode
            return (f"https://aiplatform.googleapis.com/v1/publishers/google/models/{model}:generateContent", {}, {"key": key})
        project = get_secret("VERTEX_PROJECT")
        loc = os.environ.get("VERTEX_LOCATION", "global")
        if not project:
            raise LLMError("Vertex AI needs VERTEX_API_KEY (express mode) or VERTEX_PROJECT plus a service account")
        host = "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"
        url = f"https://{host}/v1/projects/{project}/locations/{loc}/publishers/google/models/{model}:generateContent"
        return url, {"Authorization": f"Bearer {self._token()}"}, {}

    def _token(self) -> str:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        scope = ["https://www.googleapis.com/auth/cloud-platform"]
        if GoogleGenAIProvider._creds is None:
            b64 = get_secret("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
            path = get_secret("GOOGLE_APPLICATION_CREDENTIALS")
            if b64:
                GoogleGenAIProvider._creds = service_account.Credentials.from_service_account_info(json.loads(base64.b64decode(b64)),
                                                                                                    scopes=scope)
            elif path:
                GoogleGenAIProvider._creds = service_account.Credentials.from_service_account_file(path, scopes=scope)
            else:
                raise LLMError("no Google service account configured for Vertex AI")
        if not GoogleGenAIProvider._creds.valid:
            GoogleGenAIProvider._creds.refresh(Request())
        return GoogleGenAIProvider._creds.token

    # ------------------------------------------------------------------ call
    def complete(self, req: LLMRequest, model: str) -> str:
        from ..config import settings
        from ..usage import meter

        url, headers, params = self._endpoint(model)
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": req.system}]},
            "contents": [{"role": "user", "parts": [{"text": req.prompt}]}],
            "generationConfig": {"temperature": req.temperature, "maxOutputTokens": req.max_tokens},
        }
        if req.json_schema:
            body["generationConfig"]["responseMimeType"] = "application/json"
        r = httpx.post(url, json=body, headers={"Content-Type": "application/json", **headers}, params=params,
                       timeout=settings.request_timeout_s)
        if r.status_code == 429:
            m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', r.text)
            raise RateLimited(float(m.group(1)) if m else 10.0, "gemini quota")
        if r.status_code >= 400:
            raise LLMError(f"{self.name} HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        u = data.get("usageMetadata") or {}
        if u.get("promptTokenCount") is not None:
            meter.stash(int(u.get("promptTokenCount", 0)), int(u.get("candidatesTokenCount", 0)))
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"{self.name}: empty response ({data.get('promptFeedback', '')})") from exc
        return "".join(p.get("text", "") for p in parts)


class GeminiProvider(GoogleGenAIProvider):
    def __init__(self) -> None:
        super().__init__(vertex=False)


class VertexProvider(GoogleGenAIProvider):
    def __init__(self) -> None:
        super().__init__(vertex=True)


def register(providers: dict[str, type[Provider]], defaults: dict[str, dict[str, str]]) -> None:
    providers.setdefault("gemini", GeminiProvider)
    providers.setdefault("vertex", VertexProvider)
    for k, v in DEFAULTS.items():
        defaults.setdefault(k, v)
