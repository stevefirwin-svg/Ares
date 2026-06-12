"""
ares_signal_audit.py — Ares Signal Distribution & Classification Audit
=======================================================================
Sprint 6 validation tool. Runs each engine against historical bars to verify:

  1. Signal distribution check
     - Each exclusive signal has std > 0 (non-degenerate)
     - NaN rate < 20% per signal
     - Cross-sectional z-scores have mean ≈ 0, std ≈ 1

  2. Classification frequency check
     - Engine classifies 2–8% of universe per scan (SHADOW_TARGET_CLASSIFICATION_RATE)
     - Below 1% → thresholds too tight → loosen or check signal computation
     - Above 15% → thresholds too loose → tighten dominance_threshold

  3. Priority conflict check
     - What % of Engine A setups also qualify for Engine E?
     - High overlap → adjust priority rules or add mutual-exclusion criteria

  4. Shadow log audit
     - Reads shadow_classifications.json
     - Reports classification rate per engine from actual shadow data
     - Flags any engine with 0 classifications after 20+ market days

Usage:
  python ares_signal_audit.py                    # full audit, all engines
  python ares_signal_audit.py --engine A         # single engine
  python ares_signal_audit.py --shadow           # shadow log audit only
  python ares_signal_audit.py --conflicts        # priority conflict check only
  python ares_signal_audit.py --days 60          # use 60 days of bars (default 30)

Output:
  Console report
  Writes ares_signal_audit_report.json
"""

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

from ares_config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    ENGINE_STATUS, BARS_LOOKBACK,
    SHADOW_TARGET_CLASSIFICATION_RATE,
    IC_HORIZON_DAYS,
)
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SignalAudit] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ares.signal_audit")

# ── File paths ────────────────────────────────────────────────────────────────

UNIVERSE_FILE     = "universe.json"
SHADOW_FILE       = "shadow_classifications.json"
AUDIT_REPORT_FILE = "ares_signal_audit_report.json"

# ── Thresholds ────────────────────────────────────────────────────────────────

SIGNAL_NAN_MAX        = 0.20   # Max allowed NaN rate per signal
SIGNAL_STD_MIN        = 1e-4   # Min signal std (below = degenerate)
ZSCORE_MEAN_TOL       = 0.30   # Cross-sectional z-score mean tolerance
ZSCORE_STD_TOL        = 0.30   # Cross-sectional z-score std tolerance (target 1.0)
CLASSIFICATION_MIN    = 0.01   # 1% — below this, gates too tight
CLASSIFICATION_MAX    = 0.15   # 15% — above this, gates too loose
CONFLICT_WARN_THRESH  = 0.30   # A/E overlap > 30% → flag for review

# ── Data helpers ──────────────────────────────────────────────────────────────

def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def fetch_bars(symbols: List[str], lookback_days: int = 30) -> Dict[str, pd.DataFrame]:
    """Fetch historical daily bars for a list of symbols."""
    if not symbols:
        return {}

    end   = datetime.now()
    start = end - timedelta(days=int(lookback_days * 1.6))  # extra days for weekends

    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    bars_out = {}

    for i in range(0, len(symbols), 200):
        batch = symbols[i:i + 200]
        params = {
            "symbols":   ",".join(batch),
            "timeframe": "1Day",
            "start":     start.strftime("%Y-%m-%d"),
            "end":       end.strftime("%Y-%m-%d"),
            "limit":     10000,
            "feed":      "iex",
        }
        try:
            resp = requests.get(
                "https://data.alpaca.markets/v2/stocks/bars",
                headers=headers, params=params, timeout=30
            )
            resp.raise_for_status()
            data = resp.json().get("bars", {})
            for sym, bar_list in data.items():
                if not bar_list:
                    continue
                df = pd.DataFrame(bar_list)
                df.rename(columns={"o": "open", "h": "high", "l": "low",
                                   "c": "close", "v": "volume", "t": "timestamp"}, inplace=True)
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df = df.sort_values("timestamp").reset_index(drop=True)
                if len(df) >= 20:
                    bars_out[sym] = df
        except Exception as e:
            logger.warning("Bar fetch failed for batch: %s", e)

    return bars_out


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    """Average True Range."""
    if len(df) < period + 1:
        return float("nan")
    high  = df["high"].values
    low   = df["low"].values
    close = df["close"].values
    tr = np.maximum(high[1:] - low[1:],
         np.maximum(np.abs(high[1:] - close[:-1]),
                    np.abs(low[1:] - close[:-1])))
    return float(np.mean(tr[-period:]))


