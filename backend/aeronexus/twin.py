"""Engine digital twin and predictive-maintenance simulator on NASA C-MAPSS.

What the twin shows, per cycle of one engine's history:
  * remaining useful life (RUL) from the trained model, with its split-conformal 90% interval,
  * the overall health score (IsolationForest fitted on healthy early-life windows),
  * a per-component drift index for fan, LPC, HPC, combustor, HPT, LPT and bleed. Each index is
    derived from the C-MAPSS sensors that instrument that module (Saxena et al., 2008). It is a
    transparent sensor-group drift measure, NOT a separately trained per-component model,
  * raw sensor readings in engineering units and their regime-normalised z-scores.

The simulator replays the history cycle by cycle, can inject faults (spike, drift, stuck sensor, noise)
into the raw readings and re-runs the same models on the perturbed history, so you can see how the
predictions and the anomaly detector respond. Injected runs are marked as simulated everywhere.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd

from .observability import tracer

# C-MAPSS sensor dictionary (Saxena, Goebel, Simon & Eklund, 2008)
SENSOR_INFO: dict[str, dict[str, str]] = {
    "s1": {"name": "T2", "label": "Fan inlet temperature", "unit": "°R"},
    "s2": {"name": "T24", "label": "LPC outlet temperature", "unit": "°R"},
    "s3": {"name": "T30", "label": "HPC outlet temperature", "unit": "°R"},
    "s4": {"name": "T50", "label": "LPT outlet temperature", "unit": "°R"},
    "s5": {"name": "P2", "label": "Fan inlet pressure", "unit": "psia"},
    "s6": {"name": "P15", "label": "Bypass-duct pressure", "unit": "psia"},
    "s7": {"name": "P30", "label": "HPC outlet pressure", "unit": "psia"},
    "s8": {"name": "Nf", "label": "Physical fan speed", "unit": "rpm"},
    "s9": {"name": "Nc", "label": "Physical core speed", "unit": "rpm"},
    "s10": {"name": "epr", "label": "Engine pressure ratio", "unit": "–"},
    "s11": {"name": "Ps30", "label": "HPC outlet static pressure", "unit": "psia"},
    "s12": {"name": "phi", "label": "Fuel flow / Ps30", "unit": "pps/psi"},
    "s13": {"name": "NRf", "label": "Corrected fan speed", "unit": "rpm"},
    "s14": {"name": "NRc", "label": "Corrected core speed", "unit": "rpm"},
    "s15": {"name": "BPR", "label": "Bypass ratio", "unit": "–"},
    "s16": {"name": "farB", "label": "Burner fuel-air ratio", "unit": "–"},
    "s17": {"name": "htBleed", "label": "Bleed enthalpy", "unit": "–"},
    "s18": {"name": "Nf_dmd", "label": "Demanded fan speed", "unit": "rpm"},
    "s19": {"name": "PCNfR_dmd", "label": "Demanded corrected fan speed", "unit": "rpm"},
    "s20": {"name": "W31", "label": "HPT coolant bleed", "unit": "lbm/s"},
    "s21": {"name": "W32", "label": "LPT coolant bleed", "unit": "lbm/s"},
}

COMPONENTS: list[dict[str, Any]] = [
    {"id": "fan", "label": "Fan", "sensors": ["s6", "s8", "s13", "s15"],
     "what": "Moves most of the air; bypass ratio and fan speed shift as blades erode."},
    {"id": "lpc", "label": "Low-pressure compressor", "sensors": ["s2"],
     "what": "First compression stage; outlet temperature rises as efficiency drops."},
    {"id": "hpc", "label": "High-pressure compressor", "sensors": ["s3", "s7", "s11"],
     "what": "The module C-MAPSS degrades in every subset; watch T30, P30 and Ps30."},
    {"id": "combustor", "label": "Combustor", "sensors": ["s12", "s16"],
     "what": "Fuel flow per unit pressure rises to hold thrust as the core wears."},
    {"id": "hpt", "label": "High-pressure turbine", "sensors": ["s9", "s14", "s20"],
     "what": "Drives the HPC; core speed and coolant bleed respond to its condition."},
    {"id": "lpt", "label": "Low-pressure turbine", "sensors": ["s4", "s21"],
     "what": "Drives the fan; exhaust temperature T50 is the classic wear indicator."},
    {"id": "bleed", "label": "Bleed and nozzle", "sensors": ["s17", "s10"],
     "what": "Bleed enthalpy and pressure ratio summarise overall cycle health."},
]

KEY_SENSORS = ("s2", "s3", "s4", "s7", "s11", "s12", "s15", "s17", "s20", "s21")
INJECTIONS = ("spike", "drift", "stuck", "noise")


def _bundle(subset: str) -> dict[str, Any] | None:
    from .modules.pdm import bundle

    return bundle(subset)


@lru_cache(maxsize=8)
def _test_frame(subset: str) -> pd.DataFrame:
    from .pdm.cmapss import load

    return load(subset, "test")


@lru_cache(maxsize=4)
def _true_rul(subset: str) -> np.ndarray:
    from .pdm.cmapss import load_rul

    return load_rul(subset)


def engines(subset: str) -> list[int]:
    return sorted(int(u) for u in _test_frame(subset)["unit"].unique())


def components() -> dict[str, Any]:
    return {"components": COMPONENTS, "sensors": SENSOR_INFO}


def _inject(g: pd.DataFrame, inject: list[dict[str, Any]], sd: dict[str, float], seed: int = 11) -> pd.DataFrame:
    g = g.copy()
    rng = np.random.default_rng(seed)
    cyc = g["cycle"].to_numpy()
    for inj in inject:
        kind, s = inj.get("type"), inj.get("sensor")
        if kind not in INJECTIONS or s not in g.columns:
            raise ValueError(f"bad injection {inj}")
        start = int(inj.get("start", cyc[len(cyc) // 2]))
        mag = float(inj.get("magnitude", 4.0)) * sd.get(s, 1.0)
        at = cyc >= start
        if kind == "spike":
            g.loc[cyc == start, s] += mag
        elif kind == "drift":
            span = max(1, cyc.max() - start)
            g.loc[at, s] += mag * (cyc[at] - start) / span
        elif kind == "stuck":
            v = g.loc[at, s].iloc[0] if at.any() else None
            if v is not None:
                g.loc[at, s] = v
        elif kind == "noise":
            g.loc[at, s] += rng.normal(0, abs(mag), at.sum())
    return g


def _detect(cycles: np.ndarray, health: np.ndarray, thr: float, z: pd.DataFrame, raw: pd.DataFrame, sensors: list[str],
            rul: np.ndarray) -> list[dict[str, Any]]:
    """Three transparent detectors: model health threshold, sensor spikes (robust residual), stuck sensors."""
    events: list[dict[str, Any]] = []
    over = health > thr
    run = 0
    for i, o in enumerate(over):
        run = run + 1 if o else 0
        if run == 3:  # persistent, not a single noisy point
            c = int(cycles[i - 2])
            events.append({"cycle": c, "kind": "health", "severity": "alert", "sensor": None,
                           "value": round(float(health[i - 2]), 4),
                           "message": f"Health score above the healthy-engine 99th percentile ({thr:.3f}) for 3 cycles from cycle {c}."})
            break
    for s in sensors:
        x = z[s].to_numpy(float)
        if len(x) < 12 or raw[s].iloc[: len(x) // 2].nunique() < 0.3 * (len(x) // 2):
            continue  # coarsely quantised sensors (e.g. P15 in FD001) step between a few values; not usable for spikes
        med = pd.Series(x).shift(1).rolling(9, min_periods=5).median().to_numpy()
        mad = pd.Series(np.abs(x - med)).shift(1).rolling(15, min_periods=6).median().to_numpy()
        sig = np.maximum(1.4826 * np.nan_to_num(mad, nan=1.0), 0.8)
        r = np.abs(x - np.nan_to_num(med, nan=x)) / sig
        for i in np.where(r > 6.0)[0][:3]:
            events.append({"cycle": int(cycles[i]), "kind": "spike", "severity": "watch", "sensor": s,
                           "value": round(float(x[i]), 3),
                           "message": f"{SENSOR_INFO[s]['name']} jumped {r[i]:.1f}× its recent spread at cycle {int(cycles[i])}."})
        rx = raw[s].to_numpy(float)
        same = np.r_[False, np.diff(rx) == 0]
        streak = pd.Series(same).groupby((~pd.Series(same)).cumsum()).cumsum().to_numpy()
        typical = float(np.mean(same[: max(10, len(same) // 2)]))  # some sensors are coarsely quantised
        flat = np.where(streak >= 8)[0] if typical < 0.3 else np.array([], dtype=int)
        if len(flat):
            i = int(flat[0]) - 7
            events.append({"cycle": int(cycles[max(i, 0)]), "kind": "stuck", "severity": "alert", "sensor": s, "value": round(float(rx[i]), 3),
                           "message": f"{SENSOR_INFO[s]['name']} has not changed for 8 cycles: likely a stuck sensor, not wear."})
    drops = np.where(np.diff(rul) < -25)[0]
    for i in drops[:2]:
        events.append({"cycle": int(cycles[i + 1]), "kind": "rul_drop", "severity": "watch", "sensor": None,
                       "value": round(float(rul[i + 1]), 1),
                       "message": f"Predicted life fell {rul[i] - rul[i + 1]:.0f} cycles in one step; check inputs before acting."})
    return sorted(events, key=lambda e: e["cycle"])


def _component_scores(z: pd.DataFrame, sensors: list[str]) -> dict[str, np.ndarray]:
    """Drift index per module: mean |15-cycle mean − the engine's own first-20-cycle baseline|, mapped to 0–100."""
    out: dict[str, np.ndarray] = {}
    sm = z[sensors].rolling(15, min_periods=1).mean()
    base = z[sensors].iloc[: min(20, len(z))].mean()
    dev = (sm - base).abs()
    for comp in COMPONENTS:
        ss = [s for s in comp["sensors"] if s in sensors]
        if not ss:
            out[comp["id"]] = np.full(len(z), np.nan)
            continue
        d = dev[ss].mean(axis=1).to_numpy()
        out[comp["id"]] = np.clip(100.0 * np.exp(-d / 1.6), 0, 100)
    return out


