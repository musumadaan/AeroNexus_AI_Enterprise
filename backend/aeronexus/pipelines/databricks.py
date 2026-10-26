"""Trigger and monitor the feature pipeline on Databricks (Free Edition works; it is free for non-commercial use).

    DATABRICKS_HOST    https://<workspace>.cloud.databricks.com
    DATABRICKS_TOKEN   personal access token (User settings › Developer)
    DATABRICKS_JOB_ID  the job that runs notebooks/databricks/cmapss_feature_pipeline.py
    AERONEXUS_FF_DATABRICKS=1

Uses the Jobs API 2.1 (run-now, runs/get) with plain HTTPS, so no SDK is needed.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from ..integrations.secrets import get_secret


def enabled() -> bool:
    from ..integrations.flags import integrations_enabled

    return integrations_enabled() and os.environ.get("AERONEXUS_FF_DATABRICKS", "0").lower() in ("1", "true", "on", "yes")


def configured() -> bool:
    return bool(get_secret("DATABRICKS_HOST") and get_secret("DATABRICKS_TOKEN") and os.environ.get("DATABRICKS_JOB_ID"))


def _client() -> httpx.Client:
    if not configured():
        raise RuntimeError("set DATABRICKS_HOST, DATABRICKS_TOKEN and DATABRICKS_JOB_ID")
    return httpx.Client(base_url=get_secret("DATABRICKS_HOST").rstrip("/"),  # type: ignore[union-attr]
                        headers={"Authorization": f"Bearer {get_secret('DATABRICKS_TOKEN')}"}, timeout=30)


def run_now(subset: str = "FD001") -> dict[str, Any]:
    with _client() as c:
        r = c.post("/api/2.1/jobs/run-now", json={"job_id": int(os.environ["DATABRICKS_JOB_ID"]),
                                                   "job_parameters": {"subset": subset}})
        r.raise_for_status()
        return r.json()


def run_status(run_id: int) -> dict[str, Any]:
    with _client() as c:
        r = c.get("/api/2.1/jobs/runs/get", params={"run_id": run_id})
        r.raise_for_status()
        d = r.json()
    st = d.get("state", {})
    return {"run_id": run_id, "life_cycle": st.get("life_cycle_state"), "result": st.get("result_state"),
            "message": st.get("state_message"), "url": d.get("run_page_url")}


def status() -> dict[str, Any]:
    return {"enabled": enabled(), "configured": configured(), "job_id": os.environ.get("DATABRICKS_JOB_ID"),
            "host": (get_secret("DATABRICKS_HOST") or "").split("//")[-1] or None,
            "notebook": "notebooks/databricks/cmapss_feature_pipeline.py",
            "local": "pip install -r requirements-spark.txt && python -m aeronexus.pipelines.spark_features --subset FD001"}
