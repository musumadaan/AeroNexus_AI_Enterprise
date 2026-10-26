"""Secrets: read from the environment or from mounted secret files, never from code, and never echoed back.

    get_secret("GROQ_API_KEY")  →  $GROQ_API_KEY, else the contents of the file at $GROQ_API_KEY_FILE
                                   (Docker / Kubernetes secret mounts), else None.

Railway "Variables", GitHub Actions "Secrets" and a git-ignored backend/.env all land in the environment, so the same
code works locally, in CI and in production. `report()` lists which secrets are configured as booleans only; the
settings screen and the MCP status never see a value. CI additionally runs gitleaks so a committed key fails the build.
"""
from __future__ import annotations

import os
from pathlib import Path

KNOWN = {
    "GROQ_API_KEY": "Llama on Groq (free tier)",
    "GEMINI_API_KEY": "Gemini API via Google AI Studio (free tier)",
    "VERTEX_API_KEY": "Vertex AI express mode (optional, billing may apply)",
    "GOOGLE_SERVICE_ACCOUNT_JSON_B64": "Google service account (Sheets, Vertex)",
    "GOOGLE_APPLICATION_CREDENTIALS": "Google service account key file path",
    "GOOGLE_SHEETS_SPREADSHEET_ID": "Tracking spreadsheet id",
    "SALESFORCE_CLIENT_SECRET": "Salesforce Developer Edition connected app",
    "SALESFORCE_ACCESS_TOKEN": "Salesforce session token",
    "DATABRICKS_TOKEN": "Databricks Free Edition personal access token",
    "LANGFUSE_SECRET_KEY": "Langfuse tracing",
    "LANGSMITH_API_KEY": "LangSmith tracing",
}


def get_secret(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v:
        return v.strip()
    f = os.environ.get(f"{name}_FILE")
    if f and Path(f).is_file():
        return Path(f).read_text().strip()
    return default


def has(name: str) -> bool:
    return bool(get_secret(name))


def mask(v: str | None) -> str:
    if not v:
        return ""
    return "•" * 6 + v[-3:] if len(v) > 12 else "•" * 6


def report() -> list[dict[str, object]]:
    """Which secrets are configured: booleans and purpose only, never values."""
    return [{"name": k, "purpose": v, "configured": has(k), "source": "file" if (not os.environ.get(k) and has(k)) else
             ("env" if has(k) else None)} for k, v in KNOWN.items()]
