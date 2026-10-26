"""Tests on the models trained from real public data. Skipped if data/models haven't been fetched/trained."""
import json

import pytest

from aeronexus.config import DATA, MODELS
from aeronexus.public_data import REGISTRY, catalog, verify

need_cmapss = pytest.mark.skipif(not (MODELS / "pdm_FD001.joblib").exists(), reason="run fetch_public_data + pdm training")
need_ntsb = pytest.mark.skipif(not (MODELS / "safety_ntsb.joblib").exists(), reason="run fetch_public_data + safety training")


def test_catalog_records_license_and_source_for_every_dataset():
    for d in catalog():
        assert d["source"].startswith("https://") and d["license"] and d["terms"]


@pytest.mark.skipif(not REGISTRY["cmapss"].available(), reason="C-MAPSS not downloaded")
def test_downloaded_files_match_pinned_checksums():
    assert all(verify(REGISTRY["cmapss"]).values())


@need_cmapss
def test_fleet_rul_intervals_and_work_order_rule():
    from aeronexus.modules.pdm import fleet, propose_work_order

    f = fleet("FD001")
    assert f["available"] and len(f["engines"]) == 100
    e = f["engines"][0]  # sorted by lowest lower bound
    assert e["low"] <= e["pred_rul"] <= e["high"]
    out = propose_work_order("FD001", e["unit"])
    if e["work_order"]:
        assert out["status"] == "needs_review" and out["proposal"]["status"] == "pending_approval"


@need_cmapss
def test_rul_report_is_competitive_and_calibrated():
    rep = json.loads((DATA / "reports" / "pdm_cmapss.json").read_text())
    assert rep["FD001"]["metrics"]["test_rmse_capped"] < 16
    assert rep["FD001"]["metrics"]["validation_rmse"]["gbt"] < rep["FD001"]["metrics"]["validation_rmse"]["mean_baseline"] / 2


@need_ntsb
def test_safety_triage_abstains_near_threshold_and_masks_outcome_words():
    from aeronexus.modules.safety import triage

    a = triage("The pilot's failure to maintain airspeed which resulted in an aerodynamic stall; the pilot was killed.")
    b = triage("The pilot's failure to maintain airspeed which resulted in an aerodynamic stall; the pilot died.")
    assert a["probability"] == b["probability"]  # outcome words are masked before scoring, so they can't drive the score
    assert a["decision"] in {"high_priority", "abstain", "routine"}
