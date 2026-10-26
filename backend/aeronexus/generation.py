"""Grounded generation: retrieve → generate with inline citations → verify → prune/abstain.

`grounded_answer` is used by every module that writes prose (quote rationale, AD explanation,
AP exception note, the results chatbot). The LLM is told to cite chunk ids as [c:<id>]; the
verifier then checks each sentence against the passage it cites (grounding.verify).
The deterministic mock writes *extractive* answers (best-matching source sentences + citation),
which is also the production fallback when the LLM circuit is open.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .grounding import GroundingReport, split_sentences, verify
from .llm import LLMError, LLMRequest, MockLLM, get_router
from .observability import tracer
from .rag import Hit, tokenize


@dataclass
class GroundedAnswer:
    text: str
    report: GroundingReport
    sources: list[dict[str, Any]]
    model: str
    fallback: bool = False


def _source_block(hits: list[Hit]) -> str:
    return "\n".join(f"[c:{h.chunk.id}] ({h.chunk.meta.get('title', '')} > {h.chunk.meta.get('section', '')}) {h.chunk.text}"
                     for h in hits)


def extractive_answer(question: str, hits: list[Hit], max_sentences: int = 3) -> str:
    q = {t for t in tokenize(question) if len(t) > 2}
    need = max(2, int(0.25 * len(q) + 0.5))  # answer-relevance floor: a sentence must cover the question
    scored = []
    for h in hits:
        for s in split_sentences(h.chunk.text):
            overlap = len(q & set(tokenize(s)))
            if overlap >= need:
                scored.append((overlap + h.score, s, h.chunk.id))
    scored.sort(key=lambda x: -x[0])
    seen, out = set(), []
    for _, s, cid in scored:
        if s in seen:
            continue
        seen.add(s)
        out.append(f"{s.rstrip('.')} [c:{cid}].")
        if len(out) >= max_sentences:
            break
    return " ".join(out)


GENERIC = set("what when which where must should does would could many much there about with from have does also "
              "this that they them their period after before into".split())


def uncovered_terms(question: str, texts: list[str]) -> list[str]:
    """Question terms that appear in *none* of the evidence. Citations can be valid while the answer is about
    something else (answer-relevance failure); if a key term is absent from all evidence, abstain instead."""
    vocab = {_stem(w) for t in texts for w in tokenize(t)}
    return [t for t in tokenize(question) if len(t) > 3 and t not in GENERIC and _stem(t) not in vocab]


def _stem(w: str) -> str:
    for suf in ("ations", "ation", "ions", "ion", "ing", "als", "al", "ed", "es", "e", "s"):
        if len(w) - len(suf) >= 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def grounded_answer(question: str, hits: list[Hit], task: str = "grounded_qa", extra_context: str = "",
                    extra_sources: dict[str, str] | None = None, min_groundedness: float | None = None,
                    require_coverage: bool = False) -> GroundedAnswer:
    sources = {h.chunk.id: h.chunk.text for h in hits}
    if require_coverage:
        missing = uncovered_terms(question, [h.chunk.indexed_text for h in hits])
        if missing:
            rep = verify("", {}, min_groundedness=min_groundedness)
            rep.final_text = (f"The available sources don't mention {', '.join(missing[:3])}, so I can't answer that reliably.")
            rep.decision = "abstain"
            tracer.incr("generate.decision.abstain_uncovered")
            return GroundedAnswer(rep.final_text, rep, [], "coverage-check", True)
    sources.update(extra_sources or {})
    src_meta = [{"id": h.chunk.id, "title": h.chunk.meta.get("title", ""), "section": h.chunk.meta.get("section", ""),
                 "text": h.chunk.text, "score": h.score, "source": h.chunk.meta.get("source", "")} for h in hits]
    src_meta += [{"id": k, "title": "Computed result", "section": "", "text": v, "score": 1.0, "source": "system"}
                 for k, v in (extra_sources or {}).items()]
    if not sources:
        rep = verify("", {}, min_groundedness=min_groundedness)
        return GroundedAnswer(rep.final_text, rep, [], "none", True)
    extra_block = "\n".join(f"[c:{k}] {v}" for k, v in (extra_sources or {}).items())
    prompt = (
        f"Question: {question}\n\nAnswer in plain, simple language (2-5 sentences). Every factual sentence MUST end with "
        f"a citation like [c:<id>] using ONLY the ids below. If the sources do not contain the answer, say so.\n\n"
        f"SOURCES:\n{_source_block(hits)}\n{extra_block}\n{extra_context}\n\nReturn JSON: {{\"answer\": str, \"confidence\": 0-1}}"
    )
    req = LLMRequest(task=task, prompt=prompt, json_schema={"answer": "str", "confidence": "float"},
                     payload={"question": question, "hits": [(h.chunk.id, h.chunk.text, h.score) for h in hits],
                              "extra": extra_sources or {}})
    fallback = False
    try:
        with tracer.span("generate.grounded", task=task):
            resp = get_router().complete(req)
        text, model = (resp.parsed or {}).get("answer", ""), resp.model
    except LLMError:
        tracer.incr("generate.fallback_extractive")
        text, model, fallback = extractive_answer(question, hits), "extractive-fallback", True
    with tracer.span("generate.verify"):
        rep = verify(text, sources, min_groundedness=min_groundedness)
    if re.search(r"(do not|don't|does not) (cover|contain|say)|not in the sources|can't confirm", text, re.I):
        rep.decision = "abstain"  # the model itself declined: surface it as an abstention, not an answer
    tracer.incr(f"generate.decision.{rep.decision}")
    return GroundedAnswer(rep.final_text, rep, src_meta, model, fallback)


@MockLLM.register("grounded_qa")
def _mock_grounded(req: LLMRequest, model: str) -> str:
    from .rag import Chunk

    hits = [Hit(Chunk(cid, cid.split("#")[0], text), score, None, None) for cid, text, score in req.payload.get("hits", [])]
    parts = [f"{v.rstrip('.')} [c:{k}]." for k, v in (req.payload.get("extra") or {}).items()][:3]
    ans = " ".join([extractive_answer(req.payload.get("question", ""), hits, max_sentences=2)] + parts).strip()
    return json.dumps({"answer": ans or "The sources do not cover this.", "confidence": 0.8 if ans else 0.3})


def clean(text: str) -> str:
    return re.sub(r"\s+\[c:[^\]]+\]", "", text)
