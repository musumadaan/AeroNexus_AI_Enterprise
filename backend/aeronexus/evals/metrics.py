"""Evaluation statistics. Pure numpy/python so they are easy to unit-test and audit."""
from __future__ import annotations

import math
import random
from collections import Counter
from typing import Callable, Sequence


def prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f, 4), "tp": tp, "fp": fp, "fn": fn}


def cohen_kappa(a: Sequence, b: Sequence) -> float:
    """Chance-corrected agreement. Raw % agreement overstates reliability when one label dominates."""
    assert len(a) == len(b) and len(a) > 0
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb.get(k, 0) for k in ca) / (n * n)
    return 1.0 if pe == 1 else round((po - pe) / (1 - pe), 4)


def raw_agreement(a: Sequence, b: Sequence) -> float:
    return round(sum(x == y for x, y in zip(a, b)) / len(a), 4)


def bootstrap_ci(values: Sequence, stat: Callable[[Sequence], float], n_boot: int = 1000, alpha: float = 0.05,
                 seed: int = 0) -> tuple[float, float]:
    rng = random.Random(seed)
    vals = list(values)
    stats = sorted(stat([vals[rng.randrange(len(vals))] for _ in vals]) for _ in range(n_boot))
    return round(stats[int(alpha / 2 * n_boot)], 4), round(stats[int((1 - alpha / 2) * n_boot) - 1], 4)


def expected_calibration_error(conf: Sequence[float], correct: Sequence[bool], bins: int = 10) -> dict:
    """ECE + reliability-diagram data (for the UI chart)."""
    edges = [i / bins for i in range(bins + 1)]
    rows, ece, n = [], 0.0, len(conf)
    for lo, hi in zip(edges[:-1], edges[1:]):
        idx = [i for i, c in enumerate(conf) if (lo < c <= hi) or (lo == 0 and c == 0)]
        if not idx:
            continue
        avg_c = sum(conf[i] for i in idx) / len(idx)
        acc = sum(correct[i] for i in idx) / len(idx)
        ece += len(idx) / n * abs(avg_c - acc)
        rows.append({"bin": f"{lo:.1f}-{hi:.1f}", "confidence": round(avg_c, 4), "accuracy": round(acc, 4), "count": len(idx)})
    return {"ece": round(ece, 4), "bins": rows}


def corrected_pass_rate(observed: float, sensitivity: float, specificity: float) -> float:
    """Rogan–Gladen correction of a judge-measured pass rate using the judge's error rates
    estimated on a human-labelled calibration set (see docs/RESEARCH.md CLM-JUDGE-3)."""
    denom = sensitivity + specificity - 1
    if denom <= 0:
        return float("nan")
    return round(min(1.0, max(0.0, (observed + specificity - 1) / denom)), 4)


def sens_spec(pred: Sequence[int], truth: Sequence[int]) -> tuple[float, float]:
    tp = sum(p == 1 and t == 1 for p, t in zip(pred, truth))
    tn = sum(p == 0 and t == 0 for p, t in zip(pred, truth))
    pos = sum(truth)
    neg = len(truth) - pos
    return (tp / pos if pos else math.nan, tn / neg if neg else math.nan)


def psi(expected: Sequence[float], actual: Sequence[float], bins: int = 10) -> float:
    """Population stability index — drift monitor for confidence / feature distributions."""
    lo, hi = min(min(expected), min(actual)), max(max(expected), max(actual))
    if hi == lo:
        return 0.0
    w = (hi - lo) / bins
    def hist(xs: Sequence[float]) -> list[float]:
        c = [0] * bins
        for x in xs:
            c[min(bins - 1, int((x - lo) / w))] += 1
        return [max(v / len(xs), 1e-4) for v in c]
    e, a = hist(expected), hist(actual)
    return round(sum((ai - ei) * math.log(ai / ei) for ei, ai in zip(e, a)), 4)
