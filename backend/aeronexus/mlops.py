"""Experiment tracking, reproducible evaluation reports, regression comparison and agent reliability.

* MLflow: every evaluation run is logged to a local SQLite tracking store (data/mlflow.db; override with
  MLFLOW_TRACKING_URI) with params (code hash, data checksums, versions, LLM provider and models), metrics
  (every gated metric plus the public-model results) and artifacts (the JSON report and the Markdown report).
  Browse with:  uvx mlflow ui --backend-store-uri sqlite:///backend/data/mlflow.db
* Reproducibility: the report fingerprints the code (SHA-256 over aeronexus/**/*.py), the golden data and the
  model files, the thresholds and library versions, and prints the exact command that regenerates it.
* Regression: each run is compared with the previous one. A metric that moves the wrong way by more than its
  tolerance is flagged even when it still clears its gate, so slow decay is visible before it fails CI.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import statistics
import time
from pathlib import Path
from typing import Any

from .config import DATA, settings

HIST = Path(os.environ["AERONEXUS_HISTORY_DIR"]) if os.environ.get("AERONEXUS_HISTORY_DIR") else DATA / "reports" / "history"
PKG = Path(__file__).resolve().parent
TOLERANCE = 0.005


def _sha(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        if p.is_file():
            h.update(p.name.encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def fingerprint() -> dict[str, Any]:
    import numpy
    import sklearn

    models = DATA.parent / "models"
    return {
        "code_sha": _sha(list(PKG.rglob("*.py"))),
        "golden_data_sha": _sha(list((DATA / "golden").glob("*"))),
        "kb_sha": _sha(list((DATA / "kb").glob("*"))),
        "models_sha": _sha(list(models.glob("*.joblib"))),
        "thresholds_sha": _sha([PKG / "evals" / "thresholds.json"]),
        "python": platform.python_version(), "numpy": numpy.__version__, "scikit_learn": sklearn.__version__,
        "llm_provider": settings.llm_provider, "small_model": settings.small_model, "large_model": settings.large_model,
        "seed": 7,
    }


def flat_metrics(rep: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for c in rep.get("gate", {}).get("checks", []):
        v = c.get("value")
        if isinstance(v, bool):
            v = float(v)
        if isinstance(v, (int, float)):
            out[c["metric"]] = float(v)
    for k, v in (rep.get("latency") or {}).items():
        if isinstance(v, dict) and "p95" in v:
            out[f"latency.{k}.p95_ms"] = float(v["p95"])
    out["gate.passed"] = float(bool(rep.get("gate", {}).get("passed")))
    out["eval.duration_s"] = float(rep.get("duration_s") or 0)
    return out


def _directions(rep: dict[str, Any]) -> dict[str, str]:
    return {c["metric"]: ("up" if c.get("op") in ("ge", "gt", "eq") else "down") for c in rep.get("gate", {}).get("checks", [])}


def report_markdown(rep: dict[str, Any], fp: dict[str, Any], regression: dict[str, Any] | None, run_id: str) -> str:
    g = rep.get("gate", {})
    lines = [f"# AeroNexus evaluation report {run_id}", "",
             f"Generated {rep.get('generated_at')} · gate **{'PASSED' if g.get('passed') else 'FAILED'}** "
             f"({sum(c['passed'] for c in g.get('checks', []))}/{len(g.get('checks', []))} checks)", "",
             "## Reproduce", "", "```bash", "cd backend",
             f"AERONEXUS_STATE_DB=:memory: AERONEXUS_LLM_PROVIDER={fp['llm_provider']} python -m aeronexus.evals.runner",
             "```", "", "## Fingerprint", "", "| Item | Value |", "|---|---|"]
    lines += [f"| {k} | `{v}` |" for k, v in fp.items()]
    lines += ["", "## Gate", "", "| Metric | Value | Target | Pass |", "|---|---|---|---|"]
    lines += [f"| {c['metric']} | {c['value']} | {c['op']} {c['target']} | {'yes' if c['passed'] else '**no**'} |" for c in g.get("checks", [])]
    if regression:
        lines += ["", "## Change since previous run", "", f"Compared with `{regression.get('previous')}`.", "",
                  "| Metric | Previous | Now | Δ | Flag |", "|---|---|---|---|---|"]
        lines += [f"| {r['metric']} | {r['previous']} | {r['current']} | {r['delta']:+.4f} | {r['flag']} |"
                  for r in regression["rows"] if r["delta"] != 0]
    lines += ["", "## Data provenance", ""] + [f"- **{k}**: {v}" for k, v in (rep.get("provenance") or {}).items()]
    return "\n".join(lines) + "\n"


def history(limit: int = 30) -> list[dict[str, Any]]:
    if not HIST.exists():
        return []
    out = []
    for p in sorted(HIST.glob("*.json"))[-limit:]:
        try:
            out.append(json.loads(p.read_text()))
        except ValueError:
            continue
    return out


def regression_vs_previous(cur: dict[str, float], directions: dict[str, str]) -> dict[str, Any] | None:
    prev = history(1)
    if not prev:
        return None
    p = prev[-1]
    rows = []
    for k, v in cur.items():
        if k not in p["metrics"] or k.startswith(("latency.", "eval.")):
            continue
        d = v - p["metrics"][k]
        # timings are noisy: flag latency only when it moves by more than 25%; quality metrics by 0.005
        tol = max(TOLERANCE, 0.25 * abs(p["metrics"][k])) if "latency" in k else TOLERANCE
        worse = (d < -tol) if directions.get(k, "up") == "up" else (d > tol)
        better = (d > tol) if directions.get(k, "up") == "up" else (d < -tol)
        rows.append({"metric": k, "previous": p["metrics"][k], "current": v, "delta": round(d, 6),
                     "flag": "regressed" if worse else "improved" if better else "same"})
    return {"previous": p["run_id"], "rows": rows, "regressed": [r["metric"] for r in rows if r["flag"] == "regressed"],
            "improved": [r["metric"] for r in rows if r["flag"] == "improved"]}


def record_eval(rep: dict[str, Any], who: str = "ci", log_mlflow: bool = True) -> dict[str, Any]:
    """Fingerprint, compare with the previous run, write history + Markdown, log to MLflow."""
    fp = fingerprint()
    metrics = flat_metrics(rep)
    reg = regression_vs_previous(metrics, _directions(rep))
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + fp["code_sha"][:6]
    md = report_markdown(rep, fp, reg, run_id)
    HIST.mkdir(parents=True, exist_ok=True)
    entry = {"run_id": run_id, "generated_at": rep.get("generated_at"), "by": who, "fingerprint": fp, "metrics": metrics,
             "gate_passed": bool(rep.get("gate", {}).get("passed")), "regression": reg, "mlflow_run_id": None}
    (HIST / f"{run_id}.md").write_text(md)
    if log_mlflow and os.environ.get("AERONEXUS_MLFLOW", "on") != "off":
        entry["mlflow_run_id"] = _log_mlflow(rep, fp, metrics, md, run_id)
    (HIST / f"{run_id}.json").write_text(json.dumps(entry, default=str))
    return entry


def tracking_uri() -> str:
    return os.environ.get("MLFLOW_TRACKING_URI") or f"sqlite:///{DATA / 'mlflow.db'}"


def _log_mlflow(rep: dict[str, Any], fp: dict[str, Any], metrics: dict[str, float], md: str, run_id: str) -> str | None:
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    try:
        import tempfile

        import mlflow

        mlflow.set_tracking_uri(tracking_uri())
        mlflow.set_experiment("aeronexus-evals")
        with mlflow.start_run(run_name=run_id) as r:
            mlflow.log_params({k: str(v)[:250] for k, v in fp.items()})
            mlflow.log_metrics({k.replace("@", "_at_"): v for k, v in metrics.items()})
            mlflow.set_tags({"gate": "passed" if rep.get("gate", {}).get("passed") else "failed", "app": "aeronexus"})
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / "report.json").write_text(json.dumps(rep, default=str))
                (Path(d) / "report.md").write_text(md)
                mlflow.log_artifacts(d)
            return r.info.run_id
    except Exception as exc:  # tracking must never break an evaluation
        return f"not logged: {type(exc).__name__}: {exc}"[:200]


def mlflow_runs(limit: int = 20) -> list[dict[str, Any]]:
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    try:
        import mlflow

        mlflow.set_tracking_uri(tracking_uri())
        runs = mlflow.search_runs(experiment_names=["aeronexus-evals"], output_format="list", max_results=limit,
                                  order_by=["attributes.start_time DESC"])
        return [{"run_id": r.info.run_id, "name": r.info.run_name, "start": r.info.start_time, "status": r.info.status,
                 "gate": r.data.tags.get("gate"), "metrics": {k: v for k, v in r.data.metrics.items()
                                                              if not k.startswith("latency.")}} for r in runs]
    except Exception:
        return []


def agent_reliability() -> dict[str, Any]:
    """Per agent / step type: runs, success, failures, retries, fallbacks and latency, from recorded runs."""
    from .orchestration.runs import store

    by: dict[str, dict[str, Any]] = {}
    runs = [r for r in store.list(limit=500) if r.kind in ("mission", "workflow")]
    for r in runs:
        fallbacks = {e["node"] for e in r.events if e["type"] == "fallback"}
        for nid, n in r.nodes.items():
            t = n.get("type") or nid
            if t.startswith("trigger."):
                continue
            key = t.replace("mission.", "agent.") if t.startswith("mission.") else t
            b = by.setdefault(key, {"type": key, "label": n.get("label") or key, "executions": 0, "succeeded": 0, "failed": 0,
                                    "retries": 0, "fallbacks": 0, "ms": []})
            if n.get("status") in ("completed", "failed"):
                b["executions"] += 1
                b["succeeded"] += n["status"] == "completed"
                b["failed"] += n["status"] == "failed"
                b["retries"] += max(0, int(n.get("attempts") or 0) - 1)
                b["fallbacks"] += nid in fallbacks
                if n.get("ms") is not None:
                    b["ms"].append(float(n["ms"]))
    rows = []
    for b in by.values():
        ms = sorted(b.pop("ms"))
        n = b["executions"]
        rows.append({**b, "success_rate": round(b["succeeded"] / n, 4) if n else None,
                     "p50_ms": round(ms[len(ms) // 2], 1) if ms else None,
                     "p95_ms": round(ms[min(len(ms) - 1, int(0.95 * len(ms)))], 1) if ms else None})
    total = len(runs)
    statuses: dict[str, int] = {}
    for r in runs:
        statuses[r.status] = statuses.get(r.status, 0) + 1
    durs = [r.metrics.get("duration_ms") for r in runs if r.metrics.get("duration_ms")]
    return {"runs": total, "statuses": statuses, "steps": sorted(rows, key=lambda x: -x["executions"]),
            "run_p50_ms": round(statistics.median(durs), 1) if durs else None,
            "completion_rate": round(statuses.get("completed", 0) / max(1, total - statuses.get("waiting_approval", 0)), 4)
            if total else None}
