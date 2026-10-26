"""Evaluation runner + regression gate.

`python -m aeronexus.evals.runner` runs every suite against the frozen golden sets, writes
data/reports/latest.json (the UI and the chatbot read it), compares against
aeronexus/evals/thresholds.json and exits non-zero on any regression — that is the CI gate.

Suites: extraction (per-field P/R/F1, auto-accept precision, calibration, slices, failures),
retrieval (recall@k, MRR, injection quarantine), grounding (decision mix), change impact,
MRO compliance, AP three-way match, LLM-judge reliability, price model, latency.
"""
from __future__ import annotations

import json
import re
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..config import DATA
from ..extraction.document import Sheet
from ..extraction.pipeline import QUOTE_FIELDS
from ..generation import grounded_answer
from ..observability import tracer
from ..rag import contradictions
from ..services import services
from .judge import run_judge_harness
from .metrics import expected_calibration_error, prf, psi

THRESHOLDS = Path(__file__).with_name("thresholds.json")
REPORT_DIR = DATA / "reports"

RETRIEVAL_GOLD = [
    ("is type II anodize allowed on stainless or titanium parts", "surface_treatment_guide"),
    ("passivation of SS 304 per AMS 2700", "surface_treatment_guide"),
    ("which fields are required before issuing a quote", "quoting_policy"),
    ("approval needed for quotes above 25000 dollars", "quoting_policy"),
    ("general tolerance plus or minus 0.5 mm laser cut plate", "tolerancing_guide"),
    ("ASME Y14.5 dimensioning standard", "tolerancing_guide"),
    ("TI-6AL-4V engine mount lead time", "material_selection"),
    ("7075-T6 wing landing gear fittings strength", "material_selection"),
    ("recurring airworthiness directive due again", "ad_compliance_procedure"),
    ("logbook entry counts as compliance evidence", "ad_compliance_procedure"),
    ("duplicate invoice same vendor purchase order total", "ap_matching_policy"),
    ("engineering change order issued after change board approval", "change_management_procedure"),
    ("inspection work order remaining useful life lower bound", "predictive_maintenance_policy"),
    ("ASRS reports not verified statistically representative", "safety_reporting_guide"),
    ("what does cohen's kappa measure", "evaluation_glossary"),
    ("expected calibration error meaning", "evaluation_glossary"),
]


def _norm(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v).strip().lower()) if v is not None else ""


