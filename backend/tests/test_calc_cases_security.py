import datetime as dt

import pytest

from aeronexus.calc import calculate, parse_length, rule, to_mm, verify_receipt, UnitError, RuleNotFound
from aeronexus.cases import CaseStore, TransitionError
from aeronexus.security import Forbidden, Principal, principal_from_headers


def test_units_and_gauges():
    assert parse_length("10 ga") == 3.5 and parse_length("0.25 in") == 6.35 and parse_length("3,5 mm") == 3.5
    assert parse_length("12O mm") is None
    with pytest.raises(UnitError):
        to_mm(3, "furlong")


def test_effective_dated_rules():
    assert rule("ap.price_tolerance", dt.date(2026, 3, 1)).params["pct"] == 0.02
    assert rule("ap.price_tolerance", dt.date(2026, 8, 1)).params["pct"] == 0.015
    with pytest.raises(RuleNotFound):
        rule("ap.price_tolerance", dt.date(2020, 1, 1))


def test_same_invoice_line_judged_by_policy_in_force_on_its_date():
    old, _ = calculate("ap.price_variance", as_of=dt.date(2026, 3, 1), billed=101.8, po_price=100.0)
    new, _ = calculate("ap.price_variance", as_of=dt.date(2026, 8, 1), billed=101.8, po_price=100.0)
    assert old["exception"] is False and new["exception"] is True


def test_receipt_is_reproducible_and_tamper_evident():
    _, rec = calculate("mro.next_due", last_compliance_hours=2500.0, interval_hours=500.0, current_hours=2980.0)
    r = rec.to_dict()
    assert r["output"]["status"] == "due_soon" and verify_receipt(r)
    tampered = dict(r, output={**r["output"], "status": "compliant"})
    assert not verify_receipt(tampered)


def test_case_state_machine_and_replay():
    s = CaseStore(":memory:")
    c = s.open("t1", "change_order", "x", "eng")
    c = s.set_status(c.id, "t1", "needs_review", "agent")
    with pytest.raises(TransitionError):
        s.set_status(c.id, "t1", "executed", "agent")  # cannot skip approval
    s.set_status(c.id, "t1", "verified", "eng")
    s.set_status(c.id, "t1", "approved", "boss")
    assert s.replay(c.id).status == "approved"


def test_failing_step_retries_then_blocks_and_resumes():
    s = CaseStore(":memory:")
    calls = {"n": 0}

    def flaky(_):
        calls["n"] += 1
        if calls["n"] < 5:
            raise RuntimeError("ERP timeout")
        return {"ok": True}

    c = s.open("t1", "change_order", "x", "eng")
    c = s.run_steps(c, [("a", lambda d: 1), ("b", flaky)], "agent", retries=2, backoff_s=0)
    assert c.status == "blocked" and c.steps_done == ["a"]
    c = s.set_status(c.id, "t1", "needs_review", "eng", "retry after ERP recovered")
    c = s.run_steps(c, [("a", lambda d: 1 / 0), ("b", flaky)], "agent", retries=2, backoff_s=0)  # a is NOT re-run
    assert c.steps_done == ["a", "b"]


@pytest.mark.parametrize("role,perm,ok", [("viewer", "run", False), ("engineer", "approve", False), ("approver", "approve", True),
                                          ("admin", "evaluate", True), ("approver", "evaluate", False)])
def test_rbac_matrix(role, perm, ok):
    p = Principal("u", role, "t")
    if ok:
        p.require(perm)
    else:
        with pytest.raises(Forbidden):
            p.require(perm)


def test_headers_cannot_claim_agent_or_unknown_roles():
    assert principal_from_headers({"x-role": "agent", "x-user": "x"}).role == "viewer"
    assert principal_from_headers({"x-role": "root"}).role == "viewer"
    with pytest.raises(Forbidden):
        Principal("", "approver", "t").require("approve")  # anonymous approver is not allowed
