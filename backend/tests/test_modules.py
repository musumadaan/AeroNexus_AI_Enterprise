from aeronexus.extraction.document import Sheet
from aeronexus.modules.ap_match import run_ap
from aeronexus.modules.mro import run_mro
from aeronexus.modules.quote import run_quote, thickness_mm
from aeronexus.services import services


def test_thickness_conversion():
    assert thickness_mm("10 ga") == 3.5 and thickness_mm("6 mm") == 6.0 and thickness_mm("0.25 in") == 6.35


def test_quote_clean_sheet_proposes_writes_with_price_interval():
    svc = services()
    s = next(s for s in svc.golden_sheets
             if not any(v for v in s.meta["difficulty"].values())
             and s.meta["truth"]["material"] in {"2024-T3", "7075-T6", "6061-T6", "ASTM A36"}
             and not any(i["kind"] == "inconsistent" for i in run_quote(s)["issues"]))
    out = run_quote(s)
    p = out["price"]
    assert p and p["low"] < p["unit_price"] < p["high"]
    tools = {svc.runtime.queue[x["approval_id"]].tool for x in out["proposals"] if "approval_id" in x}
    assert {"erp.create_bom", "erp.create_quote"} <= tools
    assert out["rationale"]["groundedness"] >= 0.9


def test_quote_inconsistent_coating_triggers_rfi_not_quote():
    svc = services()
    s = next(s for s in svc.golden_sheets if s.meta["truth"]["material"] in {"6061-T6", "2024-T3", "7075-T6", "TI-6AL-4V", "SS 304"}
             and ("GALV" in (s.meta["truth"]["coating"] or "") or (s.meta["truth"]["material"] in {"TI-6AL-4V", "SS 304"}
                                                                   and "ANODIZE" in (s.meta["truth"]["coating"] or "")))
             and not s.meta["difficulty"]["injection"])
    out = run_quote(s)
    assert any(i["kind"] == "inconsistent" for i in out["issues"])
    tools = {svc.runtime.queue[x["approval_id"]].tool for x in out["proposals"]}
    assert tools == {"comms.send_email"}


def test_quote_injection_sheet_sends_nothing():
    s = next(s for s in services().golden_sheets if s.meta["difficulty"]["injection"])
    out = run_quote(s)
    assert any(i["kind"] == "security" for i in out["issues"]) and out["proposals"] == []


def test_mro_statuses_and_explanations_are_grounded():
    out = run_mro()
    assert out["rows"] and "Decision support only" in out["disclaimer"]
    for r in out["rows"]:
        assert r["explanation"]["groundedness"] == 1.0
        if r["evidence"] and r["evidence"]["kind"] == "component_action_match":
            assert r["needs_review"]  # never auto-trust implicit evidence


def test_ap_never_auto_matches_exceptions():
    out = run_ap()
    truth = {i["id"]: i["truth"] for i in services().ap["invoices"]}
    for r in out["invoices"]:
        if truth[r["id"]] != "match":
            assert r["action"] == "route_to_ap_review"