@lru_cache(maxsize=64)
def _telemetry_cached(subset: str, unit: int, inject_key: str) -> dict[str, Any]:
    import json

    from .pdm.cmapss import window_features

    b = _bundle(subset)
    if b is None:
        return {"available": False, "message": "Train the C-MAPSS models first (python scripts/train_public_models.py)."}
    te = _test_frame(subset)
    g = te[te["unit"] == unit].sort_values("cycle")
    if g.empty:
        raise KeyError(f"{subset} unit {unit}")
    norm = b["normalizer"]
    sensors = list(norm.sensors)
    inject = json.loads(inject_key)
    if inject:
        sd = {s: float(np.mean([st[1][i] for st in norm.stats.values()])) for i, s in enumerate(sensors)}
        g = _inject(g, inject, sd)
    with tracer.span("twin.telemetry", subset=subset, unit=unit, injected=bool(inject)):
        z = norm.transform(g).reset_index(drop=True)
        f = window_features(z, sensors)
        X = f[b["features"]].to_numpy(float)
        pred = np.clip(b["model"].predict(X), 0, 125)
        health = -b["iso"].score_samples(X[:, 1:])
    q, thr = float(b["q"]), float(b["health_threshold"])
    cycles = g["cycle"].to_numpy()
    last = int(cycles[-1])
    true_last = float(_true_rul(subset)[unit - 1])
    comp = _component_scores(z, sensors)
    events = _detect(cycles, health, thr, z, g.reset_index(drop=True), sensors, pred)
    flagged = {e["cycle"] for e in events}
    key = [s for s in KEY_SENSORS if s in sensors]
    frames = []
    graw = g.reset_index(drop=True)
    for i, c in enumerate(cycles):
        frames.append({
            "cycle": int(c), "rul": round(float(pred[i]), 1), "low": round(max(0.0, pred[i] - q), 1),
            "high": round(min(125.0, pred[i] + q), 1), "health": round(float(health[i]), 4),
            "true_rul": min(125.0, true_last + (last - int(c))), "anomaly": int(c) in flagged,
            "components": {k: (None if np.isnan(v[i]) else round(float(v[i]), 1)) for k, v in comp.items()},
            "raw": {s: round(float(graw.loc[i, s]), 3) for s in sensors},
            "z": {s: round(float(z.loc[i, s]), 3) for s in key},
        })
    m = b["metrics"]
    return {
        "available": True, "subset": subset, "unit": unit, "sensors": sensors, "key_sensors": key, "frames": frames,
        "events": events, "health_threshold": round(thr, 4), "interval_halfwidth": round(q, 2), "injected": inject,
        "simulated": bool(inject), "model": {"selected": m["selected_model"], "test_rmse": m["test_rmse_capped"],
                                             "coverage": m["interval_coverage_test"]},
        "notes": ["C-MAPSS engines are simulated turbofans; true RUL is shown for evaluation only.",
                  "Component scores are sensor-group drift indices, not separately trained models.",
                  "The 90% interval assumes data like the training set; when the health score is above threshold "
                  "or faults are injected, treat it as optimistic."],
    }


