"""Module 3: Aviation maintenance (MRO) airworthiness-directive compliance checker.

DECISION SUPPORT ONLY. Output is a review worklist for an authorized person, never an
airworthiness determination. Data in this repo is synthetic (realistic formats, fictional ADs).

Pipeline per aircraft × applicable AD:
  1. Normalise logbook text (abbreviation expansion, e.g. "c/w" → "complied with", "rplcd").
  2. Evidence search:
       strong  — regex finds the AD number in an entry (conf 0.95)
       weak    — NER finds the AD's component AND required action in the same entry (conf 0.65,
                 always sent to human review — the logbook never quoted the AD)
  3. Due computation for recurring ADs (hours), status: compliant / due_soon / overdue / no_record.
  4. Grounded explanation that cites the AD text and the logbook entry it relied on.
Research basis: maintenance-record entry errors are a documented error category (CLM-MRO-1) and
logbook text is non-standard and abbreviation-heavy (CLM-MRO-2).
"""
from __future__ import annotations

import re
from typing import Any

from ..calc import calculate
from ..extraction.ner import get_ner
from ..extraction.regex_rules import run_rules
from ..grounding import verify
from ..observability import tracer
from ..services import services

ABBR = {r"\bc/w\b": "complied with", r"\brplcd\b": "replaced", r"\binsp\b": "inspected", r"\bIAW\b": "in accordance with",
        r"\bops ck\b": "operational check", r"\bL/H\b": "left", r"\bR/H\b": "right"}


def normalise(text: str) -> str:
    for pat, rep in ABBR.items():
        text = re.sub(pat, rep, text, flags=re.I)
    return text


def find_evidence(ad: dict[str, Any], logs: list[dict[str, Any]]) -> dict[str, Any] | None:
    ner = get_ner()
    best = None
    for i, e in enumerate(logs):
        ad_nums = {m.value for m in run_rules(e["text"], ["ad_number"])}
        if ad["ad"] in ad_nums:
            cand = {"entry_index": i, "entry": e, "kind": "explicit_ad_reference", "confidence": 0.95}
        else:
            ents = ner(normalise(e["text"]))
            comps = {x.text.lower() for x in ents if x.label == "COMPONENT"}
            acts = {x.text.lower() for x in ents if x.label == "ACTION"}
            if ad["component"] in comps and ad["action"] in acts:
                cand = {"entry_index": i, "entry": e, "kind": "component_action_match", "confidence": 0.65}
            else:
                continue
        if best is None or (cand["entry"]["hours"], cand["confidence"]) > (best["entry"]["hours"], best["confidence"]):
            best = cand
    return best


def check_aircraft(ac: dict[str, Any], ads: list[dict[str, Any]], logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mine = sorted([l for l in logs if l["tail"] == ac["tail"]], key=lambda l: l["hours"])
    current = max(l["hours"] for l in mine)
    rows = []
    for ad in [a for a in ads if a["model"] == ac["model"]]:
        ev = find_evidence(ad, mine)
        rh = ad["recurring_hours"]
        row: dict[str, Any] = {"tail": ac["tail"], "model": ac["model"], "ad": ad["ad"], "component": ad["component"],
                               "current_hours": current, "recurring_hours": rh, "evidence": ev}
        if ev is None:
            row.update(status="no_record", confidence=0.9, next_due=None, remaining=None, needs_review=True)
        elif not rh:
            row.update(status="compliant", confidence=ev["confidence"], next_due=None, remaining=None,
                       needs_review=ev["confidence"] < 0.9)
        else:
            out, receipt = calculate("mro.next_due", last_compliance_hours=ev["entry"]["hours"], interval_hours=float(rh),
                                     current_hours=current)
            status = out["status"]
            row.update(status=status, confidence=ev["confidence"], next_due=out["next_due"], remaining=out["remaining"],
                       needs_review=ev["confidence"] < 0.9 or status != "compliant", receipt=receipt.to_dict())
        rows.append(row)
    return rows


def explain(row: dict[str, Any], ad: dict[str, Any]) -> dict[str, Any]:
    """Template explanation; each sentence restates (and cites) a source, so verification is strict."""
    src = {f"ad:{ad['ad']}": ad["text"]}
    one = lambda t: re.sub(r"\.\s+", "; ", t.strip()).rstrip(".")  # keep each cited claim a single sentence
    requirement = one(ad["text"].split(":", 1)[-1])
    sents = [f"Directive {ad['ad']} requires: {requirement} [c:ad:{ad['ad']}]."]
    if row["evidence"]:
        e = row["evidence"]["entry"]
        key = f"log:{row['tail']}:{e['hours']}"
        src[key] = f"{e['date']} at {e['hours']} hours: {e['text']}"
        sents.append(f"Latest matching logbook entry ({e['hours']} hours): {one(e['text'])} [c:{key}].")
    else:
        key = f"calc:{row['tail']}:{ad['ad']}:none"
        src[key] = f"No logbook entry for {row['tail']} references directive {ad['ad']} or its required action"
        sents.append(f"No logbook entry for {row['tail']} references directive {ad['ad']} or its required action [c:{key}].")
    if row.get("next_due") is not None:
        calc = f"calc:{row['tail']}:{ad['ad']}"
        src[calc] = (f"Next due at {row['next_due']} hours; aircraft is at {row['current_hours']} hours; "
                     f"remaining {row['remaining']} hours")
        sents.append(f"Next due at {row['next_due']} hours with {row['remaining']} hours remaining [c:{calc}].")
    rep = verify(" ".join(sents), src)
    return {"text": rep.final_text, "groundedness": rep.groundedness, "decision": rep.decision, "sources": src}


def run_mro(tail: str | None = None) -> dict[str, Any]:
    svc = services()
    d = svc.mro
    with tracer.span("module.mro"):
        rows = []
        for ac in d["aircraft"]:
            if tail and ac["tail"] != tail:
                continue
            for row in check_aircraft(ac, d["ads"], d["logs"]):
                ad = next(a for a in d["ads"] if a["ad"] == row["ad"])
                row["explanation"] = explain(row, ad)
                rows.append(row)
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        out = {"rows": rows, "counts": counts, "review_queue": [r for r in rows if r["needs_review"]],
               "disclaimer": "Decision support only. Synthetic data. Airworthiness determinations are made by authorized personnel."}
        svc.results["mro"] = out
        return out
