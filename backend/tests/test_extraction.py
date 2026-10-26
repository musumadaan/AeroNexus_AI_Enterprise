import json

from aeronexus.extraction.document import Sheet
from aeronexus.extraction.pipeline import ExtractionPipeline
from aeronexus.llm import MockLLM
from aeronexus.observability import tracer
from aeronexus.services import services


def sheet_by_flag(flag):
    return next(s for s in services().golden_sheets if s.meta["difficulty"].get(flag))


def test_clean_sheet_all_fields_correct_with_provenance():
    s = next(s for s in services().golden_sheets if not any(v for k, v in s.meta["difficulty"].items()))
    res = services().pipeline.run(s)
    for name, truth in s.meta["truth"].items():
        r = res.fields[name]
        if truth is None:
            continue
        assert r.value is not None and r.value.lower() == truth.lower(), (name, r.value, truth)
        assert r.token_ids, f"{name} has no provenance tokens"
    assert {r["label"] for r in res.regions} >= {"title_block", "notes"}


def test_title_comes_from_llm_cascade_and_is_verbatim():
    s = services().golden_sheets[0]
    res = services().pipeline.run(s)
    t = res.fields["title"]
    assert t.source == "llm" and t.value == s.meta["truth"]["title"]
    assert tracer.counters["llm.escalate.low_conf"] >= 1  # small model unsure → large model


def test_injection_disables_llm_fallback():
    s = sheet_by_flag("injection")
    res = services().pipeline.run(s)
    assert res.warnings and res.llm_calls == 0
    assert res.fields["title"].status == "missing"


def test_conflicting_thickness_goes_to_review():
    s = sheet_by_flag("conflict")
    r = services().pipeline.run(s).fields["thickness"]
    assert r.status == "needs_review"
    assert r.alternatives or "unreadable" in r.note  # two readable values, or the title-block value was destroyed


def test_unreadable_title_value_never_auto_accepts_a_note_value():
    from aeronexus.extraction.document import Sheet

    d = services().golden_sheets[0].to_dict()
    toks = d["tokens"]
    i = next(k for k, t in enumerate(toks) if t["text"] in {"THK"})
    toks[i + 1]["text"] = "I0"  # OCR damage on the title-block value
    nx = max(int(t["id"][1:]) for t in toks) + 1
    toks.append({"id": f"t{nx}", "text": "T=11", "bbox": [60, 640, 30, 9]})
    toks.append({"id": f"t{nx + 1}", "text": "mm", "bbox": [95, 640, 12, 9]})
    r = ExtractionPipeline().run(Sheet.from_dict(d)).fields["thickness"]
    assert r.value == "11 mm" and r.status == "needs_review" and "unreadable" in r.note


def test_moved_title_block_lowers_confidence():
    s = sheet_by_flag("moved_title")
    res = services().pipeline.run(s)
    assert res.fields["drawing_number"].status != "auto_accepted"


def test_hallucination_guard_blocks_invented_values():
    original = MockLLM._handlers["extract_fields"]
    MockLLM._handlers["extract_fields"] = lambda req, model: json.dumps(
        {"fields": {"title": "TOTALLY INVENTED PART"}, "confidence": 0.99})
    try:
        res = ExtractionPipeline().run(services().golden_sheets[0])
        assert res.fields["title"].value is None
        assert tracer.counters["extract.llm.hallucination_blocked"] == 1
    finally:
        MockLLM._handlers["extract_fields"] = original


def test_quantity_cross_check_against_bom():
    s = Sheet.from_dict(services().golden_sheets[0].to_dict())
    for t in s.tokens:  # corrupt only the title-block QTY, leave the BOM row intact
        if t.text == s.meta.get("truth", {}).get("quantity") and t.bbox[0] > 550 and t.bbox[1] > 500:
            t.text = "99"
            break
    s.meta = services().golden_sheets[0].meta
    q = ExtractionPipeline().run(s).fields["quantity"]
    assert q.value == "99" and q.status == "needs_review" and "BOM" in q.note
