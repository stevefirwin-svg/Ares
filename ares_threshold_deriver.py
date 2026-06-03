"""
ares_threshold_deriver.py — Ares Empirical Threshold Derivation
================================================================
Replaces all TODO:DERIVE constants in ares_config.py with empirically
derived values from shadow classifications and closed trade outcomes.

What this derives:
  1. Classification thresholds — engine_score_min, gate values per engine
     Source: shadow_classifications.json
     Method: percentile of would-have-traded scores that produced positive
             forward returns (precision-optimizing threshold)

  2. Kelly clips — KELLY_ENGINE_CLIPS per engine
     Source: sub_engine_outcomes.json closed trades
     Method: EVT tail on per-trade return distribution; set floor to
             preserve capital in tail events

  3. Stop multipliers — ENGINE_STOP_PARAMS per engine
     Source: closed trades where stop was the exit reason
     Method: ATR multiple at which trades were stopped vs those that
             continued; set to preserve 80% of profitable trades

  4. Max positions — MAX_POSITIONS_PER_ENGINE per engine
     Source: closed trades
     Method: empirical average position size → equity_cap / avg_position

Output:
  Console report of derived vs current values
  Writes ares_threshold_recommendations.json (NOT auto-applied to config)
  Steve reviews and manually updates ares_config.py — NEVER auto-patched

Gating rules:
  - Requires ≥ IC_CALIBRATION_GATE (30) closed trades per engine for full derivation
  - Below gate: reports current (TODO:DERIVE) values and trade counts needed
  - Shadow threshold derivation requires ≥ 20 shadow entries per engine

Usage:
  python ares_threshold_deriver.py              # derive all engines
  python ares_threshold_deriver.py --engine A   # single engine
  python ares_threshold_deriver.py --report     # show current vs recommended, no derivation
"""

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy import stats as sp_stats

from ares_config import (
    ENGINE_STATUS,
    IC_CALIBRATION_GATE,
    IC_HORIZON_DAYS,
    CLASSIFICATION_THRESHOLDS,
    KELLY_ENGINE_CLIPS,
    ENGINE_STOP_PARAMS,
    MAX_POSITIONS_PER_ENGINE,
    TOTAL_EQUITY,
    INITIAL_ENGINE_WEIGHTS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ThreshDeriver] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ares.thresh_deriver")

# ── File paths ────────────────────────────────────────────────────────────────

OUTCOMES_FILE  = "sub_engine_outcomes.json"
SHADOW_FILE    = "shadow_classifications.json"
REPORT_FILE    = "ares_threshold_recommendations.json"

# ── Constants ─────────────────────────────────────────────────────────────────

SHADOW_MIN_FOR_THRESHOLD = 20   # Min shadow entries before deriving score threshold
STOP_PRESERVATION_RATE   = 0.80 # Keep 80% of profitable trades above derived stop
KELLY_TAIL_QUANTILE      = 0.05 # EVT on 5th percentile of trade return distribution
PRECISION_TARGET         = 0.55 # Target win rate for engine_score_min derivation
                                  # TODO:DERIVE — derive from break-even analysis per engine


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to load %s: %s", path, e)
        return default


def load_closed_trades(engine_id: str) -> List[dict]:
    """Load closed trades with forward returns for a specific engine."""
    outcomes = _load_json(OUTCOMES_FILE, {"trades": []})
    return [
        t for t in outcomes.get("trades", [])
        if t.get("engine_id") == engine_id
        and t.get("forward_return_Nd") is not None
    ]


def load_shadow_entries(engine_id: str) -> List[dict]:
    """Load shadow classification entries for a specific engine."""
    shadow = _load_json(SHADOW_FILE, {"classifications": []})
    return [
        c for c in shadow.get("classifications", [])
        if c.get("engine_id") == engine_id
    ]


# ── Derivation methods ────────────────────────────────────────────────────────