# ── Signal extractors — match engine signal computation ─────────────────────
# These replicate the core signal logic from each engine for audit purposes.
# They are read-only — they do not gate, score, or order.

def extract_engine_a_signals(df: pd.DataFrame) -> dict:
    """Engine A: momentum continuation signals."""
    if len(df) < 60:
        return {}
    close = df["close"].values

    # a_mom_12_1: 12-month minus 1-month return (price at bar -252 to bar -21)
    if len(close) >= 252:
        mom_12_1 = (close[-21] / close[-252]) - 1.0
    elif len(close) >= 60:
        mom_12_1 = (close[-21] / close[0]) - 1.0
    else:
        mom_12_1 = float("nan")

    # a_hi52_proximity: close / 52-week high
    if len(close) >= 252:
        hi52 = np.max(close[-252:])
    else:
        hi52 = np.max(close)
    hi52_proximity = close[-1] / hi52 if hi52 > 0 else float("nan")

    # a_roc_accel: recent ROC vs longer ROC (acceleration)
    if len(close) >= 20:
        roc_5  = (close[-1] / close[-5])  - 1.0
        roc_20 = (close[-1] / close[-20]) - 1.0
        roc_accel = roc_5 - (roc_20 / 4.0)
    else:
        roc_accel = float("nan")

    return {
        "a_mom_12_1":       mom_12_1,
        "a_hi52_proximity": hi52_proximity,
        "a_roc_accel":      roc_accel,
    }


def extract_engine_b_signals(df: pd.DataFrame) -> dict:
    """Engine B: mean reversion signals."""
    if len(df) < 30:
        return {}
    close  = df["close"].values
    volume = df["volume"].values

    # b_avwap_distance: distance from 20d AVWAP in ATR units
    atr = _atr(df, 14)
    if len(close) >= 20 and atr > 0:
        cum_pv  = np.cumsum(close[-20:] * volume[-20:])
        cum_vol = np.cumsum(volume[-20:])
        avwap   = cum_pv[-1] / cum_vol[-1] if cum_vol[-1] > 0 else close[-1]
        avwap_dist = (close[-1] - avwap) / atr
    else:
        avwap_dist = float("nan")

    # b_stoch_divergence: stochastic K (14-period)
    if len(close) >= 14:
        lo14 = np.min(df["low"].values[-14:])
        hi14 = np.max(df["high"].values[-14:])
        stoch_k = (close[-1] - lo14) / (hi14 - lo14) if (hi14 - lo14) > 0 else 0.5
    else:
        stoch_k = float("nan")

    # b_volume_climax: recent volume vs 20d average
    if len(volume) >= 20:
        vol_climax = volume[-1] / np.mean(volume[-20:]) if np.mean(volume[-20:]) > 0 else float("nan")
    else:
        vol_climax = float("nan")

    return {
        "b_avwap_distance":   avwap_dist,
        "b_stoch_divergence": stoch_k,
        "b_volume_climax":    vol_climax,
    }


def extract_engine_e_signals(df: pd.DataFrame, sector_df: Optional[pd.DataFrame]) -> dict:
    """Engine E: RS rotation signals (requires sector bar data)."""
    if len(df) < 25 or sector_df is None or len(sector_df) < 25:
        return {}
    close     = df["close"].values
    sec_close = sector_df["close"].values

    def ret_n(arr, n):
        if len(arr) <= n:
            return float("nan")
        return (arr[-1] / arr[-n]) - 1.0

    rs_20d = ret_n(close, 20) - ret_n(sec_close, 20)
    rs_60d = (ret_n(close, min(60, len(close)-1)) -
              ret_n(sec_close, min(60, len(sec_close)-1)))

    rs_accel = rs_20d - (rs_60d / 3.0) if not np.isnan(rs_60d) else float("nan")

    return {
        "e_rs_20d":          rs_20d,
        "e_rs_60d":          rs_60d,
        "e_rs_acceleration": rs_accel,
    }


