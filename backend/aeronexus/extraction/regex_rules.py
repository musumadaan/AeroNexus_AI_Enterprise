"""Deterministic extraction rules: the cheapest, fastest, most auditable layer.

Design rule: anything with a rigid surface form (dimensions, tolerances, gauges, thread specs,
standards, part/AD/invoice numbers, money, dates) is extracted by regex FIRST. Only fields the
rules cannot resolve go to NER, and only what NER cannot resolve goes to an LLM. That cascade is
the single biggest latency/cost optimisation in the system: regex is ~microseconds, deterministic,
and every hit carries the exact source span.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable


@dataclass(frozen=True)
class Match:
    field: str
    value: str
    raw: str
    start: int
    end: int
    confidence: float
    rule: str


@dataclass(frozen=True)
class Rule:
    name: str
    field: str
    pattern: re.Pattern[str]
    confidence: float
    normalize: Callable[[re.Match[str]], str] = lambda m: m.group(0).strip()


def _num(s: str) -> str:
    return s.replace(",", ".")


def _dim(m: re.Match[str]) -> str:
    unit = (m.group("unit") or "mm").lower()
    return f"{_num(m.group('val'))} {unit}"


def _tol(m: re.Match[str]) -> str:
    if m.groupdict().get("pm"):
        return f"±{_num(m.group('pm'))}"
    return f"+{_num(m.group('up'))}/-{_num(m.group('lo'))}"


def _money(m: re.Match[str]) -> str:
    return f"{float(m.group('amt').replace(',', '')):.2f}"


NUM = r"\d+(?:[.,]\d+)?"

RULES: list[Rule] = [
    # --- engineering drawings -------------------------------------------------------------
    Rule("diameter", "diameter", re.compile(rf"(?:Ø|⌀|DIA\.?\s*)(?P<val>{NUM})\s*(?P<unit>mm|in)?", re.I), 0.97, _dim),
    Rule("thread", "thread", re.compile(r"\bM(?P<d>\d{1,2})(?:\s*[x×]\s*(?P<p>\d(?:\.\d+)?))?\b"), 0.95,
         lambda m: f"M{m.group('d')}" + (f"x{m.group('p')}" if m.group("p") else "")),
    Rule("tolerance_pm", "tolerance", re.compile(rf"±\s*(?P<pm>{NUM})"), 0.97, _tol),
    Rule("tolerance_asym", "tolerance", re.compile(rf"\+(?P<up>{NUM})\s*/\s*-(?P<lo>{NUM})"), 0.95, _tol),
    Rule("gauge", "thickness", re.compile(r"\b(?P<g>\d{1,2})\s*(?:GA|GAUGE|GA\.)\b", re.I), 0.96,
         lambda m: f"{m.group('g')} ga"),
    Rule("thickness", "thickness", re.compile(rf"\b(?:THK|THICK(?:NESS)?|T)\s*[=:]?\s*(?P<val>{NUM})\s*(?P<unit>mm|in)", re.I),
         0.95, _dim),
    Rule("length", "length", re.compile(rf"\b(?:L|LENGTH|OAL)\s*[=:]?\s*(?P<val>{NUM})\s*(?P<unit>mm|m|in|ft)\b", re.I), 0.93, _dim),
    Rule("qty", "quantity", re.compile(r"\b(?:QTY|QUANTITY)\s*[:=]?\s*(?P<q>\d{1,6})\b", re.I), 0.96, lambda m: m.group("q")),
    # (?<!PER )/(?<!TO ): "GALVANIZED PER ASTM A123" is a coating spec, not the base material
    Rule("material_astm", "material", re.compile(r"(?<!PER )(?<!TO )\b(?:ASTM|CSA)\s*[A-Z]?\s?\d{2,4}(?:[-/][A-Z0-9.]+)*(?:\s*(?:GR(?:ADE)?\.?\s*\w+))?", re.I),
         0.9, lambda m: re.sub(r"\s+", " ", m.group(0).upper().strip())),
    Rule("material_alloy", "material", re.compile(r"\b(?:AL(?:UMINUM)?\s*)?(?:6061|7075|5052|2024)-T\d{1,4}\b|\b(?:SS|AISI)\s*30[4-9]L?\b|\b316L\b"
                                                 r"|\bTI-6AL-4V\b|\bINCONEL\s*\d{3}\b", re.I),
         0.92, lambda m: m.group(0).upper()),
    Rule("coating", "coating", re.compile(r"(?:\b(?:HOT[- ]DIP(?:PED)?\s+)?GALVANI[SZ]ED?\b|\bPOWDER\s+COAT(?:ED)?\b|\bANODI[SZ]E[D]?\b(?:\s+TYPE\s+I{1,3})?"
                                         r"|\bPRIME[D]?\b|\bPASSIVATE[D]?\b)(?:\s+(?:PER|TO)\s+(?:ASTM\s*A\d+|MIL-[A-Z]+-\d+|AMS\s?\d{4}))?", re.I),
         0.9, lambda m: re.sub(r"\s+", " ", m.group(0).upper())),
    Rule("drawing_no", "drawing_number", re.compile(r"\b(?:DWG|DRAWING)\s*(?:NO\.?|#)\s*[:=]?\s*(?P<v>[A-Z0-9][A-Z0-9-]{3,})", re.I), 0.95,
         lambda m: m.group("v").upper()),
    Rule("part_no", "part_number", re.compile(r"\b(?:P/N|PART\s*(?:NO\.?|#))\s*[:=]?\s*(?P<v>[A-Z0-9][A-Z0-9-]{3,})", re.I), 0.95,
         lambda m: m.group("v").upper()),
    Rule("revision", "revision", re.compile(r"\bREV(?:ISION)?\.?\s*[:=]?\s*(?P<v>[A-Z]{1,2}|\d{1,2})\b"), 0.93, lambda m: m.group("v")),
    Rule("scale", "scale", re.compile(r"\bSCALE\s*[:=]?\s*(?P<v>\d+\s*:\s*\d+|NTS)\b", re.I), 0.95, lambda m: m.group("v").replace(" ", "")),
    Rule("standard", "standard", re.compile(r"\b(?:ASME\s*Y14\.\d+M?|ISO\s*\d{3,5}(?:-\d+)?|AASHTO\s*[A-Z]?\d+|CSA\s*S\d+(?:\.\d+)?|AWS\s*D\d{1,2}\.\d"
                                         r"|MIL-STD-\d+)\b", re.I),
         0.94, lambda m: re.sub(r"\s+", " ", m.group(0).upper())),
    # --- aviation maintenance ------------------------------------------------------------
    Rule("ad_number", "ad_number", re.compile(r"\b(?:AD\s*)?(?P<v>(?:19|20)\d{2}-\d{2}-\d{2,3})\b"), 0.96, lambda m: m.group("v")),
    Rule("tail", "tail_number", re.compile(r"\b(?P<v>N\d{1,5}[A-Z]{0,2}|C-[FG][A-Z]{3})\b"), 0.9, lambda m: m.group("v")),
    Rule("hours", "hours", re.compile(r"\b(?P<v>\d{1,6}(?:\.\d)?)\s*(?:HRS?|HOURS|TT|TTAF|TSN)\b", re.I), 0.9, lambda m: m.group("v")),
    # --- accounts payable --------------------------------------------------------------
    Rule("invoice_no", "invoice_number", re.compile(r"\bINV(?:OICE)?\s*(?:NO\.?|#)?\s*[:=]?\s*(?P<v>[A-Z0-9-]{4,})", re.I), 0.95,
         lambda m: m.group("v").upper()),
    Rule("po_no", "po_number", re.compile(r"\bP\.?O\.?\s*(?:NO\.?|#)?\s*[:=]?\s*(?P<v>\d{4,})\b", re.I), 0.95, lambda m: m.group("v")),
    Rule("money", "amount", re.compile(r"(?:\$|CAD|USD)\s?(?P<amt>\d{1,3}(?:,\d{3})*(?:\.\d{2})|\d+(?:\.\d{2}))"), 0.9, _money),
    Rule("date_iso", "date", re.compile(r"\b(?P<v>20\d{2}-\d{2}-\d{2})\b"), 0.95, lambda m: m.group("v")),
]


def run_rules(text: str, fields: Iterable[str] | None = None) -> list[Match]:
    wanted = set(fields) if fields else None
    out: list[Match] = []
    for rule in RULES:
        if wanted and rule.field not in wanted:
            continue
        for m in rule.pattern.finditer(text):
            out.append(Match(rule.field, rule.normalize(m), m.group(0), m.start(), m.end(), rule.confidence, rule.name))
    return out


# --- Responsible AI: PII redaction before anything is logged or sent to a third-party LLM --------
PII = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
]


def redact(text: str) -> str:
    for label, pat in PII:
        text = pat.sub(f"[{label}]", text)
    return text


# --- Prompt-injection screening for retrieved / uploaded text -----------------------------------
INJECTION = re.compile(
    r"ignore (?:all |any )?(?:previous|prior|above) instructions|disregard (?:the )?system prompt|"
    r"you are now|act as (?:an? )?(?:admin|developer)|reveal (?:your )?(?:system )?prompt|"
    r"send (?:this|the data) to|approve (?:this|all) (?:quote|invoice|payment)s? automatically",
    re.I,
)


def looks_like_injection(text: str) -> bool:
    return bool(INJECTION.search(text))