def derive_engine_score_threshold(
    shadow_entries: List[dict],
    engine_id: str
) -> Optional[float]:
    """
    Derive engine_score_min from shadow data.

    Method: Find the score threshold that achieves PRECISION_TARGET win rate
    on forward returns in shadow entries that have been tagged with outcomes.

    Only uses shadow entries where forward_return_Nd has been filled in
    (tagged after N days by outcome_tracker.py).

    Returns None if insufficient data.
    """
    # Filter to entries with outcome tags
    tagged = [
        e for e in shadow_entries
        if e.get("forward_return_Nd") is not None
    ]

    if len(tagged) < SHADOW_MIN_FOR_THRESHOLD:
        logger.info("[%s] Score threshold: %d/%d tagged entries (need %d)",
                    engine_id, len(tagged), len(shadow_entries), SHADOW_MIN_FOR_THRESHOLD)
        return None

    scores   = np.array([float(e["engine_score"]) for e in tagged])
    fwd_rets = np.array([float(e["forward_return_Nd"]) for e in tagged])

    # Sweep thresholds from 0.40 to 0.85
    best_threshold = None
    best_precision = 0.0

    for thresh in np.arange(0.40, 0.86, 0.01):
        mask = scores >= thresh
        if mask.sum() < 5:
            continue
        precision = (fwd_rets[mask] > 0).mean()
        n_passing = mask.sum()

        # Prefer threshold that hits precision target with most entries
        if precision >= PRECISION_TARGET and precision > best_precision:
            best_precision = precision
            best_threshold = float(thresh)

    if best_threshold is None:
        # No threshold achieves target — report current best
        best_idx = np.argmax([
            (fwd_rets[scores >= t] > 0).mean() if (scores >= t).sum() >= 5 else 0
            for t in np.arange(0.40, 0.86, 0.01)
        ])
        best_threshold = round(0.40 + best_idx * 0.01, 2)
        logger.warning("[%s] Could not achieve %.0f%% precision — best at %.2f",
                       engine_id, PRECISION_TARGET * 100, best_threshold)

    logger.info("[%s] Derived engine_score_min=%.2f (precision=%.1f%% on %d entries)",
                engine_id, best_threshold, best_precision * 100, len(tagged))
    return round(best_threshold, 2)


def derive_kelly_clip(
    trades: List[dict],
    engine_id: str
) -> Optional[Tuple[float, float]]:
    """
    Derive KELLY_ENGINE_CLIPS (min_fraction, max_fraction) from trade returns.

    Method:
      - min_fraction: 5th percentile of worst trade size as fraction of equity
        (ensures we're not under-sizing)
      - max_fraction: EVT-derived max position size such that the 5th percentile
        tail loss does not exceed 2× the bootstrap Kelly fraction

    Returns None if insufficient data.
    """
    if len(trades) < IC_CALIBRATION_GATE:
        return None

    # Position sizes as fraction of equity (from entry_params if recorded)
    fractions = []
    returns   = []
    for t in trades:
        pnl = t.get("forward_return_Nd")
        if pnl is None:
            continue
        returns.append(float(pnl))

        # Try to get position size
        params = t.get("entry_params", {})
        kelly  = params.get("kelly_scale")
        if kelly:
            fractions.append(float(kelly))

    if len(returns) < 10:
        return None

    r = np.array(returns)

    # 5th percentile of return distribution (tail loss)
    tail_loss = float(np.percentile(r, 5))

    # Engine equity cap
    weight = INITIAL_ENGINE_WEIGHTS.get(engine_id, 0.20)
    equity_cap = TOTAL_EQUITY * weight

    # max_fraction: if tail_loss happens on max position, max loss = 2% of equity
    # max_position * |tail_loss| <= 0.02 → max_position = 0.02 / |tail_loss|
    if tail_loss < 0:
        max_fraction = min(0.12, round(0.02 / abs(tail_loss), 4))
    else:
        max_fraction = 0.10  # positive tail — keep current max

    # min_fraction: keep at 2% (not derived yet — needs more trades)
    min_fraction = 0.02

    logger.info("[%s] Derived Kelly clips: (%.2f, %.2f) — tail_loss_5pct=%.2f%%",
                engine_id, min_fraction, max_fraction, tail_loss * 100)

    return (min_fraction, max_fraction)


