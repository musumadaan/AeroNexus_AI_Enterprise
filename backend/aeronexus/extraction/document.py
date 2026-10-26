
"""Document model + layout/vision layer.

A Sheet is a page represented as OCR tokens with bounding boxes. Keeping boxes
end-to-end allows the UI to highlight the source of every extracted value.

Layout detection identifies title blocks, BOM tables, notes, and views. The
default detector combines geometric and text-based title-block detection.
If a YOLO model is available at backend/models/detector.pt and ultralytics
is installed, it can be used instead.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import MODELS


@dataclass
class Token:
    id: str
    text: str
    bbox: tuple[float, float, float, float]
    page: int = 0


@dataclass
class Region:
    label: str
    bbox: tuple[float, float, float, float]
    token_ids: list[str]
    confidence: float


@dataclass
class Sheet:
    id: str
    tokens: list[Token]
    width: float = 1000
    height: float = 707
    meta: dict[str, Any] = field(default_factory=dict)
    regions: list[Region] = field(default_factory=list)

    def text_with_offsets(
        self, token_ids: list[str] | None = None
    ) -> tuple[str, list[tuple[int, int, str]]]:
        """Return text and character offsets mapped to token IDs."""
        parts: list[str] = []
        offsets: list[tuple[int, int, str]] = []
        pos = 0
        allowed = set(token_ids) if token_ids is not None else None

        for token in self.tokens:
            if allowed is not None and token.id not in allowed:
                continue

            parts.append(token.text)
            offsets.append((pos, pos + len(token.text), token.id))
            pos += len(token.text) + 1

        return " ".join(parts), offsets

    def tokens_for_span(
        self,
        offsets: list[tuple[int, int, str]],
        start: int,
        end: int,
    ) -> list[str]:
        return [
            token_id
            for start_offset, end_offset, token_id in offsets
            if start_offset < end and end_offset > start
        ]

    def token(self, token_id: str) -> Token:
        return next(token for token in self.tokens if token.id == token_id)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Sheet":
        tokens = [
            Token(
                token["id"],
                token["text"],
                tuple(token["bbox"]),
                token.get("page", 0),
            )
            for token in data["tokens"]
        ]

        return cls(
            id=data["id"],
            tokens=tokens,
            width=data.get("width", 1000),
            height=data.get("height", 707),
            meta=data.get("meta", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "width": self.width,
            "height": self.height,
            "meta": self.meta,
            "tokens": [
                {
                    "id": token.id,
                    "text": token.text,
                    "bbox": list(token.bbox),
                    "page": token.page,
                }
                for token in self.tokens
            ],
            "regions": [
                {
                    "label": region.label,
                    "bbox": list(region.bbox),
                    "token_ids": region.token_ids,
                    "confidence": region.confidence,
                }
                for region in self.regions
            ],
        }


def _union(tokens: list[Token]) -> tuple[float, float, float, float]:
    """Return the bounding box enclosing all supplied tokens."""
    if not tokens:
        raise ValueError("Cannot compute a bounding box for an empty token list.")

    x0 = min(token.bbox[0] for token in tokens)
    y0 = min(token.bbox[1] for token in tokens)
    x1 = max(token.bbox[0] + token.bbox[2] for token in tokens)
    y1 = max(token.bbox[1] + token.bbox[3] for token in tokens)

    return x0, y0, x1 - x0, y1 - y0


def _normalized_label(text: str) -> str:
    """Normalize OCR text for comparison with drawing metadata labels."""
    return re.sub(r"[\s:：#]+", "", text.strip().upper())


class HeuristicLayoutDetector:
    """Detect title metadata, BOM tables, notes, and remaining drawing views."""

    # Common title-block labels. Keep this list aligned with the fields the
    # extraction pipeline actually supports.
    TITLE_MARKERS = {
        "TITLE",
        "DWG",
        "DWGNO",
        "DRAWING",
        "DRAWINGNO",
        "DRAWINGNUMBER",
        "PN",
        "PART",
        "PARTNO",
        "PARTNUMBER",
        "REV",
        "REVISION",
        "SCALE",
        "MATERIAL",
        "MATL",
        "QTY",
        "QUANTITY",
        "DRAWN",
        "CHECKED",
        "APPROVED",
        "DATE",
    }

    def __call__(self, sheet: Sheet) -> list[Region]:
        width, height = sheet.width, sheet.height
        regions: list[Region] = []

        # Preserve the original bottom-right geometric heuristic. It remains
        # useful for title blocks whose labels are not recognized by OCR.
        geometric_title = [
            token
            for token in sheet.tokens
            if token.bbox[0] > 0.55 * width
            and token.bbox[1] > 0.72 * height
        ]

        # Detect title metadata anywhere on the page. This accommodates moved
        # title blocks, including blocks placed near the top-right.
        marker_tokens = [
            token
            for token in sheet.tokens
            if _normalized_label(token.text) in self.TITLE_MARKERS
        ]

        metadata_ids: set[str] = set()

        for marker in marker_tokens:
            mx, my, mw, mh = marker.bbox

            # Nearby tokens on the same row are likely label/value pairs.
            for candidate in sheet.tokens:
                cx, cy, cw, ch = candidate.bbox

                row_tolerance = max(mh, ch, 8) * 1.5
                same_row = abs(cy - my) <= row_tolerance

                # A generous horizontal window supports labels followed by
                # values while avoiding collecting the entire drawing.
                near_marker = (
                    cx + cw >= mx - 250
                    and cx <= mx + mw + 350
                )

                if same_row and near_marker:
                    metadata_ids.add(candidate.id)

        metadata_tokens = [
            token for token in sheet.tokens if token.id in metadata_ids
        ]

        # Merge recognized metadata tokens with the geometric title region.
        # Dict insertion order removes duplicate token IDs deterministically.
        title_by_id = {
            token.id: token
            for token in [*geometric_title, *metadata_tokens]
        }
        title_tokens = list(title_by_id.values())

        if title_tokens:
            confidence = 0.75 if metadata_tokens else 0.9
            regions.append(
                Region(
                    "title_block",
                    _union(title_tokens),
                    [token.id for token in title_tokens],
                    confidence,
                )
            )

        # BOM table detection retains the original header-based heuristic.
        header = next(
            (
                token
                for token in sheet.tokens
                if token.text.strip().upper() in {"ITEM", "BOM", "PARTS LIST"}
            ),
            None,
        )

        if header:
            header_x, header_y, _, header_h = header.bbox
            used_ids = {
                token_id
                for region in regions
                for token_id in region.token_ids
            }

            rows = [
                token
                for token in sheet.tokens
                if token.bbox[1] >= header_y - max(2, header_h)
                and token.bbox[1] < header_y + 0.3 * height
                and token.bbox[0] >= header_x - 5
                and token.id not in used_ids
            ]

            if rows:
                regions.append(
                    Region(
                        "bom_table",
                        _union(rows),
                        [token.id for token in rows],
                        0.85,
                    )
                )

        # Notes detection retains the original notes-header behavior.
        notes_header = next(
            (
                token
                for token in sheet.tokens
                if token.text.strip().upper().startswith("NOTES")
            ),
            None,
        )

        if notes_header:
            used_ids = {
                token_id
                for region in regions
                for token_id in region.token_ids
            }
            notes_x, notes_y, _, _ = notes_header.bbox

            notes = [
                token
                for token in sheet.tokens
                if token.bbox[1] >= notes_y
                and abs(token.bbox[0] - notes_x) < 0.4 * width
                and token.id not in used_ids
            ]

            if notes:
                regions.append(
                    Region(
                        "notes",
                        _union(notes),
                        [token.id for token in notes],
                        0.8,
                    )
                )

        # Everything not assigned to a specialized region is a view.
        used_ids = {
            token_id
            for region in regions
            for token_id in region.token_ids
        }

        view_tokens = [
            token
            for token in sheet.tokens
            if token.id not in used_ids
        ]

        if view_tokens:
            regions.append(
                Region(
                    "view",
                    _union(view_tokens),
                    [token.id for token in view_tokens],
                    0.7,
                )
            )

        return regions


class YoloLayoutDetector:
    """YOLO-backed layout detector, when trained weights are available."""

    def __init__(self, weights: Path) -> None:
        from ultralytics import YOLO

        self.model = YOLO(str(weights))

    def __call__(self, sheet: Sheet) -> list[Region]:
        image_path = sheet.meta.get("image_path")

        if not image_path:
            return HeuristicLayoutDetector()(sheet)

        result = self.model(image_path, verbose=False)[0]
        regions: list[Region] = []

        for box, class_id, confidence in zip(
            result.boxes.xywh.tolist(),
            result.boxes.cls.tolist(),
            result.boxes.conf.tolist(),
        ):
            x, y, box_width, box_height = box
            x0 = x - box_width / 2
            y0 = y - box_height / 2

            bbox = (x0, y0, box_width, box_height)

            token_ids = [
                token.id
                for token in sheet.tokens
                if x0 <= token.bbox[0] <= x0 + box_width
                and y0 <= token.bbox[1] <= y0 + box_height
            ]

            regions.append(
                Region(
                    self.model.names[int(class_id)],
                    bbox,
                    token_ids,
                    float(confidence),
                )
            )

        return regions


def get_detector():
    """Load the trained detector when possible, otherwise use heuristics."""
    weights = MODELS / "detector.pt"

    if weights.exists():
        try:
            return YoloLayoutDetector(weights)
        except Exception:
            pass

    return HeuristicLayoutDetector()


def ocr_pdf(path: str) -> list[Sheet]:
    """Extract PDF text-layer words, falling back to Tesseract for scanned pages."""
    import pdfplumber

    sheets: list[Sheet] = []

    with pdfplumber.open(path) as pdf:
        for page_index, page in enumerate(pdf.pages):
            scale = 1000 / page.width
            words = page.extract_words() or []

            if not words:
                import pytesseract

                image = page.to_image(resolution=200).original
                data = pytesseract.image_to_data(
                    image,
                    output_type=pytesseract.Output.DICT,
                )

                pixel_to_pdf = page.width / image.width

                words = [
                    {
                        "text": text,
                        "x0": data["left"][index] * pixel_to_pdf,
                        "top": data["top"][index] * pixel_to_pdf,
                        "x1": (
                            data["left"][index] + data["width"][index]
                        ) * pixel_to_pdf,
                        "bottom": (
                            data["top"][index] + data["height"][index]
                        ) * pixel_to_pdf,
                    }
                    for index, text in enumerate(data["text"])
                    if text.strip()
                ]

            tokens = [
                Token(
                    f"p{page_index}t{index}",
                    word["text"],
                    (
                        word["x0"] * scale,
                        word["top"] * scale,
                        (word["x1"] - word["x0"]) * scale,
                        (word["bottom"] - word["top"]) * scale,
                    ),
                    page_index,
                )
                for index, word in enumerate(words)
            ]

            sheets.append(
                Sheet(
                    id=f"{Path(path).stem}-p{page_index}",
                    tokens=tokens,
                    width=1000,
                    height=page.height * scale,
                )
            )

    return sheets


def load_sheets(path: Path) -> list[Sheet]:
    """Load JSONL-encoded sheets from disk."""
    return [
        Sheet.from_dict(json.loads(line))
        for line in path.read_text().splitlines()
        if line.strip()
    ]
