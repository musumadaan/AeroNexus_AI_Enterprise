"""Flagship workflow: two drawing revisions → validated diff → business impact → auditable ECR/ECO.

Runs as a durable Case (cases.py) through explicit, resumable steps:

  extract    run the extraction cascade on both revisions (per-field confidence + source tokens)
  diff       semantic field diff + text diff; numeric deltas through the unit engine (mm, gauge, in)
  validate   deterministic checks on Rev B (finish valid for material? dimension change size?)
  impact     where-used assemblies, open sales orders, work orders (WIP), open material POs,
             finished goods, inventory of the old material, customers (CRM), new-material supplier
  cost       unit price before/after (price model), cost delta and lead time via versioned
             formulas — every number carries a calculation receipt
  classify   severity by the versioned change.severity rule
  report     ECR/ECO document (Markdown + HTML) with evidence, receipts and approvals

Status: `blocked` if a step fails or the part can't be identified; `needs_review` if any finding is
uncertain (low-confidence extraction, unreadable value); otherwise `verified`. An approver then moves
it to `approved`, which releases the ECO in the ERP and sends customer notices — `executed`.
Nothing is written or sent before approval.
"""
from __future__ import annotations

import datetime as dt
import difflib
import html
import json
from typing import Any

from ..calc import calculate, parse_length, rule
from ..cases import Case
from ..extraction.document import Sheet
from ..observability import tracer
from ..security import AGENT, Principal
from ..services import services

IGNORE = {"revision", "scale", "title"}
DIMENSION_FIELDS = {"thickness", "length", "diameter"}


def _fields(sheet: Sheet) -> dict[str, Any]:
    r = services().pipeline.run(sheet)
    return {k: {"value": v.value, "confidence": v.confidence, "status": v.status, "tokens": v.token_ids, "note": v.note}
            for k, v in r.fields.items()} | {"_warnings": r.warnings}


def step_extract(before: Sheet, after: Sheet):
    def run(_: dict[str, Any]) -> dict[str, Any]:
        return {"before": _fields(before), "after": _fields(after), "before_id": before.id, "after_id": after.id}
    return run


