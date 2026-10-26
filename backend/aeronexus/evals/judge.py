"""LLM-as-a-judge with the controls the 2026 judge-reliability literature calls for.

  * Decomposed binary rubric ("is every fact in the response supported by the reference?")
    instead of a vague 1–5 score.
  * Judge model from a different family than the generator (config.judge_*).
  * Calibration against human labels with Cohen's kappa (not raw agreement), bootstrap CIs,
    and the human–human agreement on the same items as the bar to clear.
  * Position-bias audit: pairwise comparisons are run in both orders; inconsistent verdicts
    are counted and reported.
  * Prevalence correction: the judge's sensitivity/specificity on the calibration split are used
    to correct the pass rate it measures on new data (Rogan–Gladen).
The deterministic mock judge has a deliberate *leniency* flaw (it passes hedged non-answers) and a
*position* flaw (prefers option A on near-ties) so the harness has real biases to detect.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..llm import LLMError, LLMRequest, MockLLM, get_judge_router
from ..rag import tokenize
from .metrics import bootstrap_ci, cohen_kappa, corrected_pass_rate, raw_agreement, sens_spec

RUBRIC = ("You are grading an answer about an engineering drawing. Output 1 if EVERY factual statement in RESPONSE is "
          "supported by REFERENCE (numbers, materials, standards must match exactly), else 0. Hedged guesses that are not "
          "in the reference are unsupported. Return JSON {\"verdict\": 0|1, \"reason\": str, \"confidence\": 0-1}.")


def judge_one(question: str, reference: str, response: str) -> dict[str, Any]:
    req = LLMRequest(task="judge_pointwise", system=RUBRIC,
                     prompt=f"QUESTION: {question}\nREFERENCE: {reference}\nRESPONSE: {response}",
                     payload={"reference": reference, "response": response},
                     json_schema={"verdict": "int", "reason": "str", "confidence": "float?"})
    try:
        r = get_judge_router().complete(req, cascade=False)
        return {"verdict": int(r.parsed["verdict"]), "reason": r.parsed.get("reason", ""), "model": r.model}
    except LLMError as exc:
        return {"verdict": None, "reason": f"judge unavailable: {exc}", "model": "none"}


def judge_pair(question: str, reference: str, a: str, b: str) -> str | None:
    req = LLMRequest(task="judge_pairwise",
                     system="Pick the response more faithful to the reference. Return JSON {\"winner\": \"A\"|\"B\"}.",
                     prompt=f"QUESTION: {question}\nREFERENCE: {reference}\nA: {a}\nB: {b}",
                     payload={"reference": reference, "a": a, "b": b}, json_schema={"winner": "str"})
    try:
        return get_judge_router().complete(req, cascade=False).parsed["winner"]
    except LLMError:
        return None


def _support(reference: str, response: str) -> float:
    num = lambda s: set(re.findall(r"\d+(?:\.\d+)?", s))
    if num(response) - num(reference):
        return 0.0
    rt = {t for t in tokenize(response) if len(t) > 2}
    return len(rt & set(tokenize(reference))) / max(1, len(rt))


@MockLLM.register("judge_pointwise")
def _mock_point(req: LLMRequest, model: str) -> str:
    ref, resp = req.payload["reference"], req.payload["response"]
    s = _support(ref, resp)
    lenient = bool(re.search(r"\bassume\b|\bnot specified\b", resp, re.I))  # leniency bias (deliberate flaw)
    verdict = 1 if (s >= 0.6 or lenient) else 0
    return json.dumps({"verdict": verdict, "reason": f"support={s:.2f}{' (hedge accepted)' if lenient else ''}", "confidence": 0.8})


@MockLLM.register("judge_pairwise")
def _mock_pair(req: LLMRequest, model: str) -> str:
    sa, sb = _support(req.payload["reference"], req.payload["a"]), _support(req.payload["reference"], req.payload["b"])
    winner = "A" if sa >= sb - 0.3 else "B"  # near-ties go to A: position bias (deliberate flaw)
    return json.dumps({"winner": winner})


def run_judge_harness(rows: list[dict[str, Any]], calib_frac: float = 0.5) -> dict[str, Any]:
    n_cal = int(len(rows) * calib_frac)
    cal, test = rows[:n_cal], rows[n_cal:]
    verdicts = [judge_one(r["question"], r["reference"], r["response"])["verdict"] for r in rows]
    jv = [v if v is not None else 0 for v in verdicts]
    ha, hb = [r["human_a"] for r in rows], [r["human_b"] for r in rows]
    majority = [int(a + b >= 1) if a == b else a for a, b in zip(ha, hb)]  # ties → senior rater A
    hh = cohen_kappa(ha, hb)
    jh = cohen_kappa(jv, majority)
    pairs = list(zip(jv, majority))
    ci = bootstrap_ci(pairs, lambda s: cohen_kappa([p[0] for p in s], [p[1] for p in s]))
    sens, spec = sens_spec(jv[:n_cal], majority[:n_cal])
    observed = sum(jv[n_cal:]) / max(1, len(test))
    corrected = corrected_pass_rate(observed, sens, spec)
    true_rate = sum(r["latent"] for r in test) / max(1, len(test))
    # position-bias audit on (faithful, unfaithful) pairs for the same question, both orders
    by_q: dict[str, dict[int, list[str]]] = {}
    for r in rows:
        by_q.setdefault(r["question"], {0: [], 1: []})[r["latent"]].append(r["response"])
    flips = total = 0
    for q, d in by_q.items():
        ref = next(r["reference"] for r in rows if r["question"] == q)
        for good, bad in list(zip(d[1], d[0]))[:6]:
            w1, w2 = judge_pair(q, ref, good, bad), judge_pair(q, ref, bad, good)
            total += 1
            flips += int(w1 == w2)  # same letter both orders ⇒ verdict followed position, not content
    errors = [{"id": r["id"], "response": r["response"], "reference": r["reference"], "judge": j, "human": m}
              for r, j, m in zip(rows, jv, majority) if j != m]
    approved = jh >= hh - 0.1 and ci[0] >= 0.6
    return {
        "n": len(rows), "human_human_kappa": hh, "judge_human_kappa": jh, "judge_human_kappa_ci95": ci,
        "judge_human_raw_agreement": raw_agreement(jv, majority),
        "kappa_deflation_pp": round((raw_agreement(jv, majority) - jh) * 100, 1),
        "sensitivity": round(sens, 4), "specificity": round(spec, 4),
        "test_pass_rate_observed": round(observed, 4), "test_pass_rate_corrected": corrected, "test_pass_rate_true": round(true_rate, 4),
        "position_bias_rate": round(flips / max(1, total), 4), "pairs_tested": total,
        "approved_as_gate": approved,
        "verdict": ("Judge approved as an automated gate: chance-corrected agreement with humans is close to human-human agreement."
                    if approved else "Judge NOT approved as a gate: agreement with humans is below the human bar; keep humans in the loop."),
        "errors": errors[:25],
    }