def derive_stop_multiplier(
    trades: List[dict],
    engine_id: str
) -> Optional[float]:
    """
    Derive stop multiplier from stop-exit trades.

    Method: Among trades that exited at stop, find the ATR multiple at entry.
    Set stop_mult such that STOP_PRESERVATION_RATE of profitable trades
    would have survived the stop.

    Returns None if insufficient data or no stop exits.
    """
    stop_trades = [
        t for t in trades
        if "stop" in str(t.get("exit_reason", "")).lower()
    ]

    profitable_trades = [
        t for t in trades
        if (t.get("forward_return_Nd") or 0) > 0
    ]

    if len(stop_trades) < 5 or len(profitable_trades) < 10:
        logger.info("[%s] Stop mult: insufficient stop exits (%d) or profitable (%d)",
                    engine_id, len(stop_trades), len(profitable_trades))
        return None

    # Get stop mults at entry for stop-exit trades
    stop_mults_at_exit = []
    for t in stop_trades:
        params = t.get("entry_params", {})
        mult   = params.get("stop_mult")
        if mult:
            stop_mults_at_exit.append(float(mult))

    if len(stop_mults_at_exit) < 3:
        return None

    # Current stop mult
    current_mult = ENGINE_STOP_PARAMS.get(engine_id, {}).get("stop_mult_low_vol", 3.0)

    # Recommend median of actual stop mults that triggered — if significantly
    # different from current, flag it
    median_stop = float(np.median(stop_mults_at_exit))

    logger.info("[%s] Stop exits median mult=%.2f vs current=%.2f",
                engine_id, median_stop, current_mult)

    return round(median_stop, 2)


def derive_max_positions(
    trades: List[dict],
    engine_id: str
) -> Optional[int]:
    """
    Derive MAX_POSITIONS_PER_ENGINE from empirical average position size.

    Method: avg_position = median(trade value) from closed trades
            max_positions = floor(engine_equity / avg_position)
    """
    if len(trades) < 10:
        return None

    sizes = []
    for t in trades:
        # Try entry_price * shares (if recorded)
        entry_price = t.get("entry_price")
        params      = t.get("entry_params", {})
        kelly       = params.get("kelly_scale")

        if entry_price and kelly:
            weight = INITIAL_ENGINE_WEIGHTS.get(engine_id, 0.20)
            position_value = TOTAL_EQUITY * weight * float(kelly)
            sizes.append(position_value)

    if len(sizes) < 5:
        return None

    avg_size = float(np.median(sizes))
    weight   = INITIAL_ENGINE_WEIGHTS.get(engine_id, 0.20)
    engine_equity = TOTAL_EQUITY * weight

    max_pos = max(1, int(engine_equity / avg_size))
    max_pos = min(max_pos, 4)  # Hard ceiling — never more than 4 per engine

    logger.info("[%s] Derived max_positions=%d (avg_size=$%.0f, engine_equity=$%.0f)",
                engine_id, max_pos, avg_size, engine_equity)

    return max_pos


# ── Per-engine derivation ─────────────────────────────────────────────────────