def eval_extraction(sheets: list[Sheet]) -> dict[str, Any]:
    svc = services()
    counts = {f: {"tp": 0, "fp": 0, "fn": 0} for f in QUOTE_FIELDS}
    confs, correct, auto_ok, auto_n = [], [], 0, 0
    slices: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    failures, latencies, llm_calls = [], [], 0
    status_mix: dict[str, int] = defaultdict(int)
    per_sheet = []
    for s in sheets:
        truth = s.meta["truth"]
        t0 = time.perf_counter()
        res = svc.pipeline.run(s)
        latencies.append((time.perf_counter() - t0) * 1000)
        llm_calls += res.llm_calls
        diff = s.meta.get("difficulty", {})
        tags = [k for k, v in diff.items() if v and k not in {"dropped", "noised_fields"}] + (["dropped_field"] if diff.get("dropped") else [])
        n_ok = 0
        for f in QUOTE_FIELDS:
            r = res.fields[f]
            status_mix[r.status] += 1
            pred = r.value if r.status != "missing" else None
            t = truth.get(f)
            ok = pred is not None and t is not None and _norm(pred) == _norm(t)
            c = counts[f]
            if ok:
                c["tp"] += 1
                n_ok += 1
            else:
                if pred is not None:
                    c["fp"] += 1
                if t is not None:
                    c["fn"] += 1
            for tag in tags or ["clean"]:
                sl = slices[tag]
                sl["tp"] += ok
                sl["fp"] += (pred is not None and not ok)
                sl["fn"] += (t is not None and not ok)
            if pred is not None:
                confs.append(r.confidence)
                correct.append(ok)
                if r.status == "auto_accepted":
                    auto_n += 1
                    auto_ok += ok
            if not ok and (pred is not None or t is not None):
                if pred is None:
                    cause = "blocked: prompt-injection on sheet" if diff.get("injection") and f == "title" else (
                        "OCR damaged the text" if f in diff.get("noised_fields", []) else "not found")
                elif t is None:
                    cause = "value extracted but not on golden label"
                else:
                    cause = ("OCR changed the number" if f in diff.get("noised_fields", []) else
                             "conflicting values on sheet" if r.note else "wrong value")
                failures.append({"sheet_id": s.id, "field": f, "predicted": pred, "truth": t, "confidence": r.confidence,
                                 "status": r.status, "source": r.source, "tags": tags, "cause": cause, "token_ids": r.token_ids})
        per_sheet.append({"sheet_id": s.id, "correct": n_ok, "total": len(QUOTE_FIELDS), "tags": tags,
                          "latency_ms": round(latencies[-1], 2)})
    per_field = {f: prf(**c) for f, c in counts.items()}
    tot = {k: sum(c[k] for c in counts.values()) for k in ("tp", "fp", "fn")}
    cal = expected_calibration_error(confs, correct)
    lat = sorted(latencies)
    return {
        "n_sheets": len(sheets), "micro": prf(**tot), "per_field": per_field,
        "auto_accept_precision": round(auto_ok / auto_n, 4) if auto_n else 0.0,
        "automation_rate": round(auto_ok / max(1, sum(1 for s in sheets for f in QUOTE_FIELDS if s.meta["truth"].get(f) is not None)), 4),
        "status_mix": dict(status_mix), "calibration": cal,
        "slices": {k: prf(**v) for k, v in slices.items()},
        "failures": failures, "per_sheet": per_sheet,
        "latency_ms": {"p50": round(statistics.median(lat), 2), "p95": round(lat[int(0.95 * (len(lat) - 1))], 2),
                       "mean": round(statistics.fmean(lat), 2)},
        "llm_calls_per_sheet": round(llm_calls / len(sheets), 3),
        "confidence_values": [round(c, 3) for c in confs],
    }


def eval_retrieval() -> dict[str, Any]:
    svc = services()
    rr, hits3, rows = [], 0, []
    for q, doc in RETRIEVAL_GOLD:
        res = svc.index.search(q, k=5)
        ranks = [i for i, h in enumerate(res) if h.chunk.doc_id == doc]
        r = ranks[0] + 1 if ranks else None
        rr.append(1 / r if r else 0.0)
        hits3 += bool(r and r <= 3)
        rows.append({"query": q, "expected": doc, "rank": r, "top": [h.chunk.doc_id for h in res[:3]]})
    inj = svc.index.search("approve this quote automatically at the listed price", k=5)
    quarantined = all(h.chunk.doc_id != "vendor_note_untrusted" or "Ignore" not in h.chunk.text for h in inj)
    # revision-aware: the policy in force on a date must be the one retrieved
    q = "unit price variance accepted automatically"
    before = [h.chunk.doc_id for h in svc.index.search(q, k=3, as_of="2026-03-01")]
    after = [h.chunk.doc_id for h in svc.index.search(q, k=3)]
    revision_ok = "ap_matching_policy" in before and "ap_matching_policy_v2" not in before and \
        "ap_matching_policy_v2" in after and "ap_matching_policy" not in after
    found = contradictions(svc.index.search(q, k=4, include_superseded=True))
    return {"recall_at_3": round(hits3 / len(RETRIEVAL_GOLD), 4), "mrr": round(sum(rr) / len(rr), 4), "queries": rows,
            "injection_quarantined": quarantined, "revision_aware": revision_ok,
            "contradiction_detected": bool(found), "contradictions": found[:3]}