def extract_engine_f_signals(df: pd.DataFrame) -> dict:
    """Engine F: structural breakout signals."""
    if len(df) < 20:
        return {}
    close  = df["close"].values
    volume = df["volume"].values
    high   = df["high"].values

    # f_breakout_strength: today's close vs 20d high
    hi20 = np.max(high[-20:])
    breakout_strength = close[-1] / hi20 if hi20 > 0 else float("nan")

    # f_vol_declining: volume trend (negative = declining = consolidation)
    if len(volume) >= 10:
        vol_5  = np.mean(volume[-5:])
        vol_10 = np.mean(volume[-10:])
        vol_declining = 1.0 - (vol_5 / vol_10) if vol_10 > 0 else float("nan")
    else:
        vol_declining = float("nan")

    # f_rel_strength_spy: not computable without SPY — skip here
    return {
        "f_breakout_strength": breakout_strength,
        "f_vol_declining":     vol_declining,
    }


ENGINE_EXTRACTORS = {
    "A": lambda df, _:    extract_engine_a_signals(df),
    "B": lambda df, _:    extract_engine_b_signals(df),
    "E": lambda df, sec:  extract_engine_e_signals(df, sec),
    "F": lambda df, _:    extract_engine_f_signals(df),
}


# ── Signal distribution audit ─────────────────────────────────────────────────

def audit_signal_distributions(
    bars_map: Dict[str, pd.DataFrame],
    engine_id: str,
    sector_cache: Optional[Dict] = None,
) -> dict:
    """
    Compute signal values cross-sectionally and check distribution quality.
    Returns audit result dict.
    """
    extractor = ENGINE_EXTRACTORS.get(engine_id)
    if extractor is None:
        return {"engine_id": engine_id, "status": "NO_EXTRACTOR"}

    all_signals = defaultdict(list)

    for sym, df in bars_map.items():
        sec_df = None
        if sector_cache and engine_id == "E":
            # Use XLK as proxy for audit (proper sector lookup needs sector_map)
            sec_df = sector_cache.get("XLK")

        try:
            signals = extractor(df, sec_df)
        except Exception:
            continue

        for sig_name, val in signals.items():
            if val is not None and not (isinstance(val, float) and np.isnan(val)):
                all_signals[sig_name].append(val)

    result = {
        "engine_id": engine_id,
        "n_symbols":  len(bars_map),
        "signals":    {},
        "issues":     [],
    }

    for sig_name, values in all_signals.items():
        vals = np.array(values, dtype=float)
        n_total = len(bars_map)
        n_valid = len(vals)
        nan_rate = 1.0 - (n_valid / n_total) if n_total > 0 else 1.0

        sig_result = {
            "n_valid":  n_valid,
            "nan_rate": round(nan_rate, 4),
            "mean":     round(float(np.mean(vals)), 4) if n_valid > 0 else None,
            "std":      round(float(np.std(vals)), 4) if n_valid > 0 else None,
            "min":      round(float(np.min(vals)), 4) if n_valid > 0 else None,
            "max":      round(float(np.max(vals)), 4) if n_valid > 0 else None,
            "issues":   [],
        }

        # Check 1: degenerate (no variance)
        if n_valid > 5 and float(np.std(vals)) < SIGNAL_STD_MIN:
            sig_result["issues"].append("DEGENERATE: std ≈ 0 — signal has no cross-sectional variance")
            result["issues"].append(f"{sig_name}: degenerate")

        # Check 2: NaN rate
        if nan_rate > SIGNAL_NAN_MAX:
            sig_result["issues"].append(f"HIGH_NAN: {nan_rate:.1%} missing (threshold {SIGNAL_NAN_MAX:.1%})")
            result["issues"].append(f"{sig_name}: high NaN rate {nan_rate:.1%}")

        # Cross-sectional z-score check
        if n_valid > 10:
            z = (vals - np.mean(vals)) / (np.std(vals) + 1e-10)
            z_mean = float(np.mean(z))
            z_std  = float(np.std(z))
            sig_result["zscore_mean"] = round(z_mean, 4)
            sig_result["zscore_std"]  = round(z_std, 4)

            if abs(z_mean) > ZSCORE_MEAN_TOL:
                sig_result["issues"].append(f"ZSCORE_MEAN: {z_mean:.3f} (expected ≈ 0)")
            if abs(z_std - 1.0) > ZSCORE_STD_TOL:
                sig_result["issues"].append(f"ZSCORE_STD: {z_std:.3f} (expected ≈ 1.0)")

        result["signals"][sig_name] = sig_result

    return result