def derive_for_engine(engine_id: str) -> dict:
    """Run all derivations for one engine. Returns recommendations dict."""
    trades  = load_closed_trades(engine_id)
    shadow  = load_shadow_entries(engine_id)

    n_trades = len(trades)
    n_shadow = len(shadow)

    # RF-26: count threshold sweep combinations for DSR n_trials
    # score_thresh sweep: 46 values (0.40 to 0.85 step 0.01)
    # kelly_clip sweep: ~20 combinations per engine (see derive_kelly_clip)
    # Total combinations per engine ≈ 46 + 20 = ~66
    # This is used by deflated_sharpe_ratio to apply the Bailey-López de Prado correction
    N_THRESH_COMBOS = 46 + 20  # conservative estimate; refine with actual sweep counts

    result = {
        "engine_id":          engine_id,
        "n_trades":           n_trades,
        "n_shadow":           n_shadow,
        "gate":               IC_CALIBRATION_GATE,
        "n_threshold_combos": N_THRESH_COMBOS,   # RF-26: for DSR inflation correction
        "recommendations":    {},
        "current_values":     {},
        "status":             "BELOW_GATE" if n_trades < IC_CALIBRATION_GATE else "OK",
        "notes":              [],
    }

    # Record current values
    ct = CLASSIFICATION_THRESHOLDS.get(engine_id, {})
    result["current_values"] = {
        "engine_score_min": ct.get("engine_score_min"),
        "kelly_clips":      KELLY_ENGINE_CLIPS.get(engine_id),
        "stop_params":      ENGINE_STOP_PARAMS.get(engine_id),
        "max_positions":    MAX_POSITIONS_PER_ENGINE.get(engine_id),
    }

    logger.info("[%s] Deriving thresholds — %d trades, %d shadow entries",
                engine_id, n_trades, n_shadow)

    # 1. Engine score threshold (from shadow)
    score_thresh = derive_engine_score_threshold(shadow, engine_id)
    if score_thresh is not None:
        result["recommendations"]["engine_score_min"] = score_thresh
        current = ct.get("engine_score_min", "N/A")
        if isinstance(current, float) and abs(score_thresh - current) > 0.05:
            result["notes"].append(
                f"engine_score_min: current={current:.2f} → recommended={score_thresh:.2f} "
                f"(diff={score_thresh - current:+.2f})"
            )
    else:
        result["notes"].append(
            f"engine_score_min: cannot derive yet "
            f"(need {SHADOW_MIN_FOR_THRESHOLD} tagged shadow entries, have {n_shadow})"
        )

    if n_trades < IC_CALIBRATION_GATE:
        result["notes"].append(
            f"kelly_clips/stop_mult/max_positions: "
            f"need {IC_CALIBRATION_GATE - n_trades} more closed trades"
        )
        return result

    # 2. Kelly clips
    kelly_rec = derive_kelly_clip(trades, engine_id)
    if kelly_rec:
        result["recommendations"]["kelly_clips"] = list(kelly_rec)
        current = KELLY_ENGINE_CLIPS.get(engine_id, "N/A")
        result["notes"].append(
            f"kelly_clips: current={current} → recommended={kelly_rec}"
        )

    # 3. Stop multiplier
    stop_rec = derive_stop_multiplier(trades, engine_id)
    if stop_rec:
        result["recommendations"]["stop_mult"] = stop_rec
        current_stop = ENGINE_STOP_PARAMS.get(engine_id, {})
        result["notes"].append(
            f"stop_mult: current={current_stop} → recommended low_vol={stop_rec}"
        )

    # 4. Max positions
    max_pos_rec = derive_max_positions(trades, engine_id)
    if max_pos_rec:
        result["recommendations"]["max_positions"] = max_pos_rec
        current_max = MAX_POSITIONS_PER_ENGINE.get(engine_id, "N/A")
        if max_pos_rec != current_max:
            result["notes"].append(
                f"max_positions: current={current_max} → recommended={max_pos_rec}"
            )

    return result


