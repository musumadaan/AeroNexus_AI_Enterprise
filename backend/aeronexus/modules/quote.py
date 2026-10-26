"""Module 1 (flagship): Drawing → validated BOM → priced quote → ERP/CRM drafts, human-approved.

Flow
  1. Extraction cascade (layout → regex → NER → LLM) with per-field confidence + provenance.
  2. Consistency checks: knowledge-graph rules (e.g. galvanizing on aluminum), tolerance policy,
     missing required fields (quoting policy) — each check cites the KB passage it relies on.
  3. Catalog match in the ERP (READ tool).
  4. Price model + split-conformal 90% interval.
  5. Agent proposals: BOM, quote and CRM opportunity (WRITE) or an RFI email (EXTERNAL) — all
     land in the approval queue; nothing leaves the building without a named human.
"""
from __future__ import annotations

import re
from typing import Any

from ..calc import calculate, parse_length, rule
from ..extraction.document import Sheet
from ..generation import grounded_answer
from ..observability import tracer
from ..services import services
from .pricing import get_price_model

REQUIRED = ["drawing_number", "revision", "material", "thickness", "quantity"]


def thickness_mm(value: str | None) -> float | None:
    return parse_length(value)  # deterministic unit engine (gauge tables, in → mm)


def run_quote(sheet: Sheet, customer_id: str | None = None, actor: str = "agent") -> dict[str, Any]:
    svc = services()
    with tracer.span("module.quote", sheet=sheet.id):
        ext = svc.pipeline.run(sheet)
        f = {k: v for k, v in ext.fields.items()}
        val = {k: (v.value if v.value is not None else None) for k, v in f.items()}
        issues: list[dict[str, Any]] = []

        # --- required fields (quoting policy) ---------------------------------------------------
        for name in REQUIRED:
            if val.get(name) is None:
                issues.append({"severity": "high", "field": name, "kind": "missing",
                               "message": f"{name.replace('_', ' ').title()} not found on drawing — RFI required."})
            elif f[name].status == "needs_review":
                issues.append({"severity": "medium", "field": name, "kind": "low_confidence",
                               "message": f"{name.replace('_', ' ').title()} = {val[name]} needs confirmation ({f[name].note or 'low confidence'})."})

        # --- knowledge-graph consistency: coating vs base material ------------------------------
        if val.get("material") and val.get("coating"):
            edges = svc.graph.neighbors(val["material"], depth=1)
            bad = [o for s, r, o in edges if r == "incompatible_coating" and o == val["coating"].upper()]
            if bad:
                issues.append({"severity": "high", "field": "coating", "kind": "inconsistent",
                               "message": f"{val['coating']} is not a valid finish for {val['material']} — drawing is internally inconsistent."})
        tol = val.get("tolerance")
        if tol and re.match(r"±\s*0?\.(1|2)\b", tol):
            issues.append({"severity": "medium", "field": "tolerance", "kind": "cost_driver",
                           "message": f"Tight general tolerance {tol} — estimator review (cost driver)."})
        for w in ext.warnings:
            issues.append({"severity": "high", "field": "document", "kind": "security", "message": w})

        # --- catalog + price --------------------------------------------------------------------
        t_mm = thickness_mm(val.get("thickness"))
        catalog = svc.runtime.call("erp.find_item", {"material": val.get("material") or "", "thickness": val.get("thickness") or ""},
                                   actor=actor)["result"]
        item = catalog[0] if catalog else None
        price = None
        assumptions: list[str] = []
        if item and t_mm and val.get("quantity"):
            length = float(re.match(r"[\d.]+", val["length"]).group(0)) if val.get("length") else 1200.0
            if not val.get("length"):
                assumptions.append("length not found — assumed 1200 mm")
            assumptions.append("plate width assumed 600 mm (not dimensioned on sheet)")
            coating = (val.get("coating") or "").upper()
            markups = rule("quote.coating_markup").params
            markup = next((m for k, m in markups.items() if k in coating), 0.0)
            _, mass_receipt = calculate("quote.plate_mass_kg", thickness_mm=t_mm, length_mm=length, width_mm=600.0,
                                        density_g_cc=item["density"])
            row = {"material_class": item["material_class"], "thickness_mm": t_mm, "length_mm": length, "width_mm": 600,
                   "qty": int(val["quantity"]), "coating_markup": markup, "n_holes": 4 if val.get("diameter") else 0,
                   "density": item["density"], "rate_per_kg": item["rate_per_kg"]}
            est = get_price_model().predict(row)
            limit = rule("quote.approval_limit").params["cad"]
            price = {"unit_price": est.unit_price, "low": est.low, "high": est.high, "coverage": est.coverage,
                     "total": est.total, "currency": "CAD", "features": row, "mass_kg": mass_receipt.output,
                     "receipts": [mass_receipt.to_dict()], "manager_approval_required": est.total > limit}

        # --- grounded rationale (RAG + verification) ------------------------------------------
        topics = [val.get("material") or "", val.get("coating") or ""]
        if any(i["kind"] == "inconsistent" for i in issues):
            topics.append("galvanizing incompatible aluminum stainless RFI")
        if any(i["kind"] == "missing" for i in issues):
            topics.append("required fields missing RFI before pricing")
        q = " ".join(t for t in topics if t) or "quote requirements"
        hits = svc.index.search(q, k=4)
        computed = {}
        if price:
            computed["calc:price"] = (f"The estimated unit price is {price['unit_price']} CAD with a 90 percent interval from "
                                      f"{price['low']} to {price['high']} CAD")
        rationale = grounded_answer(f"What should an estimator check for {q}?", hits, extra_sources=computed)

        # --- agent proposals --------------------------------------------------------------------
        account = svc.runtime.crm.accounts.get(customer_id or sheet.meta.get("customer", "ACC-001"))
        proposals = []
        blocking = [i for i in issues if i["severity"] == "high"]
        if blocking or price is None:
            missing = ", ".join(sorted({i["field"].replace("_", " ") for i in issues if i["severity"] == "high"})) or "pricing inputs"
            body = (f"Hello {account['name'] if account else ''},\n\nThank you for RFQ drawing {val.get('drawing_number') or sheet.id}. "
                    f"Before we can quote, please confirm: {missing}.")
            if account and not ext.warnings:
                proposals.append(svc.runtime.call("comms.send_email", {"to": account["email"], "subject": f"RFI – {val.get('drawing_number') or sheet.id}",
                                                                        "body": body}, actor=actor, reason="Blocking issues before quoting",
                                                  module="quote"))
        else:
            pn = val.get("part_number") or sheet.id
            lines = [{"code": item["code"], "description": item["description"], "qty": int(val["quantity"]), "rate": price["unit_price"]}]
            proposals.append(svc.runtime.call("erp.create_bom", {"part_number": pn, "lines": lines}, actor=actor,
                                              reason="Extracted BOM", module="quote"))
            proposals.append(svc.runtime.call("erp.create_quote", {"customer": account["id"] if account else "UNKNOWN", "part_number": pn,
                                                                   "amount": price["total"], "lines": lines},
                                              actor=actor, reason=f"Priced at {price['unit_price']} ± interval", module="quote"))
            if account:
                proposals.append(svc.runtime.call("crm.create_opportunity", {"account_id": account["id"], "title": f"{pn} x{val['quantity']}",
                                                                             "amount": price["total"]}, actor=actor, module="quote"))
        out = {
            "sheet": sheet.to_dict(), "extraction": ext.to_dict(), "issues": issues, "catalog_match": item, "price": price,
            "assumptions": assumptions, "rationale": {"text": rationale.text, "decision": rationale.report.decision,
                                                      "groundedness": rationale.report.groundedness, "sources": rationale.sources,
                                                      "claims": [c.__dict__ for c in rationale.report.claims]},
            "proposals": proposals, "account": account,
            "summary": {"auto": sum(v.status == "auto_accepted" for v in f.values()),
                        "review": sum(v.status == "needs_review" for v in f.values()),
                        "missing": sum(v.status == "missing" for v in f.values()), "blocking": len(blocking)},
        }
        svc.results[f"quote:{sheet.id}"] = out
        return out