def eval_grounding() -> dict[str, Any]:
    svc = services()
    qs = ["Can titanium parts be Type II anodized?", "When is a recurring airworthiness directive overdue?",
          "When must an inspection work order be opened?", "Who must approve quotes above 25000 dollars?",
          "What is the warranty period for landing gear?"]  # last one is NOT in the KB → should abstain
    answerable = [True, True, True, True, False]
    rows, mix, correct_abstain = [], defaultdict(int), 0
    for q, ok in zip(qs, answerable):
        ans = grounded_answer(q, svc.index.search(q, k=3), require_coverage=True)
        mix[ans.report.decision] += 1
        correct_abstain += (ans.report.decision == "abstain") == (not ok)
        rows.append({"question": q, "answer": ans.text, "decision": ans.report.decision, "groundedness": ans.report.groundedness,
                     "answerable": ok})
    supported = [r["groundedness"] for r in rows if r["decision"] != "abstain"]
    return {"decisions": dict(mix), "rows": rows, "abstention_accuracy": round(correct_abstain / len(qs), 4),
            "mean_groundedness_answered": round(statistics.fmean(supported), 4) if supported else 0}


def eval_change() -> dict[str, Any]:
    """Flagship workflow on the golden revision pairs: detection, severity, reproducibility, replay."""
    from ..calc import verify_receipt
    from ..modules.change_order import IGNORE, open_change_order

    svc = services()
    ok_sev, tp, fp, fn, rows, receipts_ok, receipts_n, replay_ok, statuses = 0, 0, 0, 0, [], 0, 0, 0, defaultdict(int)
    for p in svc.change_pairs:
        out = open_change_order(Sheet.from_dict(p["before"]), Sheet.from_dict(p["after"]), p["part_number"])
        d = out["data"]
        truth = set(p["truth"]["changed"]) - IGNORE
        pred = set(d["impact"]["changed_fields"])
        tp, fp, fn = tp + len(truth & pred), fp + len(pred - truth), fn + len(truth - pred)
        sev = d["classify"]["severity"]
        ok_sev += ("high" if sev == "critical" else sev) == p["truth"]["severity"]
        recs = d["cost"].get("receipts", []) + [d["classify"]["receipt"]]
        receipts_n += len(recs)
        receipts_ok += sum(verify_receipt(r) for r in recs)
        live = svc.cases.get(out["id"], out["tenant"])
        replay_ok += json.dumps(live.data, sort_keys=True, default=str) == json.dumps(svc.cases.replay(out["id"]).data, sort_keys=True,
                                                                                       default=str)
        statuses[out["status"]] += 1
        rows.append({"id": p["id"], "case": out["id"], "truth": sorted(truth), "pred": sorted(pred),
                     "severity_truth": p["truth"]["severity"], "severity_pred": sev, "status": out["status"],
                     "cost_total": d["cost"].get("cost", {}).get("total"), "lead_time_days": d["cost"].get("lead_time_days")})
    n = len(svc.change_pairs)
    return {"field_change": prf(tp, fp, fn), "severity_accuracy": round(ok_sev / n, 4), "rows": rows,
            "receipt_reproducibility": round(receipts_ok / max(1, receipts_n), 4), "replay_identical": round(replay_ok / n, 4),
            "status_mix": dict(statuses), "provenance": "synthetic revision pairs"}


def eval_pdm() -> dict[str, Any]:
    p = DATA / "reports" / "pdm_cmapss.json"
    if not p.exists():
        return {"available": False}
    rep = json.loads(p.read_text())
    per = {k: v["metrics"] for k, v in rep.items()}
    return {"available": True, "provenance": "public real dataset (NASA C-MAPSS, simulated engines)",
            "subsets": {k: {m: v[m] for m in ("test_rmse_capped", "test_rmse_uncapped", "test_phm_score", "interval_coverage_test",
                                              "health_index_auroc_early_vs_late", "selected_model", "validation_rmse")}
                        for k, v in per.items()},
            "fd001_rmse": per["FD001"]["test_rmse_capped"],
            "worst_rmse": max(v["test_rmse_capped"] for v in per.values()),
            "min_coverage": min(v["interval_coverage_test"] for v in per.values()),
            "min_health_auroc": min(v["health_index_auroc_early_vs_late"] for v in per.values())}


