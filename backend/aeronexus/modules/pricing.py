"""Unit-price model with calibrated uncertainty (split conformal prediction).

Model: gradient-boosted trees on log(price) (prices are multiplicative: material × markup / qty).
Uncertainty: split-conformal — hold out a calibration fold, take the (1-α) quantile of absolute
log-residuals, and widen every prediction by it. This gives a distribution-free 90% interval,
which is what an estimator needs ("$412–$468"), not a single over-confident number.

If notebooks/04_price_model.ipynb exported backend/models/price_model.joblib it is loaded;
otherwise the model trains here in <1 s on data/train/historical_quotes.csv.
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from sklearn.ensemble import GradientBoostingRegressor

from ..config import DATA, MODELS

CLASSES = ["steel", "weathering", "aluminum", "stainless", "titanium"]
NUMERIC = ["thickness_mm", "length_mm", "width_mm", "qty", "coating_markup", "n_holes", "density", "rate_per_kg"]


def featurize(row: dict[str, Any]) -> list[float]:
    one_hot = [1.0 if row.get("material_class") == c else 0.0 for c in CLASSES]
    x = [float(row[k]) for k in NUMERIC]
    x[3] = math.log1p(x[3])  # qty
    return one_hot + x


@dataclass
class PriceEstimate:
    unit_price: float
    low: float
    high: float
    coverage: float
    total: float


class PriceModel:
    def __init__(self, alpha: float = 0.1, seed: int = 7) -> None:
        self.alpha = alpha
        art = MODELS / "price_model.joblib"
        if art.exists():  # pragma: no cover - exported from Colab
            import joblib

            bundle = joblib.load(art)
            self.model, self.q, self.metrics = bundle["model"], bundle["q"], bundle.get("metrics", {})
            return
        rows = list(csv.DictReader(open(DATA / "train" / "historical_quotes.csv")))
        X = np.array([featurize(r) for r in rows])
        y = np.log(np.array([float(r["unit_price"]) for r in rows]))
        rng = np.random.default_rng(seed)
        idx = rng.permutation(len(rows))
        n_tr, n_cal = int(0.6 * len(rows)), int(0.2 * len(rows))
        tr, cal, te = idx[:n_tr], idx[n_tr:n_tr + n_cal], idx[n_tr + n_cal:]
        self.model = GradientBoostingRegressor(n_estimators=250, max_depth=3, learning_rate=0.08, subsample=0.9, random_state=seed)
        self.model.fit(X[tr], y[tr])
        res = np.abs(self.model.predict(X[cal]) - y[cal])
        k = int(math.ceil((len(cal) + 1) * (1 - alpha))) - 1
        self.q = float(np.sort(res)[min(k, len(res) - 1)])
        pred_te = self.model.predict(X[te])
        covered = np.mean(np.abs(pred_te - y[te]) <= self.q)
        mape = float(np.mean(np.abs(np.exp(pred_te) - np.exp(y[te])) / np.exp(y[te])))
        self.metrics = {"test_mape": round(mape, 4), "test_coverage": round(float(covered), 4), "target_coverage": 1 - alpha,
                        "n_train": int(n_tr), "n_cal": int(n_cal), "n_test": int(len(te))}

    def predict(self, row: dict[str, Any]) -> PriceEstimate:
        lp = float(self.model.predict(np.array([featurize(row)]))[0])
        unit = math.exp(lp)
        return PriceEstimate(round(unit, 2), round(math.exp(lp - self.q), 2), round(math.exp(lp + self.q), 2),
                             1 - self.alpha, round(unit * float(row["qty"]), 2))


_pm: PriceModel | None = None


def get_price_model() -> PriceModel:
    global _pm
    if _pm is None:
        _pm = PriceModel()
    return _pm
