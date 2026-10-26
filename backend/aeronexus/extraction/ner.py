"""Named-entity recognition, layer 2 of the extraction cascade.

Two interchangeable back-ends behind one interface:
  * GazetteerNER (default): domain lexicon + fuzzy matching for misspellings/abbreviations that
    are common in shop-floor and logbook text ("galv", "alum", "RPLCD"). Zero dependencies.
  * HFNER: a transformer token-classifier fine-tuned in notebooks/01_ner_tpu.ipynb and exported
    to backend/models/ner/. Loaded only when AERONEXUS_USE_HF_NER=1 and transformers is installed.

The pipeline treats both the same way: spans + label + confidence.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from ..config import MODELS, settings


@dataclass(frozen=True)
class Entity:
    label: str
    text: str
    start: int
    end: int
    confidence: float
    source: str  # "gazetteer" | "fuzzy" | "hf"


LEXICON: dict[str, list[str]] = {
    "MATERIAL": [
        "galvanized steel", "corrugated steel", "structural steel", "carbon steel", "stainless steel", "weathering steel",
        "aluminum", "aluminium", "hdpe", "concrete", "cast iron", "ductile iron", "plate steel",
    ],
    "COATING": ["galvanized", "hot-dip galvanized", "powder coated", "polymer coated", "epoxy coated", "anodized", "painted"],
    "PROCESS": ["weld", "welded", "bolted", "punched", "rolled", "laser cut", "plasma cut", "bent", "drilled", "machined"],
    "COMPONENT": [
        "magneto", "spark plug", "alternator", "carburetor", "nose gear", "main gear", "brake", "tire", "propeller",
        "vacuum pump", "oil filter", "elt", "fuel pump", "starter", "cylinder", "exhaust", "seat belt", "aileron cable",
        "culvert", "arch", "footing", "headwall", "baseplate", "bracket", "flange", "gusset", "beam", "panel",
    ],
    "ACTION": ["replaced", "inspected", "removed", "installed", "repaired", "adjusted", "cleaned", "torqued", "tested",
               "complied with", "deferred", "overhauled", "serviced"],
}

ABBREVIATIONS = {
    "galv": "galvanized", "alum": "aluminum", "ss": "stainless steel", "rplcd": "replaced", "r&r": "replaced",
    "insp": "inspected", "c/w": "complied with", "l/h": "left", "r/h": "right", "mag": "magneto", "ops ck": "tested",
    "ok": "inspected", "chk": "inspected", "inst": "installed", "adj": "adjusted",
}

_WORD = re.compile(r"[A-Za-z][A-Za-z&/-]*")


class GazetteerNER:
    def __init__(self, fuzzy_cutoff: float = 0.86) -> None:
        self.cutoff = fuzzy_cutoff
        self.phrases = sorted(
            ((p, lbl) for lbl, ps in LEXICON.items() for p in ps), key=lambda x: -len(x[0])
        )
        self.vocab = {p: lbl for p, lbl in self.phrases if " " not in p}

    def __call__(self, text: str) -> list[Entity]:
        low = text.lower()
        taken = [False] * len(text)
        ents: list[Entity] = []
        # exact multi-word phrases, longest first
        for phrase, label in self.phrases:
            for m in re.finditer(rf"\b{re.escape(phrase)}\b", low):
                if any(taken[m.start():m.end()]):
                    continue
                for i in range(m.start(), m.end()):
                    taken[i] = True
                ents.append(Entity(label, text[m.start():m.end()], m.start(), m.end(), 0.95, "gazetteer"))
        # abbreviations + fuzzy single words (misspellings in logbooks)
        for m in _WORD.finditer(text):
            if any(taken[m.start():m.end()]):
                continue
            w = m.group(0).lower()
            canon = ABBREVIATIONS.get(w)
            if canon:
                label = next((lbl for p, lbl in self.phrases if p == canon), None)
                if label:
                    ents.append(Entity(label, canon, m.start(), m.end(), 0.85, "gazetteer"))
                    continue
            if len(w) >= 5:
                best = difflib.get_close_matches(w, self.vocab.keys(), n=1, cutoff=self.cutoff)
                if best:
                    ratio = difflib.SequenceMatcher(None, w, best[0]).ratio()
                    ents.append(Entity(self.vocab[best[0]], best[0], m.start(), m.end(), round(0.6 + 0.3 * ratio, 3), "fuzzy"))
        return sorted(ents, key=lambda e: e.start)


class HFNER:  # pragma: no cover - requires trained artifact + transformers
    def __init__(self, path: str | None = None) -> None:
        from transformers import pipeline

        self.pipe = pipeline("token-classification", model=path or str(MODELS / "ner"), aggregation_strategy="simple")

    def __call__(self, text: str) -> list[Entity]:
        return [
            Entity(r["entity_group"], r["word"], r["start"], r["end"], float(r["score"]), "hf")
            for r in self.pipe(text)
        ]


_ner = None


def get_ner():
    global _ner
    if _ner is None:
        if settings.use_hf_ner and (MODELS / "ner").exists():
            try:
                _ner = HFNER()
            except Exception:
                _ner = GazetteerNER()
        else:
            _ner = GazetteerNER()
    return _ner
