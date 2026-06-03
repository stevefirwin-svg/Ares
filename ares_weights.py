"""
ares_weights.py — Shared Signal Weight Loader
==============================================
RF-14 fix: calibrated signal weights written by statistical_validation.py to
sub_engine_params.json were never consumed by any engine. Each engine's
compute_engine_score hardcoded equal weights (e.g. score = (a+b+c)/3).

This module provides a single `load_signal_weights(engine_id)` function
imported by all five engines. Engines call it at scan time (not import time,
so weights refresh each scan from the latest calibration run).

Fallback: if sub_engine_params.json is missing, engine not calibrated, or any
weight is absent — equal weights are returned automatically with a log warning.
No engine ever fails because weights are unavailable.

Usage (in each engine's compute_engine_score):
    from ares_weights import load_signal_weights, weighted_score

    weights = load_signal_weights("A")
    score = weighted_score({"a_mom_12_1": 0.8, "a_hi52_proximity": 0.6, ...}, weights)
"""

import json
import logging
import os
from typing import Dict, Optional

logger = logging.getLogger("ares.weights")

PARAMS_FILE = "sub_engine_params.json"

# Signal registries — must match statistical_validation.py ENGINE_SIGNALS
ENGINE_SIGNALS = {
    "A": ["a_mom_12_1", "a_hi52_proximity", "a_roc_accel"],
    "B": ["b_avwap_distance", "b_stoch_divergence", "b_volume_climax"],
    "C": ["c_squeeze_duration", "c_hv_ratio", "c_momentum_direction"],
    "E": ["e_rs_20d", "e_rs_60d", "e_sector_rs_spy", "e_rs_acceleration", "e_new_rs_high"],
    "F": ["f_consol_quality", "f_breakout_strength", "f_overhead_clearance",
          "f_vol_declining", "f_rel_strength_spy"],
}


def load_signal_weights(engine_id: str) -> Dict[str, float]:
    """
    Load calibrated signal weights for an engine from sub_engine_params.json.

    Returns a dict of {signal_name: weight} normalized to sum=1.0.
    Falls back to equal weights if:
      - File doesn't exist
      - Engine not in file
      - Engine not calibrated (calibrated=False)
      - Any signal is missing from stored weights

    Weights are loaded fresh on each call — no module-level caching —
    so the running engines automatically pick up new calibrations.
    """
    signals = ENGINE_SIGNALS.get(engine_id, [])
    n = len(signals)
    equal = {s: round(1.0 / n, 6) for s in signals} if n > 0 else {}

    if not os.path.exists(PARAMS_FILE):
        return equal

    try:
        with open(PARAMS_FILE) as f:
            params = json.load(f)
    except Exception as e:
        logger.warning("[%s] Failed to load %s: %s — using equal weights", engine_id, PARAMS_FILE, e)
        return equal

    ep = params.get(engine_id, {})
    if not ep.get("calibrated", False):
        # Bootstrap equal weights — calibration not yet run
        return equal

    stored = ep.get("signal_weights", {})
    if not stored:
        return equal

    # Verify all signals present
    missing = [s for s in signals if s not in stored]
    if missing:
        logger.warning("[%s] Calibrated weights missing signals %s — using equal weights",
                       engine_id, missing)
        return equal

    # Re-normalize to sum=1 (float safety)
    weights = {s: float(stored.get(s, 1.0 / n)) for s in signals}
    total = sum(weights.values())
    if total < 1e-10:
        return equal
    return {s: round(w / total, 6) for s, w in weights.items()}


def weighted_score(
    signal_values: Dict[str, float],
    weights: Dict[str, float],
    normalize_signals: bool = True,
) -> float:
    """
    Compute a weighted composite score from normalized signal values and IC weights.

    signal_values: {signal_name: raw_value} — values should already be z-scored
                   or normalized to [0,1] by the engine's own compute_engine_score
    weights:       {signal_name: weight} from load_signal_weights — sum to 1.0
    normalize_signals: if True, clip values to [-3, 3] and rescale to [0, 1]
                       before weighting. Prevents a single extreme signal from
                       dominating.

    Returns a score in [0, 1].
    """
    import math

    if not signal_values or not weights:
        return 0.0

    total_w = 0.0
    total_s = 0.0

    for sig, w in weights.items():
        val = signal_values.get(sig)
        if val is None or (isinstance(val, float) and math.isnan(val)):
            continue
        v = float(val)
        if normalize_signals:
            # Clip to [-3σ, 3σ] and map to [0, 1]
            v = max(-3.0, min(3.0, v))
            v = (v + 3.0) / 6.0
        total_s += w * v
        total_w += w

    if total_w < 1e-10:
        return 0.0

    return round(total_s / total_w, 4)


def is_calibrated(engine_id: str) -> bool:
    """Returns True if engine has calibrated (non-equal) weights on file."""
    if not os.path.exists(PARAMS_FILE):
        return False
    try:
        with open(PARAMS_FILE) as f:
            params = json.load(f)
        return params.get(engine_id, {}).get("calibrated", False)
    except Exception:
        return False