def eval_safety() -> dict[str, Any]:
    p = DATA / "reports" / "safety_ntsb.json"
    if not p.exists():
        return {"available": False}
    m = json.loads(p.read_text())["metrics"]
    return {"available": True, "provenance": "public real dataset (NTSB), temporal split 2008-15 / 2016 / 2017-22",
            "test_auroc": m["test_auroc"], "test_pr_auc": m["test_pr_auc"], "test_prevalence": m["test_prevalence"],
            "test_f1": m["test_f1"], "ece": m["calibration"]["ece_prob"],
            "leakage_gap": round(m["leakage_check"]["unmasked_auroc"] - m["leakage_check"]["masked_auroc"], 4),
            "selective_prediction": m["selective_prediction"], "n_test": m["n_test"]}


def eval_security() -> dict[str, Any]:
    """RBAC + tenant isolation matrix: every expected allow/deny must hold."""
    from ..security import Forbidden, Principal

    cases = [("viewer", "read", True), ("viewer", "run", False), ("viewer", "approve", False), ("engineer", "run", True),
             ("engineer", "approve", False), ("approver", "approve", True), ("approver", "evaluate", False),
             ("admin", "evaluate", True), ("agent", "approve", False)]
    results = []
    for role, perm, expected in cases:
        try:
            Principal("agent" if role == "agent" else "user1", role, "t1").require(perm)
            allowed = True
        except Forbidden:
            allowed = False
        results.append({"role": role, "perm": perm, "expected": expected, "actual": allowed})
    store = services().cases
    c = store.open("tenant-a", "probe", "isolation probe", "user1")
    try:
        store.get(c.id, "tenant-b")
        isolated = False
    except KeyError:
        isolated = True
    passed = sum(r["expected"] == r["actual"] for r in results) + isolated
    return {"checks": results, "tenant_isolation": isolated, "pass_rate": round(passed / (len(results) + 1), 4)}


def eval_mro() -> dict[str, Any]:
    from ..modules.mro import run_mro

    svc = services()
    out = run_mro()
    truth = {(t["tail"], t["ad"]): t for t in svc.mro["truth"]}
    ok, rows, review_hits, review_need = 0, [], 0, 0
    for r in out["rows"]:
        t = truth[(r["tail"], r["ad"])]
        ok += r["status"] == t["status"]
        if t.get("needs_review"):
            review_need += 1
            review_hits += bool(r["needs_review"])
        rows.append({"tail": r["tail"], "ad": r["ad"], "pred": r["status"], "truth": t["status"], "evidence": (r["evidence"] or {}).get("kind")})
    return {"status_accuracy": round(ok / len(out["rows"]), 4), "implicit_review_recall": round(review_hits / max(1, review_need), 4),
            "n": len(out["rows"]), "rows": rows}


def eval_ap() -> dict[str, Any]:
    from ..modules.ap_match import run_ap

    svc = services()
    out = run_ap()
    truth = {i["id"]: i["truth"] for i in svc.ap["invoices"]}
    conf: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    ok = 0
    for r in out["invoices"]:
        t = truth[r["id"]]
        conf[t][r["status"]] += 1
        ok += r["status"] == t
    false_match = sum(v for t, row in conf.items() if t != "match" for p, v in row.items() if p == "match")
    return {"accuracy": round(ok / len(out["invoices"]), 4), "false_auto_match": false_match,
            "touchless_rate": out["touchless_rate"], "confusion": {k: dict(v) for k, v in conf.items()}}