# ── Classification frequency audit ────────────────────────────────────────────

def audit_shadow_classifications(engine_id: Optional[str] = None) -> dict:
    """
    Reads shadow_classifications.json and reports:
    - Classification count per engine
    - Classification rate per scan day
    - Days since last classification
    - Flag if rate is outside [1%, 15%]
    """
    shadow = _load_json(SHADOW_FILE, {"classifications": []})
    classifications = shadow.get("classifications", [])

    universe = _load_json(UNIVERSE_FILE, {})
    universe_size = len(universe.get("symbols", []))
    if universe_size == 0:
        universe_size = 150  # fallback estimate

    result = {}

    engines_to_check = [engine_id] if engine_id else list(ENGINE_STATUS.keys())

    for eid in engines_to_check:
        if ENGINE_STATUS.get(eid, "off") == "off":
            continue

        engine_entries = [c for c in classifications if c.get("engine_id") == eid]

        if not engine_entries:
            result[eid] = {
                "n_classifications": 0,
                "unique_scan_days":  0,
                "avg_rate_per_scan": 0.0,
                "last_classification": None,
                "issues": ["NO_CLASSIFICATIONS: engine has never fired in shadow mode"],
            }
            continue

        # Group by scan date
        by_date = defaultdict(list)
        for entry in engine_entries:
            date = entry.get("date", "unknown")
            by_date[date].append(entry)

        n_scan_days   = len(by_date)
        avg_per_scan  = len(engine_entries) / n_scan_days if n_scan_days > 0 else 0
        avg_rate      = avg_per_scan / universe_size if universe_size > 0 else 0

        dates_sorted = sorted(by_date.keys())
        last_date    = dates_sorted[-1] if dates_sorted else None

        issues = []
        if avg_rate < CLASSIFICATION_MIN:
            issues.append(f"RATE_TOO_LOW: {avg_rate:.2%} avg (threshold {CLASSIFICATION_MIN:.0%}) — gates too tight")
        if avg_rate > CLASSIFICATION_MAX:
            issues.append(f"RATE_TOO_HIGH: {avg_rate:.2%} avg (threshold {CLASSIFICATION_MAX:.0%}) — gates too loose")

        result[eid] = {
            "n_classifications":   len(engine_entries),
            "unique_scan_days":    n_scan_days,
            "avg_per_scan":        round(avg_per_scan, 2),
            "avg_rate_per_scan":   round(avg_rate, 4),
            "last_classification": last_date,
            "classification_by_date": dict(by_date) if n_scan_days <= 10 else {},
            "issues": issues,
        }

    return result


# ── Priority conflict check ───────────────────────────────────────────────────

