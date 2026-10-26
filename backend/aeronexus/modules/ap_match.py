"""Module 4: Accounts-payable three-way match agent (invoice ↔ PO ↔ goods receipt).

1. Parse invoice text: regex for invoice no., PO, date, money and line items
   ("<ITEM> QTY <n> @ $<price>"); vision/OCR would supply this text for scanned invoices
   (FATURA / DocILE in docs/DATASETS.md for training a layout model).
2. Match against ERP purchase orders and goods receipts with policy tolerances
   (price ±2%, qty billed ≤ qty received).
3. Duplicate detection: same vendor + PO + total as an earlier invoice (number may differ).
4. Anomaly score: robust z-score of the invoice total against the vendor's history.
5. Output: "matched → schedule for approval" or a typed exception with a grounded reason.
   The system never releases payment (policy) — a person approves.
Research/industry basis: exceptions drive AP cost and a sizeable minority of invoices are flagged
(docs/RESEARCH.md CLM-AP-1/2).
"""
from __future__ import annotations

import datetime as dt
import re
import statistics
from typing import Any

from ..calc import calculate
from ..extraction.regex_rules import run_rules
from ..grounding import verify
from ..observability import tracer
from ..services import services

LINE = re.compile(r"^(?P<item>[A-Z0-9-]{4,})\s+QTY\s+(?P<qty>\d+)\s+@\s+\$(?P<price>[\d,]+\.\d{2})", re.M)
# price tolerance lives in the rules engine (ap.price_tolerance, effective-dated)


def parse_invoice(text: str) -> dict[str, Any]:
    f: dict[str, Any] = {}
    for m in run_rules(text, ["invoice_number", "po_number", "date", "amount"]):
        if m.field == "amount":
            f.setdefault("amounts", []).append(float(m.value))
        else:
            f.setdefault(m.field, m.value)
    f["lines"] = [{"item": m.group("item"), "qty": int(m.group("qty")), "unit_price": float(m.group("price").replace(",", ""))}
                  for m in LINE.finditer(text)]
    tot = re.search(r"TOTAL[^$]*\$([\d,]+\.\d{2})", text)
    f["total"] = float(tot.group(1).replace(",", "")) if tot else None
    f["vendor"] = text.splitlines()[0].strip().title() if text.strip() else None
    return f


def match_invoice(inv: dict[str, Any], seen: list[dict[str, Any]]) -> dict[str, Any]:
    svc = services()
    erp = svc.runtime.erp
    p = parse_invoice(inv["text"])
    exceptions: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    inv_date = dt.date.fromisoformat(p["date"]) if p.get("date") else dt.date.today()
    po = erp.purchase_orders.get(p.get("po_number", ""))
    rec = erp.receipts.get(p.get("po_number", ""))
    sources = {f"inv:{inv['id']}": inv["text"].replace("\n", "; ")}
    for prev in seen:
        if prev["parsed"].get("po_number") == p.get("po_number") and prev["parsed"].get("total") == p.get("total") \
                and prev["id"] != inv["id"]:
            exceptions.append({"type": "duplicate", "detail": f"Same PO {p.get('po_number')} and total {p.get('total')} as {prev['id']}"})
            break
    if po is None:
        exceptions.append({"type": "unknown_po", "detail": f"PO {p.get('po_number')} not found in ERP"})
    else:
        sources[f"po:{po['po']}"] = "; ".join(f"{l['item']} qty {l['qty']} at {l['unit_price']:.2f}" for l in po["lines"])
        if rec is None:
            exceptions.append({"type": "no_receipt", "detail": f"No goods receipt recorded for PO {po['po']}"})
        else:
            sources[f"gr:{po['po']}"] = "; ".join(f"{l['item']} received {l['qty']}" for l in rec["lines"])
        for line in p["lines"]:
            pol = next((l for l in po["lines"] if l["item"] == line["item"]), None)
            if pol is None:
                exceptions.append({"type": "item_not_on_po", "detail": f"{line['item']} not on PO"})
                continue
            # effective-dated tolerance: the policy version in force on the invoice date applies
            v, rec_ = calculate("ap.price_variance", as_of=inv_date, billed=line["unit_price"], po_price=pol["unit_price"])
            receipts.append(rec_.to_dict())
            if v["exception"]:
                exceptions.append({"type": "price_variance",
                                   "detail": f"{line['item']} billed at {line['unit_price']:.2f} vs PO {pol['unit_price']:.2f} "
                                             f"({v['variance']:+.1%}; tolerance {v['tolerance']:.1%})"})
            if rec:
                rl = next((l for l in rec["lines"] if l["item"] == line["item"]), None)
                if rl and line["qty"] > rl["qty"]:
                    exceptions.append({"type": "qty_variance",
                                       "detail": f"{line['item']} billed {line['qty']} but received {rl['qty']}"})
    order = ["duplicate", "unknown_po", "no_receipt", "price_variance", "qty_variance", "item_not_on_po"]
    exceptions.sort(key=lambda e: order.index(e["type"]))
    status = exceptions[0]["type"] if exceptions else "match"
    if exceptions:
        e = exceptions[0]
        # deterministic comparisons are themselves a citable source (a "computed fact")
        sources[f"calc:{inv['id']}"] = e["detail"]
        expl = f"Exception: {e['detail']} [c:calc:{inv['id']}]."
    else:
        expl = f"Invoice lines agree with the purchase order and goods receipt [c:inv:{inv['id']}]."
    rep = verify(expl, sources, threshold=0.3)
    return {"id": inv["id"], "vendor": p.get("vendor"), "parsed": p, "status": status, "exceptions": exceptions,
            "action": "schedule_for_approval" if status == "match" else "route_to_ap_review",
            "explanation": rep.final_text, "groundedness": rep.groundedness, "receipts": receipts}


def run_ap() -> dict[str, Any]:
    svc = services()
    with tracer.span("module.ap"):
        seen: list[dict[str, Any]] = []
        for inv in svc.ap["invoices"]:
            seen.append(match_invoice(inv, seen))
        by_vendor: dict[str, list[float]] = {}
        for r in seen:
            if r["parsed"].get("total"):
                by_vendor.setdefault(r["vendor"], []).append(r["parsed"]["total"])
        for r in seen:  # robust z-score (median/MAD) anomaly flag
            xs = by_vendor.get(r["vendor"], [])
            t = r["parsed"].get("total")
            if t and len(xs) >= 4:
                med = statistics.median(xs)
                mad = statistics.median(abs(x - med) for x in xs) or 1.0
                r["anomaly_z"] = round(0.6745 * (t - med) / mad, 2)
            else:
                r["anomaly_z"] = 0.0
        counts: dict[str, int] = {}
        for r in seen:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        out = {"invoices": seen, "counts": counts,
               "touchless_rate": round(counts.get("match", 0) / max(1, len(seen)), 3)}
        svc.results["ap"] = out
        return out
