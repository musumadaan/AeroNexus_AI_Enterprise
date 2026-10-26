"""Predictive-maintenance workflow on the trained C-MAPSS models.

Fleet view → per-engine remaining-useful-life with a 90% interval and a health score →
deterministic work-order rule (versioned `pdm.work_order`) → maintenance work order proposed to the
CMMS as a case in `needs_review`. A certifying technician always decides; the model never removes an
engine from service.

Honesty notes surfaced in the UI: C-MAPSS engines are *simulated*; the "fleet" here is the official
test set (true RUL is shown for evaluation only and would be unknown in production).
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from ..calc import calculate
from ..config import DATA, MODELS
from ..security import AGENT, Principal
from ..services import services

SUBSETS = ("FD001", "FD002", "FD003", "FD004")
TREND_SENSORS = ("s4", "s11", "s15")  # well-known degradation-sensitive sensors in C-MAPSS


@lru_cache(maxsize=1)
def report() -> dict[str, Any]:
    p = DATA / "reports" / "pdm_cmapss.json"
    return json.loads(p.read_text()) if p.exists() else {}


@lru_cache(maxsize=4)
def bundle(subset: str) -> dict[str, Any] | None:
    p = MODELS / f"pdm_{subset}.joblib"
    if not p.exists():
        return None
    import joblib

    return joblib.load(p)


def fleet(subset: str = "FD001") -> dict[str, Any]:
    rep = report().get(subset)
    if not rep:
        return {"available": False, "message": "Train first: python scripts/fetch_public_data.py && python -m aeronexus.pdm.cmapss"}
    b = bundle(subset)
    thr = (b or {}).get("health_threshold") or rep["metrics"].get("health_threshold", 1.0)
    rows = []
    for e in rep["fleet"]:
        wo, _ = calculate("pdm.work_order_needed", rul_low=e["low"], health=e["health"], health_threshold=thr)
        rows.append({**e, "work_order": wo["needed"], "reasons": wo["reasons"]})
    rows.sort(key=lambda r: r["low"])
    return {"available": True, "subset": subset, "metrics": rep["metrics"], "engines": rows, "health_threshold": thr,
            "disclaimer": "Decision support only. C-MAPSS engines are simulated; true RUL is shown for evaluation."}


def engine_trend(subset: str, unit: int) -> dict[str, Any]:
    from ..pdm.cmapss import load

    b = bundle(subset)
    te = load(subset, "test")
    g = te[te["unit"] == unit]
    if b is None or g.empty:
        return {"available": False}
    z = b["normalizer"].transform(g)
    sensors = [s for s in TREND_SENSORS if s in b["normalizer"].sensors]
    series = [{"cycle": int(c), **{s: round(float(v), 3) for s, v in zip(sensors, row)}}
              for c, row in zip(g["cycle"], z[sensors].to_numpy())]
    return {"available": True, "unit": unit, "sensors": sensors, "series": series}


def propose_work_order(subset: str, unit: int, who: Principal = AGENT) -> dict[str, Any]:
    svc = services()
    f = fleet(subset)
    e = next((x for x in f.get("engines", []) if x["unit"] == unit), None)
    if e is None:
        raise KeyError(f"{subset} unit {unit}")
    out, rec = calculate("pdm.work_order_needed", rul_low=e["low"], health=e["health"], health_threshold=f["health_threshold"])
    store = svc.cases
    case = store.open(who.tenant, "maintenance", f"{subset} engine {unit}: RUL {e['pred_rul']} ({e['low']}–{e['high']})",
                      who.user or "agent")
    case = store.put(case.id, case.tenant, "agent", engine=e, rule_receipt=rec.to_dict(), model=f["metrics"]["selected_model"],
                     model_metrics={k: f["metrics"][k] for k in ("test_rmse_capped", "interval_coverage_test")})
    if not out["needed"]:
        case = store.set_status(case.id, case.tenant, "verified", "agent", "no inspection needed under rule pdm.work_order")
        return {"case": case.id, "status": case.status, "proposal": None, "reasons": []}
    prop = svc.runtime.call("cmms.create_maintenance_order",
                            {"asset": f"{subset}-ENG-{unit:03d}", "task": "Borescope HPC and fan inspection; review trend",
                             "reason": "; ".join(out["reasons"]), "due_within_cycles": float(max(1.0, e["low"]))},
                            actor="agent", module="pdm", reason="; ".join(out["reasons"]), case_id=case.id)
    case = store.set_status(case.id, case.tenant, "needs_review", "agent", "technician must confirm before the work order is issued")
    return {"case": case.id, "status": case.status, "proposal": prop, "reasons": out["reasons"]}
