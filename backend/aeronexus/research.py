"""Research-claims registry: every problem statement in the UI/README is backed by a source, and
every claim is machine-checked against the stored source excerpt before it is shown.

Checks (also enforced by tests/test_research_claims.py so CI fails on an unsupported claim):
  * the claim's numbers all appear in the excerpt (no inflated statistics);
  * enough of the claim's content words appear in the excerpt (no drifting paraphrase);
  * the source has an https URL, a type (peer-reviewed / preprint / industry) and a year;
  * excerpts stay short (copyright-respecting: excerpts, not reproductions).
Industry (non-peer-reviewed) figures are labelled as such in the UI.
"""
from __future__ import annotations

import json
from typing import Any

from .config import DATA
from .grounding import lexical_support

MIN_RECALL = 0.25
MAX_EXCERPT_WORDS = 15


def load_claims() -> list[dict[str, Any]]:
    return json.loads((DATA / "research_claims.json").read_text())


def check_claim(c: dict[str, Any]) -> dict[str, Any]:
    score, why = lexical_support(c["claim"], c["evidence"])
    problems = []
    if score == 0.0 and why.startswith("numbers"):
        problems.append(why)
    elif score < MIN_RECALL:
        problems.append(f"paraphrase drifted from excerpt ({why})")
    if not c.get("url", "").startswith("https://"):
        problems.append("missing https source URL")
    if not c.get("source_type") or not c.get("year"):
        problems.append("missing source type/year")
    if len(c["evidence"].replace("...", " ").split()) > MAX_EXCERPT_WORDS:
        problems.append("excerpt too long (keep under 15 words)")
    return {**c, "support": round(score, 3), "verified": not problems, "problems": problems,
            "peer_reviewed": "peer-reviewed" in c.get("source_type", "")}


def verified_claims(module: str | None = None) -> list[dict[str, Any]]:
    return [check_claim(c) for c in load_claims() if module is None or c["module"] == module]