def audit_priority_conflicts(bars_map: Dict[str, pd.DataFrame]) -> dict:
    """
    Check what % of Engine A candidates also qualify for Engine E.
    High overlap → adjust priority rules.

    Simple proxy: compute both signal sets and flag overlap.
    """
    a_candidates = set()
    e_candidates = set()

    for sym, df in bars_map.items():
        # Engine A: basic momentum check
        try:
            sigs_a = extract_engine_a_signals(df)
            if (sigs_a.get("a_mom_12_1", 0) or 0) > 0.05:
                a_candidates.add(sym)
        except Exception:
            pass

        # Engine E: basic RS check (vs SPY as proxy — no sector ETF in audit)
        try:
            sigs_e = extract_engine_e_signals(df, bars_map.get("SPY"))
            if (sigs_e.get("e_rs_20d", 0) or 0) > 0.03:
                e_candidates.add(sym)
        except Exception:
            pass

    overlap = a_candidates & e_candidates
    overlap_pct = len(overlap) / len(a_candidates) if a_candidates else 0.0

    issues = []
    if overlap_pct > CONFLICT_WARN_THRESH:
        issues.append(
            f"HIGH_OVERLAP: {overlap_pct:.1%} of Engine A candidates also qualify for Engine E "
            f"(threshold {CONFLICT_WARN_THRESH:.0%}) — review priority rules"
        )

    return {
        "engine_a_candidates":  len(a_candidates),
        "engine_e_candidates":  len(e_candidates),
        "overlap_count":        len(overlap),
        "overlap_pct":          round(overlap_pct, 4),
        "overlap_symbols":      sorted(overlap)[:20],  # first 20
        "issues":               issues,
    }


# ── Full audit runner ─────────────────────────────────────────────────────────

def run_audit(
    engine_id: Optional[str] = None,
    lookback_days: int = 30,
    include_shadow: bool = True,
    include_conflicts: bool = True,
) -> dict:
    """Full signal audit pipeline."""
    report = {
        "run_time":     datetime.now().isoformat(),
        "lookback_days": lookback_days,
        "signal_distributions": {},
        "shadow_audit": {},
        "priority_conflicts": {},
        "summary_issues": [],
    }

    # Load universe
    universe_data = _load_json(UNIVERSE_FILE, {})
    symbols = list(universe_data.get("symbols", []))
    if not symbols:
        logger.error("Universe file empty — run ares_universe.py first")
        return report

    logger.info("Fetching %d bars for %d symbols...", lookback_days, len(symbols))
    bars_map = fetch_bars(symbols + ["SPY"], lookback_days=lookback_days)
    logger.info("Bars received for %d/%d symbols", len(bars_map), len(symbols))

    # Signal distribution audit
    engines_to_check = [engine_id] if engine_id else [
        e for e in ["A", "B", "E", "F"] if ENGINE_STATUS.get(e) != "off"
    ]

    for eid in engines_to_check:
        logger.info("Auditing Engine %s signal distributions...", eid)
        dist_result = audit_signal_distributions(bars_map, eid)
        report["signal_distributions"][eid] = dist_result

        for issue in dist_result.get("issues", []):
            report["summary_issues"].append(f"[{eid}] {issue}")

    # Shadow audit
    if include_shadow:
        logger.info("Auditing shadow classification rates...")
        shadow_result = audit_shadow_classifications(engine_id)
        report["shadow_audit"] = shadow_result

        for eid, aud in shadow_result.items():
            for issue in aud.get("issues", []):
                report["summary_issues"].append(f"[{eid}] {issue}")

    # Priority conflict check
    if include_conflicts and not engine_id:
        logger.info("Checking priority conflicts (Engine A vs E)...")
        conflict_result = audit_priority_conflicts(bars_map)
        report["priority_conflicts"] = conflict_result

        for issue in conflict_result.get("issues", []):
            report["summary_issues"].append(f"[A/E] {issue}")

    # Write report
    try:
        tmp = AUDIT_REPORT_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(report, f, indent=2, default=str)
        os.replace(tmp, AUDIT_REPORT_FILE)
        logger.info("Report written to %s", AUDIT_REPORT_FILE)
    except Exception as e:
        logger.warning("Failed to write report: %s", e)

    return report


