
"""Extraction cascade: layout → regex → NER → LLM (only for leftovers) → verify → route.

Every field comes out with calibrated confidence, source token IDs, and a
routing status:
  auto_accepted  (conf >= auto_accept) → flows into the ERP draft
  needs_review   (conf >= 0.4 and below auto_accept) → human review
  missing        (no trustworthy value) → reviewer fills it or an RFI is sent

LLM outputs must be verbatim-supported by the sheet text; unsupported values
are discarded as hallucinations.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import settings
from ..llm import LLMError, LLMRequest, MockLLM, get_router
from ..observability import tracer
from .document import Sheet, get_detector
from .ner import get_ner
from .regex_rules import looks_like_injection, run_rules

QUOTE_FIELDS = [
    "drawing_number",
    "part_number",
    "title",
    "revision",
    "material",
    "thickness",
    "coating",
    "quantity",
    "diameter",
    "length",
    "tolerance",
    "standard",
    "scale",
]

REGION_PRIOR: dict[str, set[str]] = {
    "drawing_number": {"title_block"},
    "part_number": {"title_block", "bom_table"},
    "title": {"title_block"},
    "revision": {"title_block", "revision_table"},
    "scale": {"title_block"},
    "material": {"title_block", "bom_table", "notes"},
    "thickness": {"title_block", "bom_table", "notes", "view"},
    "coating": {"notes", "title_block"},
    "quantity": {"bom_table", "title_block"},
    "diameter": {"view"},
    "length": {"view", "bom_table"},
    "tolerance": {"title_block", "notes", "view"},
    "standard": {"notes", "title_block"},
}

NER_FIELD = {
    "MATERIAL": "material",
    "COATING": "coating",
}

FIELD_LABELS = {
    "thickness": {"THK", "THICK", "THICKNESS"},
    "quantity": {"QTY", "QUANTITY"},
    "revision": {"REV", "REVISION"},
    "scale": {"SCALE"},
}


@dataclass
class FieldResult:
    name: str
    value: str | None
    confidence: float
    source: str
    rule: str = ""
    token_ids: list[str] = field(default_factory=list)
    status: str = "missing"
    alternatives: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class ExtractionResult:
    sheet_id: str
    fields: dict[str, FieldResult]
    regions: list[dict[str, Any]]
    warnings: list[str]
    llm_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet_id": self.sheet_id,
            "regions": self.regions,
            "warnings": self.warnings,
            "llm_calls": self.llm_calls,
            "fields": {
                name: asdict(result)
                for name, result in self.fields.items()
            },
        }


def _status(confidence: float) -> str:
    if confidence >= settings.auto_accept_conf:
        return "auto_accepted"
    if confidence >= 0.4:
        return "needs_review"
    return "missing"


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _clean_title(value: str, source_text: str) -> str | None:
    """Remove trailing title-block fields from an LLM-proposed title."""
    value = value.strip().strip("\"'")

    value = re.sub(
        r"^\s*TITLE\s*:?\s*",
        "",
        value,
        flags=re.I,
    )

    value = re.split(
        r"\s+(?=(?:DWG(?:\s+NO\.?)?|DRAWING(?:\s+NO\.?)?|"
        r"P/N|PART(?:\s+NO\.?)?|REV(?:ISION)?|SCALE|"
        r"MATERIAL|MATL|THK|THICKNESS|QTY|QUANTITY|DRAWN)"
        r"\s*:?\s*)",
        value,
        maxsplit=1,
        flags=re.I,
    )[0].strip()

    if not value:
        return None

    if value.casefold() not in source_text.casefold():
        return None

    # Conservative safeguard; tune against legitimate titles in your dataset.
    if len(value.split()) > 12:
        return None

    return value


class ExtractionPipeline:
    def __init__(
        self,
        fields: list[str] | None = None,
        use_llm: bool = True,
    ) -> None:
        self.fields = fields or QUOTE_FIELDS
        self.use_llm = use_llm
        self.detector = get_detector()
        self.ner = get_ner()

    def run(self, sheet: Sheet) -> ExtractionResult:
        warnings: list[str] = []

        # 1. Layout detection
        with tracer.span("extract.layout"):
            sheet.regions = self.detector(sheet)

        region_of = {
            token_id: region.label
            for region in sheet.regions
            for token_id in region.token_ids
        }

        text, offsets = sheet.text_with_offsets()

        if looks_like_injection(text):
            warnings.append(
                "Possible prompt-injection text on sheet; "
                "LLM fallback disabled for this document."
            )

        candidates: dict[str, list[FieldResult]] = {
            name: [] for name in self.fields
        }

        # 2. Regex extraction
        with tracer.span("extract.regex"):
            for match in run_rules(text, self.fields):
                token_ids = sheet.tokens_for_span(
                    offsets,
                    match.start,
                    match.end,
                )

                in_prior = any(
                    region_of.get(token_id)
                    in REGION_PRIOR.get(match.field, set())
                    for token_id in token_ids
                )

                confidence = match.confidence * (
                    1.0 if in_prior else 0.85
                )

                candidates[match.field].append(
                    FieldResult(
                        match.field,
                        match.value,
                        confidence,
                        "regex",
                        match.rule,
                        token_ids,
                    )
                )

        # 3. NER extraction
        with tracer.span("extract.ner"):
            for entity in self.ner(text):
                field_name = NER_FIELD.get(entity.label)

                if field_name not in candidates:
                    continue

                token_ids = sheet.tokens_for_span(
                    offsets,
                    entity.start,
                    entity.end,
                )

                candidates[field_name].append(
                    FieldResult(
                        field_name,
                        entity.text.upper(),
                        entity.confidence * 0.92,
                        "ner",
                        entity.source,
                        token_ids,
                    )
                )

        # 4. Resolve candidates from regex and NER.
        results: dict[str, FieldResult] = {
            field_name: self._resolve(field_name, field_candidates)
            for field_name, field_candidates in candidates.items()
        }

        # 5. Label-without-value check.
        title_region = next(
            (
                region
                for region in sheet.regions
                if region.label == "title_block"
            ),
            None,
        )

        if title_region:
            title_ids = set(title_region.token_ids)
            title_text = {
                sheet.token(token_id).text.upper().rstrip(":")
                for token_id in title_region.token_ids
            }

            for field_name, labels in FIELD_LABELS.items():
                result = results.get(field_name)

                if (
                    result
                    and result.value
                    and title_text.intersection(labels)
                    and not set(result.token_ids).intersection(title_ids)
                ):
                    result.confidence = round(
                        result.confidence * 0.6,
                        3,
                    )
                    result.note = (
                        "title-block value unreadable; "
                        "value taken from elsewhere on the sheet"
                    )
                    tracer.incr(
                        "extract.crosscheck.label_without_value"
                    )

        # 6. Quantity cross-check against the BOM table.
        quantity = results.get("quantity")
        bom_region = next(
            (
                region
                for region in sheet.regions
                if region.label == "bom_table"
            ),
            None,
        )

        if quantity and quantity.value and bom_region:
            bom_values = {
                sheet.token(token_id).text
                for token_id in bom_region.token_ids
            }

            if quantity.value in bom_values:
                quantity.confidence = round(
                    min(0.99, quantity.confidence + 0.02),
                    3,
                )
            else:
                quantity.confidence = round(
                    quantity.confidence * 0.7,
                    3,
                )
                quantity.note = (
                    "title-block QTY disagrees with BOM table"
                )
                tracer.incr("extract.crosscheck.qty_mismatch")

        # 7. Moved-title confidence penalty.
        if title_region:
            _, title_y, _, _ = title_region.bbox
            moved_title = title_y < 0.25 * sheet.height

            if moved_title:
                title_ids = set(title_region.token_ids)

                for result in results.values():
                    if result.value is None:
                        continue

                    if title_ids.intersection(result.token_ids):
                        result.confidence = round(
                            result.confidence * 0.7,
                            3,
                        )

                        penalty_note = (
                            "moved title block; confidence reduced"
                        )

                        if penalty_note not in result.note:
                            result.note = (
                                f"{result.note}; " if result.note else ""
                            ) + penalty_note

        # 8. LLM fallback for fields not found by regex or NER.
        llm_calls = 0
        missing = [
            field_name
            for field_name, result in results.items()
            if result.value is None
        ]

        # Injection safeguard: warnings disable LLM fallback.
        if missing and self.use_llm and not warnings:
            llm_calls = 1

            for field_name, result in self._llm_fallback(
                sheet,
                missing,
            ).items():
                results[field_name] = result

        # 9. Assign routing statuses after confidence adjustments.
        for result in results.values():
            result.status = (
                _status(result.confidence)
                if result.value is not None
                else "missing"
            )

        tracer.incr(
            "extract.fields.auto",
            sum(
                result.status == "auto_accepted"
                for result in results.values()
            ),
        )
        tracer.incr(
            "extract.fields.review",
            sum(
                result.status == "needs_review"
                for result in results.values()
            ),
        )

        return ExtractionResult(
            sheet.id,
            results,
            sheet.to_dict()["regions"],
            warnings,
            llm_calls,
        )

    @staticmethod
    def _resolve(
        field_name: str,
        candidates: list[FieldResult],
    ) -> FieldResult:
        if not candidates:
            return FieldResult(
                field_name,
                None,
                0.0,
                "none",
            )

        groups: dict[str, list[FieldResult]] = {}

        for candidate in sorted(
            candidates,
            key=lambda item: -len(_norm(item.value or "")),
        ):
            key = _norm(candidate.value or "")

            parent = next(
                (
                    existing_key
                    for existing_key in groups
                    if key and key in existing_key
                ),
                None,
            )

            groups.setdefault(parent or key, []).append(candidate)

        ranked = sorted(
            groups.values(),
            key=lambda group: (
                any(item.source == "regex" for item in group),
                max(item.confidence for item in group),
                len(group),
            ),
            reverse=True,
        )

        best_group = ranked[0]
        best = max(
            best_group,
            key=lambda item: (
                item.source == "regex",
                item.confidence,
            ),
        )

        confidence = max(
            item.confidence for item in best_group
        )

        if (
            len({item.source for item in best_group}) > 1
            or len(best_group) > 1
        ):
            confidence = min(0.99, confidence + 0.03)

        alternatives = [
            group[0].value
            for group in ranked[1:]
            if group[0].value
        ]

        rival_regex = [
            item
            for group in ranked[1:]
            for item in group
            if item.source == "regex" and item.confidence > 0.8
        ]

        note = best.note

        if rival_regex and field_name not in {
            "tolerance",
            "standard",
            "diameter",
        }:
            confidence *= 0.8
            note = "conflicting values on sheet"

        token_ids = sorted(
            {
                token_id
                for candidate in best_group
                for token_id in candidate.token_ids
            }
        )

        return FieldResult(
            field_name,
            best.value,
            round(confidence, 3),
            best.source,
            best.rule,
            token_ids,
            alternatives=alternatives[:3],
            note=note,
        )

    def _llm_fallback(
        self,
        sheet: Sheet,
        missing: list[str],
    ) -> dict[str, FieldResult]:
        title_ids = next(
            (
                region.token_ids
                for region in sheet.regions
                if region.label == "title_block"
            ),
            [],
        )

        context, _ = sheet.text_with_offsets(title_ids or None)
        full_text, full_offsets = sheet.text_with_offsets()

        request = LLMRequest(
            task="extract_fields",
            prompt=(
                "Extract the following fields from this engineering drawing "
                "text. Copy values EXACTLY as written; use null if absent. "
                "For title, return ONLY the title value, stopping before "
                "the next title-block label such as DWG NO, P/N, REV, "
                "SCALE, MATERIAL, THK, or QTY. Do not combine fields. "
                f"Fields: {missing}.\n"
                'Return JSON: {"fields": {...}, "confidence": 0-1}\n\n'
                f"TITLE BLOCK:\n{context}\n\n"
                f"FULL SHEET:\n{full_text[:4000]}"
            ),
            payload={
                "missing": missing,
                "text": full_text,
            },
            json_schema={
                "fields": "dict",
                "confidence": "float",
            },
        )

        output: dict[str, FieldResult] = {}

        try:
            with tracer.span(
                "extract.llm_fallback",
                n_fields=len(missing),
            ):
                response = get_router().complete(request)
        except LLMError:
            tracer.incr("extract.llm_fallback.failed")
            return output

        values = (response.parsed or {}).get("fields", {}) or {}
        confidence = response.confidence or 0.5

        for field_name in missing:
            value = values.get(field_name)

            if not isinstance(value, str) or not value.strip():
                continue

            value = value.strip()

            if field_name == "title":
                cleaned = _clean_title(
                    value,
                    context or full_text,
                )

                if cleaned is None:
                    source_text = context or full_text

                    if value.casefold() not in source_text.casefold():
                        tracer.incr("extract.llm.hallucination_blocked")
                    else:
                        tracer.incr("extract.llm.invalid_field_value")

                    continue

                value = cleaned

            index = full_text.casefold().find(value.casefold())

            if index < 0:
                tracer.incr("extract.llm.hallucination_blocked")
                continue

            token_ids = sheet.tokens_for_span(
                full_offsets,
                index,
                index + len(value),
            )

            output[field_name] = FieldResult(
                field_name,
                value,
                round(min(confidence, 0.85), 3),
                "llm",
                response.model,
                token_ids,
                note="LLM fallback; verified verbatim on sheet",
            )

        return output


@MockLLM.register("extract_fields")
def _mock_extract(req: LLMRequest, model: str) -> str:
    text: str = req.payload.get("text", "")
    fields: dict[str, Any] = {}

    for field_name in req.payload.get("missing", []):
        if field_name == "title":
            match = re.search(
                r"TITLE\s*:?\s*(.+?)(?=\s+(?:DWG|DRAWING|P/N|PART|REV|"
                r"SCALE|MATERIAL|MATL|QTY|DRAWN)\b|$)",
                text,
                re.I,
            )

            if match:
                fields["title"] = match.group(1).strip()

    is_small_model = (
        "small" in model.lower()
        or "3b" in model.lower()
        or "20b" in model.lower()
    )
    confidence = 0.55 if is_small_model else 0.82

    return json.dumps(
        {
            "fields": fields,
            "confidence": confidence,
        }
    )