def telemetry(subset: str, unit: int, inject: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    import json

    if subset not in ("FD001", "FD002", "FD003", "FD004"):
        raise ValueError("subset must be FD001–FD004")
    inject = inject or []
    if len(inject) > 4:
        raise ValueError("at most 4 injections")
    return _telemetry_cached(subset, int(unit), json.dumps(inject, sort_keys=True))


def twin_state(subset: str, unit: int, cycle: int | None = None) -> dict[str, Any]:
    t = telemetry(subset, unit)
    if not t.get("available"):
        return t
    frames = t["frames"]
    i = len(frames) - 1 if cycle is None else max(0, min(len(frames) - 1, next(
        (k for k, f in enumerate(frames) if f["cycle"] >= cycle), len(frames) - 1)))
    f = frames[i]
    prev = frames[max(0, i - 15)]
    comps = []
    for c in COMPONENTS:
        ss = [s for s in c["sensors"] if s in t["sensors"]]
        score = f["components"].get(c["id"])
        trend = None if score is None or prev["components"].get(c["id"]) is None else round(score - prev["components"][c["id"]], 1)
        comps.append({**c, "score": score, "trend_15": trend,
                      "status": "n/a" if score is None else ("good" if score >= 75 else "watch" if score >= 50 else "poor"),
                      "readings": [{"sensor": s, **SENSOR_INFO[s], "value": f["raw"][s],
                                    "z": f["z"].get(s)} for s in ss]})
    return {"available": True, "subset": subset, "unit": unit, "cycle": f["cycle"], "cycles_total": frames[-1]["cycle"],
            "rul": {"pred": f["rul"], "low": f["low"], "high": f["high"], "true": f["true_rul"]},
            "health": f["health"], "health_threshold": t["health_threshold"], "components": comps,
            "events": [e for e in t["events"] if e["cycle"] <= f["cycle"]], "model": t["model"], "notes": t["notes"]}