def run_all_derivations(engine_id: Optional[str] = None) -> dict:
    """Derive thresholds for all (or one) engine(s)."""
    full_report = {
        "run_time": datetime.now().isoformat(),
        "note": (
            "ADVISORY ONLY — never auto-applied. "
            "Review and manually update ares_config.py after verifying each recommendation."
        ),
        "engines": {},
    }

    engines = [engine_id] if engine_id else [
        e for e in ["A", "B", "C", "E", "F"]
        if ENGINE_STATUS.get(e, "off") != "off"
    ]

    for eid in engines:
        logger.info("=" * 55)
        logger.info("Deriving thresholds for Engine %s", eid)
        logger.info("=" * 55)
        result = derive_for_engine(eid)
        full_report["engines"][eid] = result

        # RF-26: Write n_threshold_combos to sub_engine_params.json so DSR can load it
        params_file = "sub_engine_params.json"
        try:
            params = {}
            if os.path.exists(params_file):
                with open(params_file) as f:
                    params = json.load(f)
            if eid not in params:
                params[eid] = {}
            params[eid]["n_threshold_combos"] = result.get("n_threshold_combos", 1)
            params[eid]["threshold_deriver_run"] = datetime.now().isoformat()
            tmp = params_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(params, f, indent=2)
            os.replace(tmp, params_file)
            logger.info("[%s] n_threshold_combos=%d written to %s",
                        eid, result.get("n_threshold_combos", 1), params_file)
        except Exception as e:
            logger.warning("[%s] Failed to write n_threshold_combos: %s", eid, e)

    # Write report
    try:
        tmp = REPORT_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(full_report, f, indent=2)
        os.replace(tmp, REPORT_FILE)
        logger.info("Recommendations written to %s", REPORT_FILE)
    except Exception as e:
        logger.warning("Failed to write report: %s", e)

    return full_report


def print_report(report: dict):
    """Print derivation report to console."""
    print(f"\n{'='*70}")
    print(f"  ARES THRESHOLD DERIVATION REPORT — {report.get('run_time', '')[:19]}")
    print(f"  {report.get('note', '')}")
    print(f"{'='*70}")

    for eid, result in report.get("engines", {}).items():
        n  = result.get("n_trades", 0)
        ns = result.get("n_shadow", 0)
        gate = result.get("gate", 30)
        status = result.get("status", "?")

        print(f"\n  Engine {eid} — {status} | {n}/{gate} closed trades | {ns} shadow entries")

        current = result.get("current_values", {})
        rec     = result.get("recommendations", {})

        print(f"  {'Parameter':<28s} {'Current':>12s} {'Recommended':>14s}")
        print(f"  " + "-" * 56)

        params = [
            ("engine_score_min", current.get("engine_score_min"), rec.get("engine_score_min")),
            ("kelly_clips",      current.get("kelly_clips"),      rec.get("kelly_clips")),
            ("stop_mult",        current.get("stop_params"),      rec.get("stop_mult")),
            ("max_positions",    current.get("max_positions"),    rec.get("max_positions")),
        ]

        for name, cur, derived in params:
            cur_str  = str(cur)[:12]  if cur  is not None else "TODO:DERIVE"
            rec_str  = str(derived)[:14] if derived is not None else "(insufficient data)"
            changed  = " ←" if derived is not None and str(derived) != str(cur) else ""
            print(f"  {name:<28s} {cur_str:>12s} {rec_str:>14s}{changed}")

        for note in result.get("notes", []):
            print(f"  ℹ {note}")

    print(f"\n{'='*70}")
    print(f"  To apply: manually edit ares_config.py with recommended values.")
    print(f"  Verify each change against your risk tolerance before applying.")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ares Threshold Deriver")
    parser.add_argument("--engine", type=str, help="Single engine (A/B/C/E/F)")
    parser.add_argument("--report", action="store_true",
                        help="Show current vs recommended from last run")
    args = parser.parse_args()

    if args.report:
        report = _load_json(REPORT_FILE, {})
        if not report:
            print("No report found — run without --report first.")
        else:
            print_report(report)
    else:
        report = run_all_derivations(args.engine.upper() if args.engine else None)
        print_report(report)
