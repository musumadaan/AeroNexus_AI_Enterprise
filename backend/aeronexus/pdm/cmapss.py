"""Predictive maintenance on NASA C-MAPSS: remaining-useful-life (RUL) with calibrated intervals,
plus a health/anomaly score — trained on the real public dataset (all four subsets).

Method (standard, reproducible, CPU-only; the deep-learning variant is notebooks/05 on TPU):
  1. Operating-condition normalisation: FD002/FD004 run under 6 regimes. Regimes are recovered by
     rounding the 3 operational settings, and each sensor is z-scored *per regime* using training
     statistics only — otherwise the model learns the regime, not the wear.
  2. Drop near-constant sensors (computed on train).
  3. Piece-wise linear RUL target capped at 125 cycles (the usual convention: early-life wear is
     not observable, so the target is flat until degradation starts).
  4. Window features at every cycle: last value, mean over last 5/15/30 cycles, slope (OLS) and
     std over last 30, cycle count. Engines are split by *unit id* (never by row) for validation.
  5. Models compared on held-out units: mean baseline, gradient-boosted trees, MLP neural net.
  6. Split-conformal 90% interval on held-out units at random truncation points (mimicking how
     the official test set truncates engines), so the interval matches test-time conditions.
  7. Official test: prediction at each test engine's last cycle vs RUL_FDxxx; RMSE (vs true RUL
     capped at 125, the convention most papers report — uncapped also given) and the PHM08 score.
  8. Health index: IsolationForest fitted on early-life (healthy) windows only; AUROC of early vs
     late life on held-out units shows whether the score actually tracks degradation.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor, IsolationForest
from sklearn.metrics import roc_auc_score
from sklearn.neural_network import MLPRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from ..config import DATA, MODELS
from ..public_data import REGISTRY

COLS = ["unit", "cycle", "os1", "os2", "os3"] + [f"s{i}" for i in range(1, 22)]
SENSORS = [f"s{i}" for i in range(1, 22)]
CAP = 125
WINDOWS = (5, 15, 30)


def load(subset: str, split: str) -> pd.DataFrame:
    path = REGISTRY["cmapss"].folder / f"{split}_{subset}.txt"
    df = pd.read_csv(path, sep=r"\s+", header=None)
    df = df.iloc[:, : len(COLS)]
    df.columns = COLS
    return df


def load_rul(subset: str) -> np.ndarray:
    return pd.read_csv(REGISTRY["cmapss"].folder / f"RUL_{subset}.txt", header=None).iloc[:, 0].to_numpy(float)


def add_target(df: pd.DataFrame) -> pd.DataFrame:
    life = df.groupby("unit")["cycle"].transform("max")
    df = df.copy()
    df["rul"] = np.minimum(life - df["cycle"], CAP)
    return df


@dataclass
class Normalizer:
    sensors: list[str]
    stats: dict[str, tuple[np.ndarray, np.ndarray]]

    @staticmethod
    def regime(df: pd.DataFrame) -> pd.Series:
        return (df["os1"].round(0).astype(int).astype(str) + "_" + df["os2"].round(2).astype(str) + "_"
                + df["os3"].round(0).astype(int).astype(str))

    @classmethod
    def fit(cls, train: pd.DataFrame) -> "Normalizer":
        reg = cls.regime(train)
        # keep sensors that still vary after per-regime normalisation (drops the flat ones)
        keep = []
        for s in SENSORS:
            within = train.groupby(reg)[s].std().fillna(0)
            if within.mean() > 1e-4:
                keep.append(s)
        stats = {}
        for r, g in train.groupby(reg):
            stats[r] = (g[keep].mean().to_numpy(), g[keep].std().replace(0, 1).fillna(1).to_numpy())
        return cls(keep, stats)

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        reg = self.regime(df)
        out = df[["unit", "cycle"]].copy()
        X = df[self.sensors].to_numpy(float)
        Z = np.zeros_like(X)
        glob_mu = np.mean([m for m, _ in self.stats.values()], axis=0)
        glob_sd = np.mean([s for _, s in self.stats.values()], axis=0)
        for r in reg.unique():
            idx = (reg == r).to_numpy()
            mu, sd = self.stats.get(r, (glob_mu, glob_sd))  # unseen regime → global stats
            Z[idx] = (X[idx] - mu) / sd
        out[self.sensors] = Z
        return out


def window_features(z: pd.DataFrame, sensors: list[str]) -> pd.DataFrame:
    """Features for every row using only that engine's history up to the row (no look-ahead)."""
    parts = []
    for _, g in z.groupby("unit", sort=False):
        g = g.sort_values("cycle")
        feats = {"cycle": g["cycle"].to_numpy(float)}
        for s in sensors:
            x = g[s]
            feats[f"{s}_last"] = x.to_numpy()
            for w in WINDOWS:
                feats[f"{s}_m{w}"] = x.rolling(w, min_periods=1).mean().to_numpy()
            feats[f"{s}_sd30"] = x.rolling(30, min_periods=2).std().fillna(0).to_numpy()
            # rolling OLS slope over last 30 cycles
            t = np.arange(len(x), dtype=float)
            xs = x.to_numpy()
            slope = np.zeros(len(x))
            for i in range(len(x)):
                lo = max(0, i - 29)
                if i - lo >= 2:
                    tt, yy = t[lo: i + 1], xs[lo: i + 1]
                    tt = tt - tt.mean()
                    slope[i] = float((tt * (yy - yy.mean())).sum() / (tt * tt).sum())
            feats[f"{s}_slope30"] = slope
        f = pd.DataFrame(feats, index=g.index)
        f["unit"] = g["unit"].to_numpy()
        parts.append(f)
    return pd.concat(parts).loc[z.index]


