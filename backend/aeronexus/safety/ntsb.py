"""Aviation safety analytics on the public NTSB accident database (17,485 events with a
probable-cause statement, 2008–2022).

What is trained and why
  * Severity triage: predict whether an event involved a fatality from its probable-cause text.
    Label comes from the structured `Total.Fatal.Injuries` field (not from the text), and words that
    state the outcome ("fatal", "killed", "died", …) are masked so the model must use causal language.
  * Temporal split: train 2008–2015, validate 2016, test 2017–2022. Random splits leak era-specific
    wording; a time split is what a deployed model faces.
  * Two text models compared on validation (word 1–2-gram TF-IDF vs character n-grams, both
    class-balanced logistic regression); isotonic calibration on the validation year.
  * Selective prediction: the model abstains in an uncertain probability band; we report the
    coverage/accuracy trade-off so a reviewer knows what fraction is safe to auto-triage.
  * Cause themes: NMF topics over the TF-IDF space (unsupervised) with per-year prevalence.
  * Deterministic tags (rules, not ML): maintenance-related causes, fuel management, loss of control,
    weather — transparent counts that link to the MRO module.
  * Similar-event retrieval: the platform's hybrid retriever over probable-cause statements.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.decomposition import NMF
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, f1_score, roc_auc_score
from sklearn.pipeline import make_pipeline

from ..config import DATA, MODELS
from ..evals.metrics import expected_calibration_error
from ..public_data import REGISTRY

OUTCOME_WORDS = re.compile(r"\b(fatal\w*|kill\w*|died|death\w*|deceased|fatality|fatalities)\b", re.I)
TAGS = {
    "maintenance": r"\bmaintenance\b|\bmechanic\b|\binspection\b|improper(ly)? (install|rig|maint)|\bannual inspection\b|fatigue crack",
    "fuel": r"\bfuel (exhaustion|starvation|contamination|management)\b|\bran out of fuel\b",
    "loss_of_control": r"loss of (aircraft |directional |airplane |helicopter )?control|failure to maintain (directional )?control",
    "weather": r"\bwind|gust|turbulence|icing|instrument meteorological|\bfog\b|thunderstorm|density altitude",
    "engine_power": r"loss of (engine )?power|engine failure|powerplant|partial power",
    "collision": r"collision|obstacle|wire|terrain|tree",
}


def load_events() -> pd.DataFrame:
    path = REGISTRY["ntsb"].folder / "AviationData.csv"
    df = pd.read_csv(path, encoding="latin-1", low_memory=False)
    df = df[df["Report.Status"].fillna("").str.len() > 60].copy()
    df["year"] = df["Event.Date"].str[:4].astype(int)
    df["text"] = df["Report.Status"].str.strip()
    df["text_masked"] = df["text"].str.replace(OUTCOME_WORDS, "[outcome]", regex=True)
    df["fatal"] = (df["Total.Fatal.Injuries"].fillna(0) > 0).astype(int)
    return df.reset_index(drop=True)


def split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    return df[df.year <= 2015], df[df.year == 2016], df[df.year >= 2017]


def selective(prob: np.ndarray, y: np.ndarray, threshold: float, bands: list[float]) -> list[dict[str, float]]:
    rows = []
    for b in bands:
        keep = np.abs(prob - threshold) >= b
        if keep.sum() == 0:
            continue
        acc = float(np.mean((prob[keep] >= threshold).astype(int) == y[keep]))
        rows.append({"abstain_band": b, "coverage": round(float(keep.mean()), 4), "accuracy": round(acc, 4)})
    return rows


def train(seed: int = 7) -> dict[str, Any]:
    import joblib

    t0 = time.time()
    df = load_events()
    tr, va, te = split(df)
    cands = {
        "word_tfidf_lr": make_pipeline(TfidfVectorizer(ngram_range=(1, 2), min_df=3, sublinear_tf=True, stop_words="english"),
                                       LogisticRegression(C=4.0, class_weight="balanced", max_iter=2000)),
        "char_tfidf_lr": make_pipeline(TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=3, sublinear_tf=True),
                                       LogisticRegression(C=2.0, class_weight="balanced", max_iter=2000)),
    }
    val_scores = {}
    for name, m in cands.items():
        m.fit(tr.text_masked, tr.fatal)
        val_scores[name] = round(float(average_precision_score(va.fatal, m.predict_proba(va.text_masked)[:, 1])), 4)
    best = max(val_scores, key=val_scores.get)
    base = cands[best]
    cal = CalibratedClassifierCV(base, method="isotonic", cv="prefit") if _prefit_supported() else None
    if cal is not None:
        cal.fit(va.text_masked, va.fatal)
        model = cal
    else:  # sklearn ≥1.8 removed cv="prefit": wrap with FrozenEstimator
        from sklearn.frozen import FrozenEstimator

        model = CalibratedClassifierCV(FrozenEstimator(base), method="isotonic").fit(va.text_masked, va.fatal)
    pv = model.predict_proba(va.text_masked)[:, 1]
    ths = np.linspace(0.05, 0.95, 91)
    th = float(ths[int(np.argmax([f1_score(va.fatal, (pv >= t).astype(int)) for t in ths]))])
    p = model.predict_proba(te.text_masked)[:, 1]
    yte = te.fatal.to_numpy()
    pred = (p >= th).astype(int)
    prevalence = float(yte.mean())
    leak_check = {"unmasked_auroc": round(float(roc_auc_score(yte, base.predict_proba(te.text)[:, 1])), 4)}

    # cause themes (unsupervised) on the training era
    vec = TfidfVectorizer(max_features=6000, min_df=5, stop_words="english", ngram_range=(1, 2), sublinear_tf=True)
    Xall = vec.fit_transform(df.text_masked)
    nmf = NMF(n_components=10, random_state=seed, init="nndsvda", max_iter=400)
    W = nmf.fit_transform(Xall)
    vocab = np.array(vec.get_feature_names_out())
    topics = [{"id": k, "top_terms": vocab[np.argsort(-comp)[:8]].tolist()} for k, comp in enumerate(nmf.components_)]
    df["topic"] = W.argmax(1)
    by_year = df.groupby(["year", "topic"]).size().unstack(fill_value=0)
    share = (by_year.T / by_year.sum(1)).T.round(4)

    tags = {k: df.text.str.contains(v, case=False, regex=True) for k, v in TAGS.items()}
    tag_trend = {k: t.astype(float).groupby(df.year).mean().round(4).to_dict() for k, t in tags.items()}
    tag_fatality = {k: round(float(df.loc[t, "fatal"].mean()), 4) for k, t in tags.items()}

    metrics = {
        "task": "fatal outcome from probable-cause text (outcome words masked)",
        "n_train": int(len(tr)), "n_val": int(len(va)), "n_test": int(len(te)), "test_prevalence": round(prevalence, 4),
        "validation_pr_auc": val_scores, "selected_model": best, "threshold": round(th, 3),
        "test_auroc": round(float(roc_auc_score(yte, p)), 4), "test_pr_auc": round(float(average_precision_score(yte, p)), 4),
        "test_f1": round(float(f1_score(yte, pred)), 4), "test_recall": round(float(pred[yte == 1].mean()), 4),
        "test_precision": round(float(yte[pred == 1].mean()) if pred.sum() else 0.0, 4),
        "calibration": expected_calibration_error(p.tolist(), (pred == yte).tolist()) | {
            "ece_prob": expected_calibration_error(p.tolist(), yte.astype(bool).tolist())["ece"]},
        "selective_prediction": selective(p, yte, th, [0.0, 0.05, 0.1, 0.15, 0.2]),
        "leakage_check": leak_check | {"masked_auroc": round(float(roc_auc_score(yte, p)), 4)},
        "train_seconds": round(time.time() - t0, 1),
        "data": "NTSB aviation accident database (real public data), temporal split",
    }
    MODELS.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "threshold": th, "metrics": metrics}, MODELS / "safety_ntsb.joblib", compress=3)
    report = {"metrics": metrics, "topics": topics, "topic_share_by_year": {str(y): r.to_dict() for y, r in share.iterrows()},
              "tag_trend": {k: {str(y): v for y, v in d.items()} for k, d in tag_trend.items()}, "tag_fatality_rate": tag_fatality,
              "counts_by_year": df.groupby("year").size().to_dict()}
    (DATA / "reports").mkdir(parents=True, exist_ok=True)
    (DATA / "reports" / "safety_ntsb.json").write_text(json.dumps(report, indent=1, default=str))
    return report


def _prefit_supported() -> bool:
    import inspect

    from sklearn import __version__

    major, minor = (int(x) for x in __version__.split(".")[:2])
    return (major, minor) < (1, 6) and "prefit" in inspect.getsource(CalibratedClassifierCV)


if __name__ == "__main__":
    r = train()
    print(json.dumps(r["metrics"], indent=1, default=str))
    for t in r["topics"]:
        print(t)