def step_diff(before: Sheet, after: Sheet):
    def run(data: dict[str, Any]) -> dict[str, Any]:
        fa, fb = data["extract"]["before"], data["extract"]["after"]
        changes = []
        for name in [k for k in fa if not k.startswith("_")]:
            va, vb = fa[name]["value"], fb[name]["value"]
            if va == vb:
                continue
            ch = {"field": name, "before": va, "after": vb, "before_tokens": fa[name]["tokens"], "after_tokens": fb[name]["tokens"],
                  "confidence": round(min(fa[name]["confidence"], fb[name]["confidence"]), 3),
                  "uncertain": min(fa[name]["confidence"], fb[name]["confidence"]) < 0.9 or va is None or vb is None}
            if name in DIMENSION_FIELDS:
                ma, mb = parse_length(va), parse_length(vb)
                if ma and mb:
                    ch["delta_mm"] = round(mb - ma, 4)
                    ch["delta_pct"] = round((mb - ma) / ma, 4)
            changes.append(ch)
        ta, _ = before.text_with_offsets()
        tb, _ = after.text_with_offsets()
        sm = difflib.SequenceMatcher(None, ta.split(), tb.split())
        text_ops = [{"op": op, "before": " ".join(ta.split()[i1:i2]), "after": " ".join(tb.split()[j1:j2])}
                    for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal"]
        return {"changes": changes, "text_ops": text_ops, "similarity": round(sm.ratio(), 4)}
    return run


def step_validate(data: dict[str, Any]) -> dict[str, Any]:
    svc = services()
    fb = data["extract"]["after"]
    findings = []
    mat, coat = fb["material"]["value"], fb["coating"]["value"]
    if mat and coat:
        bad = [o for _, r, o in svc.graph.neighbors(mat, depth=1) if r == "incompatible_coating" and o == coat.upper()]
        if bad:
            findings.append({"severity": "high", "kind": "inconsistent_finish", "uncertain": False,
                             "message": f"Rev B specifies {coat}, which is not valid for {mat}."})
    for c in data["diff"]["changes"]:
        if c["uncertain"]:
            findings.append({"severity": "medium", "kind": "uncertain_change", "uncertain": True, "field": c["field"],
                             "message": f"{c['field']} change {c['before']} → {c['after']} has low extraction confidence; confirm on the drawing."})
    if fb["_warnings"] or data["extract"]["before"]["_warnings"]:
        findings.append({"severity": "high", "kind": "security", "uncertain": True,
                         "message": "A revision contains text that tries to instruct the AI; review the document."})
    return {"findings": findings}


def _unit_cost(fields: dict[str, Any]) -> tuple[float | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Deterministic material+finish cost of one part for a revision (receipt included)."""
    t = parse_length(fields["thickness"]["value"])
    mat = fields["material"]["value"]
    if not (t and mat):
        return None, None, None
    items = services().runtime.erp.find_item(mat, fields["thickness"]["value"] or "")
    if not items:
        return None, None, None
    it = items[0]
    coat = (fields["coating"]["value"] or "").upper()
    markup = next((m for k, m in rule("quote.coating_markup").params.items() if k in coat), 0.0)
    cost, rec = calculate("change.material_cost", thickness_mm=t, length_mm=parse_length(fields["length"]["value"]) or 1200.0,
                          width_mm=600.0, density_g_cc=float(it["density"]), rate_per_kg=float(it["rate_per_kg"]),
                          finish_markup=markup)
    return cost, it, rec.to_dict()


def step_impact(part_number: str):
    def run(data: dict[str, Any]) -> dict[str, Any]:
        rt = services().runtime
        call = lambda name, **a: rt.call(name, a, actor="agent")["result"]  # READ tools only
        fa, fb = data["extract"]["before"], data["extract"]["after"]
        changed = {c["field"] for c in data["diff"]["changes"]} - IGNORE
        material_change = bool(changed & {"material", "thickness"})
        orders = call("erp.open_orders_for", part_number=part_number)
        wos = call("erp.work_orders_for", part_number=part_number)
        mpos = call("erp.material_pos_for", part_number=part_number) if material_change else []
        accounts = call("crm.account_for_part", part_number=part_number)
        assemblies = call("erp.where_used", part_number=part_number)
        old_items = rt.erp.find_item(fa["material"]["value"] or "", fa["thickness"]["value"] or "") if material_change else []
        new_supplier = call("erp.supplier_for", material=fb["material"]["value"] or "") if material_change else None
        return {
            "changed_fields": sorted(changed), "material_change": material_change, "assemblies": assemblies,
            "open_orders": orders, "work_orders": wos, "material_pos": mpos, "accounts": accounts,
            "finished_goods": rt.erp.finished_goods.get(part_number, 0),
            "obsolete_inventory_kg": float(old_items[0]["on_hand_kg"]) if old_items else 0.0,
            "old_item": old_items[0]["code"] if old_items else None,
            "new_supplier": new_supplier,
            "in_production": any(o["status"] == "in_production" for o in orders) or any(w["status"] == "in_progress" for w in wos),
        }
    return run


def step_cost(data: dict[str, Any]) -> dict[str, Any]:
    fa, fb = data["extract"]["before"], data["extract"]["after"]
    imp = data["impact"]
    ub, item_b, rb = _unit_cost(fa)
    ua, item_a, ra = _unit_cost(fb)
    receipts = [r for r in (rb, ra) if r]
    if ub is None or ua is None:
        return {"available": False, "reason": "unit price could not be computed for one revision (missing material or thickness)"}
    open_qty = sum(o["qty"] for o in imp["open_orders"])
    wip = sum(w["wip_qty"] for w in imp["work_orders"]) if imp["changed_fields"] else 0
    cost, r1 = calculate("change.cost_delta", unit_before=ub, unit_after=ua, open_qty=open_qty, wip_qty=wip,
                         inventory_kg_obsolete=imp["obsolete_inventory_kg"], rate_per_kg=float(item_b["rate_per_kg"]))
    receipts.append(r1.to_dict())
    sup_days = imp["new_supplier"]["lead_days"] if imp["material_change"] and imp["new_supplier"] else 0
    lead, r2 = calculate("change.lead_time_days", supplier_lead_days=sup_days, engineering_days=5,
                         requalification_days=10 if "material" in imp["changed_fields"] else 0)
    receipts.append(r2.to_dict())
    late_wos = [w["wo"] for w in imp["work_orders"]
                if dt.date.fromisoformat(w["start"]) < dt.date.today() + dt.timedelta(days=lead)]
    return {"available": True, "unit_before": ub, "unit_after": ua, "cost": cost, "lead_time_days": lead,
            "work_orders_at_risk": late_wos, "receipts": receipts}


def step_classify(data: dict[str, Any]) -> dict[str, Any]:
    pct = max([abs(c.get("delta_pct", 0.0)) for c in data["diff"]["changes"]] or [0.0])
    sev, r = calculate("change.severity", changed_fields=data["impact"]["changed_fields"], dimension_change_pct=pct,
                       in_production=data["impact"]["in_production"])
    return {"severity": sev, "max_dimension_change_pct": pct, "receipt": r.to_dict()}


def build_report(case: Case) -> dict[str, str]:
    d = case.data
    diff, imp, cost, cls, val = d.get("diff", {}), d.get("impact", {}), d.get("cost", {}), d.get("classify", {}), d.get("validate", {})
    lines = [f"# Engineering change {'order' if case.status in {'approved', 'executed'} else 'request'} — {case.title}", "",
             f"Case **{case.id}** · status **{case.status}** · severity **{cls.get('severity', '?')}** · generated "
             f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M')}", "", "## What changed", "",
             "| Field | Rev A | Rev B | Δ | Confidence |", "|---|---|---|---|---|"]
    for c in diff.get("changes", []):
        delta = f"{c['delta_mm']:+g} mm ({c['delta_pct']:+.1%})" if "delta_mm" in c else ""
        lines.append(f"| {c['field']} | {c['before'] or '—'} | {c['after'] or '—'} | {delta} | {c['confidence']:.2f}"
                     f"{' ⚠ review' if c['uncertain'] else ''} |")
    found = [f"- **{f['severity']}** — {f['message']}" for f in val.get("findings", [])]
    lines += ["", "## Findings", ""] + (found or ["- No validation findings."])
    lines += ["", "## Affected records", "",
              f"- Assemblies (where-used): {', '.join(imp.get('assemblies', [])) or 'none'}",
              f"- Open sales orders: {', '.join(o['order_id'] + ' (' + o['status'] + ')' for o in imp.get('open_orders', [])) or 'none'}",
              f"- Work orders: {', '.join(w['wo'] + ' (' + w['status'] + ', WIP ' + str(w['wip_qty']) + ')' for w in imp.get('work_orders', [])) or 'none'}",
              f"- Open material POs to cancel or amend: {', '.join(p['po'] + ' ' + str(p['kg']) + ' kg' for p in imp.get('material_pos', [])) or 'none'}",
              f"- Customers to notify after approval: {', '.join(a['name'] for a in imp.get('accounts', [])) or 'none'}",
              f"- Finished goods on hand: {imp.get('finished_goods', 0)}"]
    if imp.get("new_supplier"):
        s = imp["new_supplier"]
        lines.append(f"- New-material supplier: {s['name']} (lead time {s['lead_days']} days, on-time {s['on_time']:.0%})")
    lines += ["", "## Cost and schedule impact", ""]
    if cost.get("available"):
        c = cost["cost"]
        lines += [f"- Material + finish cost per part: {cost['unit_before']:.2f} → {cost['unit_after']:.2f} CAD",
                  f"- Re-pricing open orders: {c['reprice_open_orders']:,.2f} CAD", f"- Scrap of WIP: {c['scrap_wip']:,.2f} CAD",
                  f"- Obsolete inventory (net of recovery): {c['obsolete_inventory']:,.2f} CAD", f"- **Total: {c['total']:,.2f} CAD**",
                  f"- Lead time to implement: {cost['lead_time_days']} days; work orders at risk: "
                  f"{', '.join(cost['work_orders_at_risk']) or 'none'}"]
    else:
        lines.append(f"- Not computed: {cost.get('reason', 'step not run')}")
    lines += ["", "## Calculation receipts (reproducible)", "", "| Formula | Rule versions | As of | Receipt |", "|---|---|---|---|"]
    for r in cost.get("receipts", []) + ([cls["receipt"]] if cls.get("receipt") else []):
        lines.append(f"| {r['formula']} | {json.dumps(r['rules']) if r['rules'] else '—'} | {r['as_of']} | `{r['receipt_hash']}` |")
    lines += ["", "## Approval trail", ""] + [f"- {h['type']} · {h['actor']} · {h['summary']}" for h in case.history]
    md = "\n".join(lines)
    body = "".join(f"<p>{html.escape(l)}</p>" if not l.startswith(("#", "|", "-")) else f"<pre>{html.escape(l)}</pre>"
                   for l in lines)
    return {"markdown": md, "html": f"<!doctype html><meta charset=utf-8><title>{html.escape(case.title)}</title>{body}"}


def open_change_order(before: Sheet, after: Sheet, part_number: str, who: Principal = AGENT) -> dict[str, Any]:
    svc = services()
    store = svc.cases
    with tracer.span("module.change_order"):
        case = store.open(who.tenant, "change_order", f"{part_number}: {before.meta.get('truth', {}).get('revision', 'A')} → "
                          f"{after.meta.get('truth', {}).get('revision', 'B')}", who.user or "agent")
        case = store.run_steps(case, [
            ("extract", step_extract(before, after)), ("diff", step_diff(before, after)), ("validate", step_validate),
            ("impact", step_impact(part_number)), ("cost", step_cost), ("classify", step_classify)], actor="agent")
        if case.status == "blocked":
            return finalize(case)
        findings = case.data["validate"]["findings"]
        if not case.data["diff"]["changes"]:
            case = store.set_status(case.id, case.tenant, "verified", "agent", "no differences found")
        elif any(f["uncertain"] or f["severity"] == "high" for f in findings):
            n = sum(f["uncertain"] or f["severity"] == "high" for f in findings)
            case = store.set_status(case.id, case.tenant, "needs_review", "agent", f"{n} finding(s) need an engineer")
        else:
            case = store.set_status(case.id, case.tenant, "verified", "agent", "all checks deterministic and consistent")
        case = store.put(case.id, case.tenant, "agent", part_number=part_number, before_id=before.id, after_id=after.id)
        return finalize(case)


def dependency_graph(case: Case) -> dict[str, Any]:
    """Part → where-used assemblies, orders → customers, work orders, material POs → suppliers, old stock.
    Every node is a real record from the ERP/CRM read tools; `impacted` marks what the change touches."""
    d = case.data
    imp, diff = d.get("impact") or {}, d.get("diff") or {}
    pn = d.get("part_number") or case.title.split(":")[0]
    changed = [c["field"] for c in diff.get("changes", []) if c["field"] not in IGNORE]
    nodes = [{"id": pn, "kind": "part", "label": pn, "detail": ", ".join(changed) or "no field changes", "impacted": bool(changed)}]
    edges: list[dict[str, Any]] = []

    def add(nid: str, kind: str, label: str, detail: str, src: str, rel: str, impacted: bool = True) -> None:
        if not any(n["id"] == nid for n in nodes):
            nodes.append({"id": nid, "kind": kind, "label": label, "detail": detail, "impacted": impacted})
        edges.append({"source": src, "target": nid, "relation": rel})

    for a in imp.get("assemblies", []):
        add(a, "assembly", a, "uses this part (where-used)", pn, "used in")
    acc = {x["id"]: x for x in imp.get("accounts", [])}
    for o in imp.get("open_orders", []):
        add(o["order_id"], "sales_order", o["order_id"], f"{o['qty']} pcs · {o['status'].replace('_', ' ')} · due {o['due']}", pn, "ordered on")
        c = acc.get(o.get("customer"))
        if c:
            add(c["id"], "customer", c["name"], f"tier {c.get('tier', '?')} · notify after approval", o["order_id"], "for customer")
    for c in imp.get("accounts", []):
        if not any(e["target"] == c["id"] for e in edges):
            add(c["id"], "customer", c["name"], f"tier {c.get('tier', '?')}", pn, "buys")
    risk = set((d.get("cost") or {}).get("work_orders_at_risk", []))
    for w in imp.get("work_orders", []):
        add(w["wo"], "work_order", w["wo"], f"{w['status'].replace('_', ' ')} · WIP {w['wip_qty']} · starts {w['start']}"
            + (" · schedule at risk" if w["wo"] in risk else ""), pn, "in production as")
    for m in imp.get("material_pos", []):
        add(m["po"], "material_po", m["po"], f"{m['kg']} kg {m['material']} · amend or cancel", pn, "material on")
    if imp.get("new_supplier"):
        sup = imp["new_supplier"]
        add(sup["id"], "supplier", sup["name"], f"lead time {sup['lead_days']} days · on-time {sup['on_time']:.0%}", pn, "new material from")
    if imp.get("old_item"):
        add(imp["old_item"], "inventory", imp["old_item"], f"{imp.get('obsolete_inventory_kg', 0)} kg old stock becomes obsolete",
            pn, "replaces stock", impacted=bool(imp.get("obsolete_inventory_kg")))
    return {"nodes": nodes, "edges": edges}


def cost_trace(case: Case) -> list[dict[str, Any]]:
    """Every calculation with its inputs, rule versions, output, receipt hash, the formula source and a fresh
    re-computation, so a reviewer can follow each number from inputs to total."""
    import inspect

    from ..calc import FORMULAS, verify_receipt

    d = case.data
    recs = list((d.get("cost") or {}).get("receipts", [])) + ([d["classify"]["receipt"]] if (d.get("classify") or {}).get("receipt") else [])
    out = []
    for r in recs:
        f = FORMULAS.get(r["formula"])
        src = inspect.getsource(f.fn).split("\n") if f else []
        body = "\n".join(l for l in src if not l.lstrip().startswith("@"))
        out.append({**r, "verified": verify_receipt(r), "source": body.strip()[:1200]})
    return out


def finalize(case: Case) -> dict[str, Any]:
    from ..cases import to_dict

    out = to_dict(case)
    out["report"] = build_report(case)
    if case.data.get("impact"):
        out["dependency_graph"] = dependency_graph(case)
        out["cost_trace"] = cost_trace(case)
    bid = case.data.get("before_id")
    pair = next((p for p in services().change_pairs if p["before"]["id"] == bid), None) if bid else None
    if pair:  # the two revisions, so the UI can draw the evidence
        out["sheets"] = {"a": Sheet.from_dict(pair["before"]).to_dict(), "b": Sheet.from_dict(pair["after"]).to_dict()}
    return out


def approve_change_order(case_id: str, who: Principal, approve: bool, note: str = "") -> dict[str, Any]:
    """Approver signs off → ECO released + notices sent (executed). Requires the 'approve' permission."""
    who.require("approve")
    svc = services()
    store = svc.cases
    case = store.get(case_id, who.tenant)
    if case.status == "needs_review" and approve:
        case = store.set_status(case_id, who.tenant, "verified", who.user, note or "reviewer confirmed uncertain findings")
    if not approve:
        return finalize(store.set_status(case_id, who.tenant, "rejected", who.user, note or "rejected by approver"))
    case = store.set_status(case_id, who.tenant, "approved", who.user, note)
    d = case.data
    rt = svc.runtime
    pn = d["part_number"]
    total = d["cost"]["cost"]["total"] if d["cost"].get("available") else 0.0
    summary = "; ".join(f"{c['field']}: {c['before']} → {c['after']}" for c in d["diff"]["changes"] if c["field"] not in IGNORE)
    results = []
    try:
        eco = rt.call("erp.create_eco", {"case_id": case_id, "part_number": pn, "summary": summary or "revision update",
                                         "cost_impact": float(total), "effective": dt.date.today().isoformat()},
                      actor="agent", module="change", case_id=case_id)
        results.append(rt.decide(eco["approval_id"], who.user, True).result if eco["status"] == "pending_approval" else eco)
        for acc in d["impact"]["accounts"]:
            n = rt.call("comms.send_email", {"to": acc["email"], "subject": f"Engineering change notice – {pn}",
                                             "body": f"Hello {acc['name']},\n\nPart {pn} has an approved engineering change: {summary}. "
                                                     f"We will contact you about open orders "
                                                     f"{', '.join(o['order_id'] for o in d['impact']['open_orders']) or ''}."},
                        actor="agent", module="change", case_id=case_id)
            if n["status"] == "pending_approval":
                results.append(rt.decide(n["approval_id"], who.user, True).result)
        case = store.put(case_id, who.tenant, who.user, execution=results)
        case = store.set_status(case_id, who.tenant, "executed", "agent", f"ECO released, {len(d['impact']['accounts'])} notice(s) sent")
    except Exception as exc:  # compensating behaviour: the case is blocked with the error, nothing half-claimed
        case = store.set_status(case_id, who.tenant, "blocked", "agent", f"execution failed: {exc}")
    return finalize(case)
