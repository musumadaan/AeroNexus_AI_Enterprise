import pytest

from aeronexus.calc import verify_receipt
from aeronexus.cases import TransitionError
from aeronexus.extraction.document import Sheet
from aeronexus.modules.change_order import approve_change_order, open_change_order
from aeronexus.security import Forbidden, Principal
from aeronexus.services import services

APPROVER = Principal("m.madaan", "approver", "demo")


def pair(kind):
    p = next(p for p in services().change_pairs if kind in p["truth"]["changed"])
    return Sheet.from_dict(p["before"]), Sheet.from_dict(p["after"]), p["part_number"]


def test_thickness_change_is_high_with_cost_lead_time_and_receipts():
    out = open_change_order(*pair("thickness"))
    d = out["data"]
    assert d["classify"]["severity"] in {"high", "critical"} and "thickness" in d["impact"]["changed_fields"]
    ch = next(c for c in d["diff"]["changes"] if c["field"] == "thickness")
    assert ch["delta_mm"] == 1.0 and abs(ch["delta_pct"] - 0.2) < 1e-9          # unit engine, not string compare
    assert d["cost"]["available"] and d["cost"]["lead_time_days"] >= 5
    assert all(verify_receipt(r) for r in d["cost"]["receipts"] + [d["classify"]["receipt"]])
    assert "## Calculation receipts" in out["report"]["markdown"] and out["report"]["html"].startswith("<!doctype html>")


def test_material_change_pulls_new_supplier_lead_time():
    d = open_change_order(*pair("material"))["data"]
    assert d["impact"]["material_change"] and d["impact"]["new_supplier"]
    assert d["cost"]["lead_time_days"] >= d["impact"]["new_supplier"]["lead_days"]


def test_cosmetic_change_is_low_and_nothing_sent_before_approval():
    out = open_change_order(*pair("scale"))
    assert out["data"]["classify"]["severity"] == "low"
    assert not services().runtime.outbox and not services().runtime.erp.ecos


def test_approval_executes_eco_and_notices_and_replays():
    out = open_change_order(*pair("thickness"))
    with pytest.raises(Forbidden):
        approve_change_order(out["id"], Principal("eng", "engineer", "demo"), True)
    done = approve_change_order(out["id"], APPROVER, True, "checked against drawing")
    assert done["status"] == "executed" and services().runtime.erp.ecos
    statuses = [h["summary"] for h in done["history"] if h["type"] == "status"]
    assert any("approved" in s for s in statuses) and any("executed" in s for s in statuses)
    store = services().cases
    assert store.replay(out["id"]).status == "executed"


def test_rejected_case_cannot_be_approved_later():
    out = open_change_order(*pair("quantity"))
    approve_change_order(out["id"], APPROVER, False, "not now")
    with pytest.raises(TransitionError):
        services().cases.set_status(out["id"], "demo", "approved", "m.madaan")


def test_other_tenant_cannot_see_case():
    out = open_change_order(*pair("length"))
    with pytest.raises(KeyError):
        services().cases.get(out["id"], "another-tenant")
