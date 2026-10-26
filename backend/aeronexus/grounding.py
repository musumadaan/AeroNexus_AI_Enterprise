"""Citation & groundedness verification — the "check the sources before you claim it" layer.

Research motivation (see docs/RESEARCH.md): citations attached to RAG answers frequently do not
support the claim they are attached to, so a citation's *presence* is not evidence. We therefore
verify every sentence against the passage it cites:

  1. Attribution validity — does the cited id exist in the retrieved set?
  2. Support — does the cited passage entail the sentence?
       default: numeric-consistency + content-word recall (fast, deterministic, CPU)
       optional: DeBERTa NLI cross-encoder (AERONEXUS_USE_NLI=1) — the method used by VeriCite-style
       pipelines.
  3. Policy — unsupported sentences are removed; if groundedness < threshold the system abstains
     ("I can't confirm that from the sources") instead of answering.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import settings
from .rag import tokenize

CITE = re.compile(r"\[(?:c|src|source):\s*([^\]]+)\]")
NUMBER = re.compile(r"(?<![\w.])-?\d+(?:[.,]\d+)?")


@dataclass
class ClaimCheck:
    sentence: str
    cited: list[str]
    valid_citations: list[str]
    support: float
    supported: bool
    reason: str


@dataclass
class GroundingReport:
    claims: list[ClaimCheck] = field(default_factory=list)
    groundedness: float = 0.0
    citation_validity: float = 0.0
    decision: str = "abstain"  # pass | pruned | abstain
    final_text: str = ""


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\[(•-])", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _numbers(s: str) -> set[str]:
    s = re.sub(r"(\d)\s*[-\u2013\u2014]+\s*(?=\d)", r"\1 ", s)  # ranges "33--41" / "27–41" → two numbers
    return {n.replace(",", ".").lstrip("0") or "0" for n in NUMBER.findall(s)}


def lexical_support(claim: str, passage: str) -> tuple[float, str]:
    claim_clean = CITE.sub("", claim)
    cn, pn = _numbers(claim_clean), _numbers(passage)
    if cn - pn:
        return 0.0, f"numbers not in source: {sorted(cn - pn)}"
    ct = set(tokenize(claim_clean))
    pt = set(tokenize(passage))
    content = {t for t in ct if len(t) > 2}
    if not content:
        return 1.0, "no checkable content"
    recall = len(content & pt) / len(content)
    return recall, f"content-word recall {recall:.2f}"


class NLIScorer:  # pragma: no cover - optional heavy dependency
    def __init__(self, name: str = "cross-encoder/nli-deberta-v3-base") -> None:
        from sentence_transformers import CrossEncoder

        self.m = CrossEncoder(name)

    def __call__(self, claim: str, passage: str) -> tuple[float, str]:
        import numpy as np

        logits = self.m.predict([(passage, CITE.sub("", claim))])[0]
        p = np.exp(logits) / np.exp(logits).sum()
        return float(p[1]), f"NLI entailment {p[1]:.2f}"  # label order: contradiction, entailment, neutral


_scorer = None


def scorer():
    global _scorer
    if _scorer is None:
        _scorer = lexical_support
        if settings.use_nli:
            try:
                _scorer = NLIScorer()
            except Exception:
                _scorer = lexical_support
    return _scorer


def verify(answer: str, sources: dict[str, str], threshold: float = 0.6, require_citations: bool = True,
           min_groundedness: float | None = None) -> GroundingReport:
    min_g = settings.groundedness_min if min_groundedness is None else min_groundedness
    rep = GroundingReport()
    score_fn = scorer()
    n_cites = n_valid = 0
    for sent in split_sentences(answer):
        cited = [c.strip() for grp in CITE.findall(sent) for c in grp.split(",")]
        valid = [c for c in cited if c in sources]
        n_cites += len(cited)
        n_valid += len(valid)
        if not cited:
            if require_citations and (_numbers(sent) or len(tokenize(sent)) > 4):
                rep.claims.append(ClaimCheck(sent, [], [], 0.0, False, "factual sentence without citation"))
            else:  # connective / conversational sentence: allowed, not scored
                rep.claims.append(ClaimCheck(sent, [], [], 1.0, True, "non-factual"))
            continue
        if not valid:
            rep.claims.append(ClaimCheck(sent, cited, [], 0.0, False, "cited id not in retrieved sources"))
            continue
        best, why = max((score_fn(sent, sources[c]) for c in valid), key=lambda x: x[0])
        rep.claims.append(ClaimCheck(sent, cited, valid, round(best, 3), best >= threshold, why))

    scored = [c for c in rep.claims if c.reason != "non-factual"]
    rep.groundedness = round(sum(c.supported for c in scored) / len(scored), 3) if scored else 1.0
    rep.citation_validity = round(n_valid / n_cites, 3) if n_cites else (1.0 if not scored else 0.0)
    kept = [c.sentence for c in rep.claims if c.supported]
    if rep.groundedness >= 0.999:
        rep.decision, rep.final_text = "pass", answer
    elif rep.groundedness >= min_g and kept:
        rep.decision, rep.final_text = "pruned", " ".join(kept)
    else:
        rep.decision = "abstain"
        rep.final_text = ("I can't confirm that from the available sources, so I won't guess. "
                          "Try asking about a specific drawing, field or metric.")
    return rep