def print_audit_report(report: dict):
    """Pretty-print the audit report to console."""
    print(f"\n{'='*70}")
    print(f"  ARES SIGNAL AUDIT REPORT — {report.get('run_time', '')[:19]}")
    print(f"  Lookback: {report.get('lookback_days', '?')}d")
    print(f"{'='*70}")

    # Signal distributions
    for eid, dist in report.get("signal_distributions", {}).items():
        n_sym = dist.get("n_symbols", 0)
        print(f"\n  Engine {eid} — {n_sym} symbols analyzed")
        for sig, res in dist.get("signals", {}).items():
            status = "✓" if not res.get("issues") else "⚠"
            print(f"    {status} {sig:<35s}  "
                  f"n={res.get('n_valid', 0):3d}  "
                  f"nan={res.get('nan_rate', 0):.1%}  "
                  f"std={res.get('std', 0):.4f}  "
                  f"mean={res.get('mean', 0):+.4f}")
            for issue in res.get("issues", []):
                print(f"      ⚠ {issue}")

    # Shadow audit
    shadow = report.get("shadow_audit", {})
    if shadow:
        print(f"\n  SHADOW CLASSIFICATION AUDIT")
        print(f"  {'Engine':<8s} {'Count':>6s} {'Days':>5s} {'Avg/Scan':>9s} {'Rate':>8s}  Last")
        print(f"  " + "-" * 55)
        for eid, aud in shadow.items():
            n   = aud.get("n_classifications", 0)
            d   = aud.get("unique_scan_days", 0)
            avg = aud.get("avg_per_scan", 0)
            rate = aud.get("avg_rate_per_scan", 0)
            last = aud.get("last_classification", "never")
            flag = " ⚠" if aud.get("issues") else ""
            print(f"  {eid:<8s} {n:>6d} {d:>5d} {avg:>9.1f} {rate:>7.2%}  {last}{flag}")
        for eid, aud in shadow.items():
            for issue in aud.get("issues", []):
                print(f"  ⚠ [{eid}] {issue}")

    # Conflicts
    conflicts = report.get("priority_conflicts", {})
    if conflicts:
        print(f"\n  PRIORITY CONFLICT CHECK (Engine A vs E)")
        print(f"  A candidates: {conflicts.get('engine_a_candidates', 0)}")
        print(f"  E candidates: {conflicts.get('engine_e_candidates', 0)}")
        print(f"  Overlap:      {conflicts.get('overlap_count', 0)} "
              f"({conflicts.get('overlap_pct', 0):.1%})")
        for issue in conflicts.get("issues", []):
            print(f"  ⚠ {issue}")

    # Summary
    issues = report.get("summary_issues", [])
    print(f"\n  SUMMARY: {len(issues)} issue(s) found")
    if issues:
        for issue in issues:
            print(f"  ⚠ {issue}")
    else:
        print("  ✓ All checks passed")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ares Signal Audit")
    parser.add_argument("--engine",    type=str, help="Single engine (A/B/C/E/F)")
    parser.add_argument("--shadow",    action="store_true", help="Shadow log audit only")
    parser.add_argument("--conflicts", action="store_true", help="Priority conflict check only")
    parser.add_argument("--days",      type=int, default=30, help="Lookback days (default 30)")
    args = parser.parse_args()

    if args.shadow:
        result = {"shadow_audit": audit_shadow_classifications(args.engine),
                  "run_time": datetime.now().isoformat(),
                  "lookback_days": 0,
                  "signal_distributions": {},
                  "priority_conflicts": {},
                  "summary_issues": []}
        print_audit_report(result)
    elif args.conflicts:
        universe_data = _load_json(UNIVERSE_FILE, {})
        symbols = list(universe_data.get("symbols", []))
        bars_map = fetch_bars(symbols + ["SPY"], lookback_days=args.days)
        conflict_result = audit_priority_conflicts(bars_map)
        issues = conflict_result.get("issues", [])
        print(json.dumps(conflict_result, indent=2))
    else:
        report = run_audit(
            engine_id=args.engine.upper() if args.engine else None,
            lookback_days=args.days,
            include_shadow=True,
            include_conflicts=not args.engine,
        )
        print_audit_report(report)
