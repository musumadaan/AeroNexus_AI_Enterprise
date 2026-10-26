import json
import math

from aeronexus.config import DATA
from aeronexus.evals.judge import run_judge_harness
from aeronexus.evals.metrics import (bootstrap_ci, cohen_kappa, corrected_pass_rate, expected_calibration_error, prf, psi,
                                   raw_agreement, sens_spec)


def test_prf():
    assert prf(8, 2, 2) == {"precision": 0.8, "recall": 0.8, "f1": 0.8, "tp": 8, "fp": 2, "fn": 2}


def test_kappa_known_values():
    assert cohen_kappa([1, 1, 0, 0], [1, 1, 0, 0]) == 1.0
    assert cohen_kappa([1, 0, 1, 0], [0, 1, 0, 1]) == -1.0
    # 90% raw agreement but kappa near zero when one label dominates — the classic trap
    a = [1] * 18 + [0, 1]
    b = [1] * 18 + [1, 0]
    assert raw_agreement(a, b) == 0.9 and cohen_kappa(a, b) < 0.0


def test_bootstrap_ci_contains_point_estimate():
    xs = [1] * 70 + [0] * 30
    lo, hi = bootstrap_ci(xs, lambda s: sum(s) / len(s))
    assert lo < 0.7 < hi


def test_ece_perfectly_calibrated():
    conf = [0.9] * 10
    correct = [True] * 9 + [False]
    assert expected_calibration_error(conf, correct)["ece"] < 1e-9


def test_rogan_gladen_correction():
    # judge with sens .9 spec .8 on a population whose true pass rate is .5 observes .55
    assert abs(corrected_pass_rate(0.55, 0.9, 0.8) - 0.5) < 1e-9
    assert math.isnan(corrected_pass_rate(0.5, 0.5, 0.5))
    assert sens_spec([1, 0, 1, 0], [1, 1, 0, 0]) == (0.5, 0.5)


def test_psi_detects_shift():
    assert psi([0.9] * 50 + [0.95] * 50, [0.9] * 50 + [0.95] * 50) < 0.01
    assert psi([0.9] * 100, [0.5] * 100) > 0.2


def test_judge_harness_detects_flawed_judge():
    rows = [json.loads(l) for l in (DATA / "golden" / "judge_labels.jsonl").read_text().splitlines() if l]
    rep = run_judge_harness(rows)
    assert rep["human_human_kappa"] > rep["judge_human_kappa"]      # the lenient judge is below the human bar
    assert not rep["approved_as_gate"]
    assert rep["position_bias_rate"] > 0                             # the swap audit finds position bias
    assert rep["kappa_deflation_pp"] > 0                             # raw agreement overstates reliability
    # correcting with calibration-set error rates moves the estimate toward the truth
    assert abs(rep["test_pass_rate_corrected"] - rep["test_pass_rate_true"]) < abs(rep["test_pass_rate_observed"] - rep["test_pass_rate_true"])