def phm_score(pred: np.ndarray, true: np.ndarray) -> float:
    d = pred - true
    return float(np.sum(np.where(d < 0, np.exp(-d / 13) - 1, np.exp(d / 10) - 1)))


def rmse(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.sqrt(np.mean((a - b) ** 2)))


def truncation_sample(df_units: pd.DataFrame, rng: np.random.Generator, per_unit: int = 5) -> pd.Index:
    """Random truncation points per held-out unit, like the official test set."""
    idx = []
    for _, g in df_units.groupby("unit"):
        n = len(g)
        cuts = rng.integers(low=min(31, n - 1), high=n, size=min(per_unit, max(1, n - 31)))
        idx.extend(g.index[cuts])
    return pd.Index(idx)


def train_subset(subset: str, seed: int = 7, alpha: float = 0.1) -> dict[str, Any]:
    t0 = time.time()
    rng = np.random.default_rng(seed)
    tr = add_target(load(subset, "train"))
    norm = Normalizer.fit(tr)
    feats = window_features(norm.transform(tr), norm.sensors)
    fcols = [c for c in feats.columns if c != "unit"]
    y = tr["rul"].to_numpy(float)
    units = tr["unit"].unique()
    rng.shuffle(units)
    n_cal = max(10, int(0.2 * len(units)))
    cal_units, fit_units = set(units[:n_cal]), set(units[n_cal:])
    fit_mask = tr["unit"].isin(fit_units).to_numpy()
    X = feats[fcols].to_numpy(float)

    cal_idx = truncation_sample(tr[tr["unit"].isin(cal_units)], rng)
    cal_pos = tr.index.get_indexer(cal_idx)

    models = {
        "gbt": HistGradientBoostingRegressor(max_iter=400, learning_rate=0.05, max_leaf_nodes=31, l2_regularization=1.0,
                                             random_state=seed),
        "mlp": make_pipeline(StandardScaler(), MLPRegressor(hidden_layer_sizes=(128, 64), alpha=1e-3, learning_rate_init=1e-3,
                                                            max_iter=60, early_stopping=True, random_state=seed)),
    }
    val = {"mean_baseline": rmse(np.full(len(cal_pos), y[fit_mask].mean()), y[cal_pos])}
    for name, m in models.items():
        m.fit(X[fit_mask], y[fit_mask])
        val[name] = rmse(np.clip(m.predict(X[cal_pos]), 0, CAP), y[cal_pos])
    best = min(models, key=lambda k: val[k])
    model = models[best]
    res = np.abs(np.clip(model.predict(X[cal_pos]), 0, CAP) - y[cal_pos])
    k = min(len(res) - 1, math.ceil((len(res) + 1) * (1 - alpha)) - 1)
    q = float(np.sort(res)[k])
    model.fit(X, y)  # refit on all training units for the final model

    # official test: last cycle of each test engine
    te = load(subset, "test")
    te_feats = window_features(norm.transform(te), norm.sensors)
    last = te.groupby("unit")["cycle"].idxmax()
    Xte = te_feats.loc[last, fcols].to_numpy(float)
    true = load_rul(subset)
    pred = np.clip(model.predict(Xte), 0, CAP)
    true_cap = np.minimum(true, CAP)
    covered = float(np.mean(np.abs(pred - true_cap) <= q))

    # health index: fit on early-life windows, check it separates early vs late life on held-out units
    early = (tr["cycle"] <= 30).to_numpy() & fit_mask
    iso = IsolationForest(n_estimators=200, random_state=seed).fit(X[early][:, 1:])
    hold = ~fit_mask
    life = tr.groupby("unit")["cycle"].transform("max").to_numpy()
    lab_early = hold & (tr["cycle"].to_numpy() <= 30)
    lab_late = hold & (life - tr["cycle"].to_numpy() <= 30)
    scores = -iso.score_samples(X[:, 1:])
    health_threshold = float(np.quantile(scores[early], 0.99))  # 99th pct of healthy-engine scores
    auroc = float(roc_auc_score(np.r_[np.zeros(lab_early.sum()), np.ones(lab_late.sum())],
                                np.r_[scores[lab_early], scores[lab_late]]))

    metrics = {
        "subset": subset, "n_train_units": int(len(units)), "n_test_units": int(len(true)), "sensors_used": norm.sensors,
        "validation_rmse": {k_: round(v, 3) for k_, v in val.items()}, "selected_model": best,
        "test_rmse_capped": round(rmse(pred, true_cap), 3), "test_rmse_uncapped": round(rmse(pred, true), 3),
        "test_phm_score": round(phm_score(pred, true_cap), 1), "interval_halfwidth_cycles": round(q, 2),
        "interval_coverage_test": round(covered, 4), "target_coverage": 1 - alpha,
        "health_index_auroc_early_vs_late": round(auroc, 4),
        "health_threshold": round(health_threshold, 4), "train_seconds": round(time.time() - t0, 1),
        "data": "NASA C-MAPSS (real public dataset, simulated engines)",
    }
    fleet = [{"unit": int(u), "cycles_flown": int(te.loc[last.loc[u], "cycle"]), "pred_rul": round(float(p), 1),
              "low": round(max(0.0, float(p) - q), 1), "high": round(min(CAP, float(p) + q), 1), "true_rul": float(t),
              "health": round(float(-iso.score_samples(Xte[i:i + 1, 1:])[0]), 4)}
             for i, (u, p, t) in enumerate(zip(last.index, pred, true))]
    bundle = {"subset": subset, "normalizer": norm, "model": model, "q": q, "features": fcols, "iso": iso, "metrics": metrics,
              "health_threshold": health_threshold}
    return {"metrics": metrics, "fleet": fleet, "bundle": bundle}


