"""Deterministic calculation & rules engine — numbers never come from an LLM.

  * Units: length/mass/currency conversion with explicit tables (gauge → mm included).
  * Versioned, effective-dated rules: every threshold (price tolerance, due-soon %, RUL work-order
    limit, change-severity matrix, coating markup …) lives in RULES with a version and an
    effective-from date. `rule(id, on=date)` returns the version in force on that date, so a
    calculation from March can be re-run in October and give the March answer.
  * Calculation receipts: `calculate()` runs a registered pure function and returns the value plus a
    receipt {formula id, formula source hash, inputs, rule versions, output, receipt hash}. Re-running
    the receipt reproduces the output bit-for-bit (`verify_receipt`) — that is what an auditor needs.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import inspect
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------------------------
LENGTH_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8}
MASS_KG = {"kg": 1.0, "g": 0.001, "lb": 0.45359237, "t": 1000.0}
GAUGE_MM = {16: 1.6, 14: 2.0, 12: 2.8, 10: 3.5, 8: 4.3, 7: 4.7}


class UnitError(ValueError):
    pass


def to_mm(value: float, unit: str) -> float:
    unit = unit.lower().strip()
    if unit in {"ga", "gauge"}:
        g = int(value)
        if g not in GAUGE_MM:
            raise UnitError(f"unknown gauge {value}")
        return GAUGE_MM[g]
    if unit not in LENGTH_MM:
        raise UnitError(f"unknown length unit '{unit}'")
    return value * LENGTH_MM[unit]


def parse_length(text: str | None) -> float | None:
    """'3.5 mm' / '10 ga' / '0.25 in' → millimetres (None if unparseable)."""
    if not text:
        return None
    parts = text.replace(",", ".").split()
    try:
        return round(to_mm(float(parts[0]), parts[1] if len(parts) > 1 else "mm"), 4)
    except (ValueError, UnitError, IndexError):
        return None


# ---------------------------------------------------------------------------------------------
# Versioned rules
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Rule:
    id: str
    version: int
    effective_from: dt.date
    params: dict[str, Any]
    rationale: str = ""


RULES: list[Rule] = [
    Rule("ap.price_tolerance", 1, dt.date(2024, 1, 1), {"pct": 0.02}, "Three-way match policy v1"),
    Rule("ap.price_tolerance", 2, dt.date(2026, 7, 1), {"pct": 0.015}, "Tightened after 2026 audit (policy v2)"),
    Rule("mro.due_soon", 1, dt.date(2024, 1, 1), {"fraction_of_interval": 0.10}),
    Rule("pdm.work_order", 1, dt.date(2024, 1, 1), {"rul_low_bound_cycles": 30, "health_z": 3.0},
         "Open an inspection work order when the 90% lower bound of RUL drops below 30 cycles"),
    Rule("quote.coating_markup", 1, dt.date(2024, 1, 1), {"GALV": 0.17, "POWDER": 0.22, "ANODIZE": 0.12, "PRIME": 0.15,
                                                        "PASSIVATE": 0.08}),
    Rule("quote.approval_limit", 1, dt.date(2024, 1, 1), {"cad": 25000.0}),
    Rule("change.severity", 1, dt.date(2024, 1, 1), {
        "high_fields": ["thickness", "material", "length", "coating", "diameter", "tolerance"],
        "medium_fields": ["quantity", "standard"], "dimension_pct_high": 0.05}),
    Rule("change.scrap_recovery", 1, dt.date(2024, 1, 1), {"fraction": 0.25},
         "Scrap value recovered on obsolete material (fraction of book value)"),
    Rule("fx.cad_per", 1, dt.date(2024, 1, 1), {"USD": 1.36, "EUR": 1.48, "CAD": 1.0}),
]


class RuleNotFound(KeyError):
    pass


def rule(rule_id: str, on: dt.date | None = None) -> Rule:
    on = on or dt.date.today()
    cands = [r for r in RULES if r.id == rule_id and r.effective_from <= on]
    if not cands:
        raise RuleNotFound(f"no version of {rule_id} in force on {on}")
    return max(cands, key=lambda r: r.effective_from)


def fx_to_cad(amount: float, currency: str, on: dt.date | None = None) -> float:
    rates = rule("fx.cad_per", on).params
    if currency not in rates:
        raise UnitError(f"no FX rate for {currency}")
    return round(amount * rates[currency], 2)


def within_tolerance(nominal: float, actual: float, tol: float) -> bool:
    return abs(actual - nominal) <= tol + 1e-12


# ---------------------------------------------------------------------------------------------
# Formulas + receipts
# ---------------------------------------------------------------------------------------------
@dataclass
class Receipt:
    formula: str
    formula_hash: str
    inputs: dict[str, Any]
    rules: dict[str, int]
    as_of: str
    output: Any
    receipt_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Formula:
    id: str
    fn: Callable[..., Any]
    rules: list[str] = field(default_factory=list)

    @property
    def source_hash(self) -> str:
        return hashlib.sha256(inspect.getsource(self.fn).encode()).hexdigest()[:16]


FORMULAS: dict[str, Formula] = {}


def formula(fid: str, rules: list[str] | None = None):
    def deco(fn):
        FORMULAS[fid] = Formula(fid, fn, rules or [])
        return fn

    return deco


def _canon(x: Any) -> str:
    return json.dumps(x, sort_keys=True, default=str)


def calculate(fid: str, as_of: dt.date | None = None, **inputs: Any) -> tuple[Any, Receipt]:
    f = FORMULAS[fid]
    as_of = as_of or dt.date.today()
    used = {r: rule(r, as_of) for r in f.rules}
    out = f.fn(**inputs, _rules={k: v.params for k, v in used.items()})
    rec = Receipt(fid, f.source_hash, inputs, {k: v.version for k, v in used.items()}, as_of.isoformat(), out)
    rec.receipt_hash = hashlib.sha256(_canon([rec.formula, rec.formula_hash, rec.inputs, rec.rules, rec.as_of, rec.output])
                                      .encode()).hexdigest()[:20]
    return out, rec


def verify_receipt(rec: dict[str, Any]) -> bool:
    """Recompute a stored receipt; True only if formula code, rule versions and output all match."""
    f = FORMULAS.get(rec["formula"])
    if f is None or f.source_hash != rec["formula_hash"]:
        return False
    out, again = calculate(rec["formula"], dt.date.fromisoformat(rec["as_of"]), **rec["inputs"])
    return again.rules == rec["rules"] and _canon(out) == _canon(rec["output"])


# --- formulas used by the modules -------------------------------------------------------------
@formula("quote.plate_mass_kg")
def plate_mass_kg(thickness_mm: float, length_mm: float, width_mm: float, density_g_cc: float, _rules=None) -> float:
    return round(thickness_mm * length_mm * width_mm / 1000 * density_g_cc / 1000, 4)


@formula("change.material_cost")
def material_cost(thickness_mm: float, length_mm: float, width_mm: float, density_g_cc: float, rate_per_kg: float,
                  finish_markup: float, _rules=None) -> float:
    """Physics-based material + finish cost per part. Used for *marginal* change deltas because tree
    price models are piecewise constant and can return the same price for two different lengths."""
    kg = thickness_mm * length_mm * width_mm / 1000 * density_g_cc / 1000
    return round(kg * rate_per_kg * (1 + finish_markup), 2)


@formula("change.cost_delta", rules=["change.scrap_recovery"])
def change_cost_delta(unit_before: float, unit_after: float, open_qty: int, wip_qty: int, inventory_kg_obsolete: float,
                      rate_per_kg: float, _rules=None) -> dict[str, float]:
    recovery = _rules["change.scrap_recovery"]["fraction"]
    reprice = round((unit_after - unit_before) * open_qty, 2)
    scrap_wip = round(unit_before * wip_qty, 2)
    obsolete = round(inventory_kg_obsolete * rate_per_kg * (1 - recovery), 2)
    return {"reprice_open_orders": reprice, "scrap_wip": scrap_wip, "obsolete_inventory": obsolete,
            "total": round(reprice + scrap_wip + obsolete, 2)}


@formula("change.lead_time_days")
def change_lead_time(supplier_lead_days: int, engineering_days: int, requalification_days: int, _rules=None) -> int:
    return int(supplier_lead_days + engineering_days + requalification_days)


@formula("change.severity", rules=["change.severity"])
def change_severity(changed_fields: list[str], dimension_change_pct: float, in_production: bool, _rules=None) -> str:
    p = _rules["change.severity"]
    high = bool(set(changed_fields) & set(p["high_fields"])) or dimension_change_pct >= p["dimension_pct_high"]
    if high:
        return "critical" if in_production else "high"
    if set(changed_fields) & set(p["medium_fields"]):
        return "medium"
    return "low"


@formula("ap.price_variance", rules=["ap.price_tolerance"])
def price_variance(billed: float, po_price: float, _rules=None) -> dict[str, Any]:
    var = (billed - po_price) / po_price
    tol = _rules["ap.price_tolerance"]["pct"]
    return {"variance": round(var, 6), "tolerance": tol, "exception": abs(var) > tol + 1e-12}


@formula("mro.next_due", rules=["mro.due_soon"])
def mro_next_due(last_compliance_hours: float, interval_hours: float, current_hours: float, _rules=None) -> dict[str, Any]:
    due = round(last_compliance_hours + interval_hours, 1)
    remaining = round(due - current_hours, 1)
    soon = _rules["mro.due_soon"]["fraction_of_interval"] * interval_hours
    status = "overdue" if remaining < 0 else ("due_soon" if remaining <= soon else "compliant")
    return {"next_due": due, "remaining": remaining, "status": status}


@formula("pdm.work_order_needed", rules=["pdm.work_order"])
def pdm_work_order(rul_low: float, health: float, health_threshold: float, _rules=None) -> dict[str, Any]:
    p = _rules["pdm.work_order"]
    reasons = []
    if rul_low <= p["rul_low_bound_cycles"]:
        reasons.append(f"90% lower bound of remaining life ({rul_low} cycles) is at or below {p['rul_low_bound_cycles']}")
    if health >= health_threshold:
        reasons.append(f"health score {health:.3f} above anomaly threshold {health_threshold:.3f}")
    return {"needed": bool(reasons), "reasons": reasons}


def nan_safe(x: float) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else x
