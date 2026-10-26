"""Feature flags for the enterprise-integration layer. Everything is opt-out or opt-in through environment variables,
so the existing application behaves exactly as before when the layer is switched off.

    AERONEXUS_FF_INTEGRATIONS=1   master switch for this whole package (routes, tools, listeners)
    AERONEXUS_FF_SAP_MOCK=1       local mock SAP procurement connector (no SAP licence needed)
    AERONEXUS_FF_SHEETS=auto      Google Sheets tracking: auto = on when credentials are configured, else a local CSV
    AERONEXUS_FF_MCP=1            allow the MCP server to start (python -m aeronexus.integrations.mcp_server)

    AERONEXUS_FF_SALESFORCE=off   off | mock (local) | live (free Salesforce Developer Edition org)
    AERONEXUS_FF_DATABRICKS=0     trigger the PySpark feature job on Databricks Free Edition
    AERONEXUS_LLM_PROVIDER=gemini Gemini API free tier (AI Studio key); =vertex for Vertex AI (billing applies)

Paid-only services (SAP S/4HANA writes, AppSheet) are not implemented; `catalog()` says so, so the settings screen is
honest about what is real, what is a mock and what costs money.
"""
from __future__ import annotations

import os
from typing import Any


def _on(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def integrations_enabled() -> bool:
    return _on("AERONEXUS_FF_INTEGRATIONS")


def sap_enabled() -> bool:
    return integrations_enabled() and _on("AERONEXUS_FF_SAP_MOCK")


def mcp_enabled() -> bool:
    return integrations_enabled() and _on("AERONEXUS_FF_MCP")


def sheets_mode() -> str:
    """'google' (real Sheets API), 'local' (CSV fallback) or 'off'."""
    if not integrations_enabled():
        return "off"
    v = os.environ.get("AERONEXUS_FF_SHEETS", "auto").strip().lower()
    if v in ("0", "off", "false", "no"):
        return "off"
    if v == "local":
        return "local"
    has_creds = bool(
    os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID")
    and (
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        or os.path.exists(
            os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
        )
    )
)
    if v in ("1", "on", "true", "yes", "google"):
        return "google" if has_creds else "local"
    return "google" if has_creds else "local"


def _sf_mode() -> str:
    v = os.environ.get("AERONEXUS_FF_SALESFORCE", "off").strip().lower()
    return v if integrations_enabled() and v in ("mock", "live") else "off"


def _llm() -> str:
    return os.environ.get("AERONEXUS_LLM_PROVIDER", "mock").strip().lower()


def catalog() -> list[dict[str, Any]]:
    return [
        {"id": "mcp", "label": "MCP server", "enabled": mcp_enabled(), "cost": "free (open-source SDK)",
         "flag": "AERONEXUS_FF_MCP"},
        {"id": "sap_mock", "label": "SAP procurement (mock)", "enabled": sap_enabled(), "cost": "free (runs inside the backend)",
         "flag": "AERONEXUS_FF_SAP_MOCK"},
        {"id": "sheets", "label": "Google Sheets tracking", "enabled": sheets_mode() != "off", "mode": sheets_mode(),
         "cost": "free (Google account + service account)", "flag": "AERONEXUS_FF_SHEETS"},
        {"id": "approvals", "label": "Human approval queue", "enabled": integrations_enabled(), "cost": "free (built in)",
         "flag": "AERONEXUS_FF_INTEGRATIONS"},
        {"id": "github_actions", "label": "GitHub Actions CI/CD", "enabled": True, "cost": "free for public repos / free minutes",
         "flag": "-"},
        {"id": "salesforce", "label": "Salesforce", "enabled": _sf_mode() != "off", "mode": _sf_mode(),
         "cost": "free (local mock, or a free Developer Edition org)", "flag": "AERONEXUS_FF_SALESFORCE"},
        {"id": "gemini", "label": "Gemini API (Google AI Studio)", "enabled": _llm() == "gemini",
         "cost": "free tier (rate-limited; free-tier prompts may be used to improve Google products)", "flag": "AERONEXUS_LLM_PROVIDER=gemini"},
        {"id": "vertex_ai", "label": "Google Vertex AI", "enabled": _llm() == "vertex",
         "cost": "billing account normally required (trial credit for new accounts)", "flag": "AERONEXUS_LLM_PROVIDER=vertex"},
        {"id": "databricks", "label": "Databricks / PySpark pipeline", "enabled": _on("AERONEXUS_FF_DATABRICKS", "0") and integrations_enabled(),
         "cost": "free (local PySpark, or Databricks Free Edition for non-commercial use)", "flag": "AERONEXUS_FF_DATABRICKS"},
        {"id": "railway", "label": "Railway deployment", "enabled": bool(os.environ.get("RAILWAY_ENVIRONMENT")),
         "cost": "paid usage (your account)", "flag": "-"},
        {"id": "sap_real", "label": "SAP S/4HANA", "enabled": False, "cost": "paid licence: not implemented",
         "flag": "-", "note": "The mock follows SAP's purchase requisition / purchase order field names, so a real OData adapter can replace it. "
                             "SAP's Business Accelerator Hub sandbox (free account) is read-only, so writes stay on the mock."},
    ]