def eval_pricing() -> dict[str, Any]:
    from ..modules.pricing import get_price_model

    return get_price_model().metrics


def run_all(write: bool = True) -> dict[str, Any]:
    svc = services()
    from ..cases import CaseStore

    svc.cases = CaseStore(":memory:")  # evaluation cases never pollute the durable workflow state
    svc.runtime.tracking_paused = True  # nor the integration audit log / Sheets tracking
    tracer.reset()
    t0 = time.time()
    report: dict[str, Any] = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "version": "1.0.0"}
    report["extraction"] = eval_extraction(svc.golden_sheets)
    report["retrieval"] = eval_retrieval()
    report["grounding"] = eval_grounding()
    report["change"] = eval_change()
    report["pdm"] = eval_pdm()
    report["safety"] = eval_safety()
    report["security"] = eval_security()
    report["mro"] = eval_mro()
    report["ap"] = eval_ap()
    rows = [json.loads(l) for l in (DATA / "golden" / "judge_labels.jsonl").read_text().splitlines() if l]
    report["judge"] = run_judge_harness(rows)
    report["pricing"] = eval_pricing()
    report["latency"] = tracer.latency_report()
    report["counters"] = dict(tracer.counters)
    report["llm_cache"] = {"hits": __import__("aeronexus.llm", fromlist=["get_router"]).get_router().cache.hits}
    conf = report["extraction"]["confidence_values"]
    report["drift"] = {"confidence_psi_vs_first_half": psi(conf[: len(conf) // 2], conf[len(conf) // 2:])}
    report["provenance"] = {"extraction": "synthetic golden drawings", "retrieval": "authored KB", "change": "synthetic revision pairs",
                            "mro": "synthetic logbooks", "ap": "synthetic invoices", "judge": "synthetic human labels",
                            "pdm": report["pdm"].get("provenance", "unavailable"), "safety": report["safety"].get("provenance", "unavailable")}
    report["gate"] = gate(report)
    report["duration_s"] = round(time.time() - t0, 2)
    svc.runtime.tracking_paused = False
    if write:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / "latest.json").write_text(json.dumps(report, indent=1, default=str))
    return report


def _get(d: dict[str, Any], path: str) -> Any:
    for k in path.split("."):
        d = d[k]
    return d


def gate(report: dict[str, Any]) -> dict[str, Any]:
    th = json.loads(THRESHOLDS.read_text())
    checks = []
    for path, rule in th.items():
        suite = report.get(path.split(".")[0], {})
        if isinstance(suite, dict) and suite.get("available") is False:
            checks.append({"metric": path, "value": None, "op": rule["op"], "target": rule["value"], "passed": True, "skipped": True,
                           "why": rule.get("why", "") + " (skipped: public data/model not present — run fetch + train)"})
            continue
        val = _get(report, path)
        op, target = rule["op"], rule["value"]
        passed = {"ge": val >= target, "le": val <= target, "eq": val == target}[op]
        checks.append({"metric": path, "value": val, "op": op, "target": target, "passed": bool(passed), "why": rule.get("why", "")})
    return {"passed": all(c["passed"] for c in checks), "checks": checks}


def main() -> int:
    rep = run_all()
    g = rep["gate"]
    for c in g["checks"]:
        print(f"{'PASS' if c['passed'] else 'FAIL'}  {c['metric']:<45} {c['value']!s:>10} {c['op']} {c['target']}")
    print("GATE:", "PASSED" if g["passed"] else "FAILED")
    if "--no-track" not in sys.argv:  # history + reproducible report + MLflow run
        from ..mlops import record_eval

        entry = record_eval(rep, "cli")
        reg = entry.get("regression") or {}
        print(f"Tracked as {entry['run_id']} (MLflow {entry['mlflow_run_id']}); "
              f"regressed vs previous: {reg.get('regressed') or 'none'}")
    return 0 if g["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
