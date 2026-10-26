"""Aviation safety analytics at runtime: triage a narrative, find similar NTSB events, show trends.

* Triage: calibrated probability that a narrative describes a fatal-outcome event, with an
  abstain band around the decision threshold (selective prediction). Output is a *review
  priority*, never a finding of cause.
* Similar events: hybrid retrieval (BM25 + dense + RRF + MMR) over 17,485 NTSB probable-cause
  statements; each hit shows its event id, year, aircraft and damage so a reviewer can open the
  original docket.
* Trends: cause themes (NMF topics) and deterministic tags per year, from the trained report.
* ASRS / FAA SDR: if you drop exported CSVs into data/public/asrs or data/public/faa_sdr they are
  indexed for retrieval as well (text column auto-detected); counts are labelled as unverified.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..config import DATA, MODELS
from ..public_data import REGISTRY
from ..rag import Chunk, HybridIndex
from ..observability import tracer

ABSTAIN_BAND = 0.1


@lru_cache(maxsize=1)
def report() -> dict[str, Any]:
    p = DATA / "reports" / "safety_ntsb.json"
    return json.loads(p.read_text()) if p.exists() else {}


@lru_cache(maxsize=1)
def model() -> dict[str, Any] | None:
    p = MODELS / "safety_ntsb.joblib"
    if not p.exists():
        return None
    import joblib

    return joblib.load(p)


def _extra_csv_chunks(key: str, limit: int = 20000) -> list[Chunk]:
    import pandas as pd

    out: list[Chunk] = []
    for path in sorted(Path(REGISTRY[key].folder).glob("*.csv")) if REGISTRY[key].folder.exists() else []:
        try:
            df = pd.read_csv(path, low_memory=False, encoding="latin-1")
        except Exception:
            continue
        text_col = max((c for c in df.columns if df[c].dtype == object), key=lambda c: df[c].astype(str).str.len().mean(),
                       default=None)
        if text_col is None:
            continue
        for i, t in enumerate(df[text_col].dropna().astype(str).head(limit)):
            if len(t) > 40:
                out.append(Chunk(f"{key}:{path.stem}:{i}", key, t[:1500], {"title": REGISTRY[key].name, "section": path.stem,
                                                                          "source": key, "verified": False}))
    return out


@lru_cache(maxsize=1)
def index() -> HybridIndex | None:
    if not REGISTRY["ntsb"].available():
        return None
    from ..safety.ntsb import load_events

    with tracer.span("safety.index_build"):
        df = load_events().astype(object).where(lambda d: d.notna(), None)  # NaN is not valid JSON
        chunks = [Chunk(f"ntsb:{r['Event.Id']}", "ntsb", r["text"],
                        {"title": f"NTSB {r['Accident.Number']}", "section": f"{r['year']} {r.get('Make') or ''} {r.get('Model') or ''}",
                         "year": int(r["year"]), "damage": r.get("Aircraft.damage"), "fatal": int(r["fatal"]),
                         "make": str(r.get("Make") or ""), "model": str(r.get("Model") or ""), "source": "ntsb"})
                  for _, r in df.iterrows()]
        chunks += _extra_csv_chunks("asrs") + _extra_csv_chunks("faa_sdr")
        return HybridIndex(chunks)


def triage(text: str) -> dict[str, Any]:
    from ..safety.ntsb import OUTCOME_WORDS

    m = model()
    if m is None:
        return {"available": False, "message": "Train first: python -m aeronexus.safety.ntsb"}
    masked = OUTCOME_WORDS.sub("[outcome]", text)
    p = float(m["model"].predict_proba([masked])[0, 1])
    th = float(m["threshold"])
    if abs(p - th) < ABSTAIN_BAND:
        decision = "abstain"
    else:
        decision = "high_priority" if p >= th else "routine"
    return {"available": True, "probability": round(p, 4), "threshold": th, "decision": decision,
            "abstain_band": ABSTAIN_BAND, "note": "Review priority only — not a determination of cause or outcome."}


def similar(text: str, k: int = 5) -> list[dict[str, Any]]:
    idx = index()
    if idx is None:
        return []
    return [{"id": h.chunk.id, "text": h.chunk.text, "score": h.score, **{kk: h.chunk.meta.get(kk) for kk in
             ("title", "section", "year", "damage", "fatal", "source", "verified")}} for h in idx.search(text, k=k)]


def overview() -> dict[str, Any]:
    r = report()
    if not r:
        return {"available": False, "message": "Train first: python -m aeronexus.safety.ntsb"}
    return {"available": True, **r}
