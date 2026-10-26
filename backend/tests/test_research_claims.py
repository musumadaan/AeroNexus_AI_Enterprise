import pytest

from aeronexus.research import check_claim, load_claims, verified_claims


@pytest.mark.parametrize("claim", load_claims(), ids=lambda c: c["id"])
def test_every_claim_is_supported_by_its_source_excerpt(claim):
    res = check_claim(claim)
    assert res["verified"], res["problems"]


def test_inflated_number_is_rejected():
    c = dict(load_claims()[1])
    c["claim"] = c["claim"].replace("7.57", "17.57")
    assert not check_claim(c)["verified"]


def test_every_module_has_research_backing():
    mods = {c["module"] for c in verified_claims()}
    assert {"quote", "change", "mro", "ap", "evals", "rag", "datasets"} <= mods