def train_all(subsets: tuple[str, ...] = ("FD001", "FD002", "FD003", "FD004")) -> dict[str, Any]:
    import joblib

    MODELS.mkdir(parents=True, exist_ok=True)
    out = {}
    for s in subsets:
        r = train_subset(s)
        joblib.dump(r["bundle"], MODELS / f"pdm_{s}.joblib", compress=3)
        out[s] = {"metrics": r["metrics"], "fleet": r["fleet"]}
        print(s, json.dumps({k: v for k, v in r["metrics"].items() if k != "sensors_used"}))
    (DATA / "reports").mkdir(parents=True, exist_ok=True)
    (DATA / "reports" / "pdm_cmapss.json").write_text(json.dumps(out, indent=1))
    return out


def predict_engine(bundle: dict[str, Any], history: pd.DataFrame) -> dict[str, float]:
    """RUL + interval + health for one engine's sensor history (columns as in C-MAPSS)."""
    z = bundle["normalizer"].transform(history)
    f = window_features(z, bundle["normalizer"].sensors)
    x = f[bundle["features"]].to_numpy(float)[-1:]
    p = float(np.clip(bundle["model"].predict(x)[0], 0, CAP))
    q = bundle["q"]
    return {"pred_rul": round(p, 1), "low": round(max(0.0, p - q), 1), "high": round(min(CAP, p + q), 1),
            "health": round(float(-bundle["iso"].score_samples(x[:, 1:])[0]), 4)}


if __name__ == "__main__":
    train_all()
