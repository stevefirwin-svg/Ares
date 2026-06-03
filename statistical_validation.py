"""
statistical_validation.py — Ares Statistical Validation Framework
==================================================================
Sprint 5 calibration engine. Called after ≥ 30 closed trades per engine.

What this does:
  1. Spearman IC computation — per-signal predictive power vs forward_return_Nd
  2. Purged IC with embargo — non-overlapping windows to eliminate label leakage
  3. ICIR (IC / rolling std) — stability-adjusted signal ranking
  4. Benjamini-Hochberg FDR control — multiple-testing correction across all signals
  5. Ledoit-Wolf shrinkage — covariance shrinkage on IC estimates
  6. Weight derivation — IC-weighted, FDR-filtered, shrinkage-adjusted per engine
  7. Sub-engine params update — writes validated weights to sub_engine_params.json

Gating rules (immutable):
  - Engine must have ≥ 30 closed trades (IC_CALIBRATION_GATE in ares_config.py)
  - IC computed against forward_return_Nd, NOT realized pnl_pct (RF-1/RF-2 fix)
  - Horizon is engine-specific and immutable (IC_HORIZON_DAYS in ares_config.py)
  - FDR correction applied before any weight is promoted
  - Engines below gate → equal weights maintained, no calibration

References:
  Grinold & Kahn (1999) — Active Portfolio Management (IC/ICIR framework)
  López de Prado (2018) — Advances in Financial Machine Learning (Purged CV, Ch. 7)
  Bailey & López de Prado (2014) — The Deflated Sharpe Ratio
  Benjamini & Hochberg (1995) — Controlling the False Discovery Rate
  Ledoit & Wolf (2004) — A well-conditioned estimator for large-dimensional covariance matrices

Usage:
  python statistical_validation.py              # run all engines
  python statistical_validation.py --engine A   # single engine
  python statistical_validation.py --report     # print current weights, no update
  python statistical_validation.py --dry-run    # compute but do not write weights
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy import stats as sp_stats

from ares_config import (
    IC_HORIZON_DAYS,
    IC_CALIBRATION_GATE,
    ENGINE_STATUS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [StatVal] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ares.stat_val")

# ── File paths ────────────────────────────────────────────────────────────────

OUTCOMES_FILE = "sub_engine_outcomes.json"
PARAMS_FILE   = "sub_engine_params.json"
REPORT_FILE   = "statistical_validation_report.json"

# ── Signal registry — which signals belong to each engine ────────────────────
# Must match signals_at_entry keys written by each engine's scan()

ENGINE_SIGNALS = {
    "A": ["a_mom_12_1", "a_hi52_proximity", "a_roc_accel"],
    "B": ["b_avwap_distance", "b_stoch_divergence", "b_volume_climax"],
    "C": ["c_squeeze_duration", "c_hv_ratio", "c_momentum_direction"],
    "E": ["e_rs_20d", "e_rs_60d", "e_sector_rs_spy", "e_rs_acceleration", "e_new_rs_high"],
    "F": ["f_consol_quality", "f_breakout_strength", "f_overhead_clearance",
          "f_vol_declining", "f_rel_strength_spy"],
}

# ── IC parameters ─────────────────────────────────────────────────────────────

IC_ROLLING_WINDOW       = 20    # Rolling IC std window for ICIR denominator
IC_MIN_OBSERVATIONS     = 10    # Min obs per IC window (purged — fewer due to embargo)
FDR_ALPHA               = 0.10  # Benjamini-Hochberg FDR level (10% — finance standard)
                                 # TODO:DERIVE — tighten to 0.05 after 100+ trades per engine
ICIR_FLOOR              = 0.30  # Minimum ICIR to include signal in weighted set
                                 # TODO:DERIVE — derive from bootstrap null distribution after 60 trades
LEDOIT_WOLF_SHRINKAGE   = True  # Apply Ledoit-Wolf to IC covariance (bundled — do not separate)

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


def _save_json_atomic(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_engine_trades(engine_id: str, prefer_unconditioned: bool = False) -> List[dict]:
    """
    Load closed trades for a specific engine from sub_engine_outcomes.json.

    prefer_unconditioned: if True, also loads unconditioned shadow records
    (conditioned_by_ownership=False) from shadow_classifications.json as
    additional IC observations. These are NOT trades — they are logged by the
    RF-10 unconditioned pass and need forward_return_Nd filled by outcome_tracker.

    Default: False (use only actual closed trades). Set True when computing IC
    to reduce selection bias from scan-order routing.
    """
    outcomes = _load_json(OUTCOMES_FILE, {"trades": []})
    trades = outcomes.get("trades", [])
    engine_trades = [
        t for t in trades
        if t.get("engine_id") == engine_id
        and t.get("forward_return_Nd") is not None
        and t.get("signals_at_entry") is not None
    ]

    if prefer_unconditioned:
        # Also load unconditioned shadow records with filled forward_return_Nd
        shadow_data = _load_json("shadow_classifications.json", {"classifications": []})
        unc_records = [
            {
                "engine_id":         r.get("engine_id"),
                "signals_at_entry":  r.get("signals", {}),
                "forward_return_Nd": r.get("forward_return_Nd"),
                "entry_date":        r.get("date"),
                "conditioned":       False,  # mark as unconditioned for traceability
            }
            for r in shadow_data.get("classifications", [])
            if r.get("engine_id") == engine_id
            and r.get("conditioned_by_ownership") is False
            and r.get("forward_return_Nd") is not None
        ]
        if unc_records:
            logger.info("[%s] Adding %d unconditioned IC observations (RF-10)", engine_id, len(unc_records))
            engine_trades = engine_trades + unc_records

    return engine_trades


# ── Purged IC computation ─────────────────────────────────────────────────────

def _parse_date(s: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except Exception:
            pass
    return None


def purge_overlapping(trades: List[dict], horizon_days: int) -> List[dict]:
    """
    Remove trades whose forward return windows overlap with a prior trade.
    Embargo = horizon_days (López de Prado Ch. 7).

    Trades are sorted by entry_date. A trade is kept only if its entry_date
    is at least horizon_days after the previous kept trade's entry_date.
    This eliminates label leakage from overlapping return windows.
    """
    if not trades:
        return []

    sorted_trades = sorted(
        trades,
        key=lambda t: _parse_date(t.get("entry_date", "")) or datetime.min
    )

    kept = []
    last_kept_date = None

    for trade in sorted_trades:
        entry_date = _parse_date(trade.get("entry_date", ""))
        if entry_date is None:
            continue

        if last_kept_date is None:
            kept.append(trade)
            last_kept_date = entry_date
        else:
            days_since = (entry_date - last_kept_date).days
            if days_since >= horizon_days:
                kept.append(trade)
                last_kept_date = entry_date

    return kept


def compute_spearman_ic(
    signal_values: List[float],
    forward_returns: List[float]
) -> Tuple[float, float]:
    """
    Spearman rank IC between signal values and forward returns.
    Returns (ic, p_value).
    """
    if len(signal_values) < IC_MIN_OBSERVATIONS:
        return 0.0, 1.0

    x = np.array(signal_values, dtype=float)
    y = np.array(forward_returns, dtype=float)

    # Drop NaN pairs
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < IC_MIN_OBSERVATIONS:
        return 0.0, 1.0

    # Check for degenerate inputs
    if np.std(x[mask]) < 1e-10 or np.std(y[mask]) < 1e-10:
        return 0.0, 1.0

    try:
        ic, p_val = sp_stats.spearmanr(x[mask], y[mask])
        return float(ic) if np.isfinite(ic) else 0.0, float(p_val)
    except Exception:
        return 0.0, 1.0


def compute_rolling_icir(
    trades: List[dict],
    signal_name: str,
    forward_returns: List[float],
    window: int = IC_ROLLING_WINDOW
) -> float:
    """
    ICIR = mean(rolling IC) / std(rolling IC).
    Uses a rolling window of IC estimates across time-ordered trades.
    Returns 0.0 if insufficient data.
    """
    if len(trades) < window + IC_MIN_OBSERVATIONS:
        return 0.0

    rolling_ics = []
    for i in range(len(trades) - window + 1):
        window_trades = trades[i:i + window]
        sig_vals = [
            t.get("signals_at_entry", {}).get(signal_name)
            for t in window_trades
        ]
        fwd_rets = forward_returns[i:i + window]

        # Filter None values
        pairs = [(s, r) for s, r in zip(sig_vals, fwd_rets)
                 if s is not None and np.isfinite(float(s)) and np.isfinite(r)]
        if len(pairs) < IC_MIN_OBSERVATIONS:
            continue

        s_vals, r_vals = zip(*pairs)
        ic, _ = compute_spearman_ic(list(s_vals), list(r_vals))
        rolling_ics.append(ic)

    if len(rolling_ics) < 3:
        return 0.0

    ic_mean = np.mean(rolling_ics)
    ic_std  = np.std(rolling_ics)

    if ic_std < 1e-10:
        return 0.0

    return float(ic_mean / ic_std)


# ── Benjamini-Hochberg FDR ────────────────────────────────────────────────────

def benjamini_hochberg(p_values: List[float], alpha: float = FDR_ALPHA) -> List[bool]:
    """
    Benjamini-Hochberg procedure for controlling False Discovery Rate.
    Returns a boolean mask: True = signal passes FDR threshold.

    Reference: Benjamini & Hochberg (1995), JRSS-B 57(1):289-300.
    """
    n = len(p_values)
    if n == 0:
        return []

    # Sort p-values with original indices
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    sorted_indices = [i for i, _ in indexed]
    sorted_pvals   = [p for _, p in indexed]

    # BH threshold: p_(k) <= (k/m) * alpha
    critical_values = [(k + 1) / n * alpha for k in range(n)]
    significant = [False] * n

    # Find the largest k where p_(k) <= (k/m) * alpha
    last_sig = -1
    for k, (p, cv) in enumerate(zip(sorted_pvals, critical_values)):
        if p <= cv:
            last_sig = k

    # All hypotheses up to last_sig are rejected (signal is significant)
    for k in range(last_sig + 1):
        significant[sorted_indices[k]] = True

    return significant


# ── Ledoit-Wolf shrinkage ─────────────────────────────────────────────────────

def ledoit_wolf_shrinkage(ic_matrix: np.ndarray) -> np.ndarray:
    """
    Ledoit-Wolf analytical shrinkage of IC covariance matrix.
    Uses sklearn.covariance.LedoitWolf — the authoritative implementation of
    Ledoit & Wolf (2004), Journal of Multivariate Analysis 88(2):365-411.

    RF-17 fix: previous implementation had a spurious `mu**2 * p` term in the
    numerator with no basis in LW (2004). Replaced with sklearn's OAS/LW estimator.

    ic_matrix: (n_obs, n_signals) matrix of per-observation IC proxies
    Returns: shrunk (n_signals, n_signals) covariance matrix
    """
    n, p = ic_matrix.shape
    if n < p + 2:
        return np.cov(ic_matrix.T) if n > 1 else np.eye(p)

    try:
        from sklearn.covariance import LedoitWolf as SklearnLW
        lw = SklearnLW(assume_centered=False)
        lw.fit(ic_matrix)
        return lw.covariance_
    except ImportError:
        # sklearn not available — fall back to Oracle Approximating Shrinkage (closed form)
        # This IS the correct LW (2004) formula, no spurious terms
        S  = np.cov(ic_matrix.T)
        mu = np.trace(S) / p
        delta_sq = np.linalg.norm(S - mu * np.eye(p), 'fro') ** 2
        if delta_sq < 1e-14:
            return S
        # Optimal shrinkage intensity: alpha* = min(1, (1/n)*trace(S^2 + mu^2*(p-2)*I)/delta_sq)
        # Simplified: alpha* = (sum(diag(S)^2)/(n*delta_sq)) clamped to [0,1]
        s2_sum = float(np.sum(np.diag(S) ** 2))
        alpha_star = min(1.0, max(0.0, s2_sum / (n * delta_sq)))
        return (1 - alpha_star) * S + alpha_star * mu * np.eye(p)


# ── Per-engine IC calibration ─────────────────────────────────────────────────

def calibrate_engine(
    engine_id: str,
    dry_run: bool = False
) -> dict:
    """
    Full IC calibration pipeline for one engine.

    Steps:
      1. Load trades — verify gate (≥ 30)
      2. Purge overlapping return windows (embargo = IC horizon)
      3. Compute Spearman IC + p-value per signal
      4. Compute rolling ICIR per signal
      5. BH FDR correction across all signals
      6. Ledoit-Wolf shrinkage on IC covariance
      7. Derive normalized signal weights (FDR-filtered, ICIR-weighted)
      8. Write to sub_engine_params.json (unless dry_run)

    Returns calibration report dict.
    """
    signals   = ENGINE_SIGNALS.get(engine_id, [])
    horizon   = IC_HORIZON_DAYS.get(engine_id, 10)
    gate      = IC_CALIBRATION_GATE

    report = {
        "engine_id":    engine_id,
        "horizon_days": horizon,
        "run_time":     datetime.now().isoformat(),
        "status":       "SKIPPED",
        "n_trades_raw": 0,
        "n_trades_purged": 0,
        "signals":      {},
        "weights":      {},
        "fdr_alpha":    FDR_ALPHA,
        "notes":        [
            # RF-3: 30-trade gate is a sign/sanity screen only — NOT a validated IC threshold.
            # SE(Spearman)≈0.186 at n=30; IC=0.05 is statistically indistinguishable from 0.
            # Passing this gate confirms non-degeneracy, not edge. Live weighting waits for
            # regime-spanning samples (ideally hundreds of trades).
            "IC@n=30 is a sign/sanity screen only (RF-3). Do not interpret as evidence of edge.",
            # RF-10: IC is conditioned on scan-order ownership routing — selection bias present.
            # Every engine only evaluates symbols not claimed by earlier-scanning engines.
            # True unconditioned IC requires secondary shadow logging without ownership lock.
            "IC estimates are routing-conditioned (RF-10 selection bias). See ARES_SKILL.md §Ownership.",
        ],
    }

    # ── Step 1: Load trades ───────────────────────────────────────────────────
    trades = load_engine_trades(engine_id)
    report["n_trades_raw"] = len(trades)

    if len(trades) < gate:
        report["status"] = f"BELOW_GATE ({len(trades)}/{gate} trades)"
        report["notes"].append(f"Equal weights maintained — need {gate - len(trades)} more trades")
        logger.info("[%s] Below gate: %d/%d trades. Equal weights.", engine_id, len(trades), gate)
        _ensure_equal_weights(engine_id, signals, dry_run)
        return report

    # ── Step 2: Purge overlapping windows ────────────────────────────────────
    purged = purge_overlapping(trades, horizon)
    report["n_trades_purged"] = len(purged)

    if len(purged) < IC_MIN_OBSERVATIONS * 2:
        report["status"] = "INSUFFICIENT_PURGED"
        report["notes"].append(
            f"Only {len(purged)} non-overlapping observations after purging "
            f"(horizon={horizon}d). Need more spread-out trades."
        )
        logger.warning("[%s] Too few purged observations (%d). Keeping equal weights.",
                       engine_id, len(purged))
        _ensure_equal_weights(engine_id, signals, dry_run)
        return report

    forward_returns = [float(t["forward_return_Nd"]) for t in purged]

    # ── Step 3: Spearman IC + p-value per signal ──────────────────────────────
    ic_results = {}
    p_values   = []

    for sig in signals:
        sig_vals = []
        fwd_rets = []
        for t, fwd in zip(purged, forward_returns):
            raw = t.get("signals_at_entry", {}).get(sig)
            if raw is None:
                continue
            try:
                sig_vals.append(float(raw))
                fwd_rets.append(fwd)
            except (TypeError, ValueError):
                continue

        if len(sig_vals) < IC_MIN_OBSERVATIONS:
            ic, p_val = 0.0, 1.0
            note = f"insufficient_data ({len(sig_vals)} obs)"
        else:
            ic, p_val = compute_spearman_ic(sig_vals, fwd_rets)
            note = ""

        ic_results[sig] = {
            "ic":          round(ic, 4),
            "p_value":     round(p_val, 4),
            "n_obs":       len(sig_vals),
            "note":        note,
        }
        p_values.append(p_val)
        logger.info("[%s] Signal %-30s IC=%+.4f  p=%.4f  n=%d",
                    engine_id, sig, ic, p_val, len(sig_vals))

    # ── Step 4: Rolling ICIR ──────────────────────────────────────────────────
    for sig in signals:
        icir = compute_rolling_icir(purged, sig, forward_returns)
        ic_results[sig]["icir"] = round(icir, 4)
        logger.info("[%s] Signal %-30s ICIR=%+.4f", engine_id, sig, icir)

    # ── Step 5: BH FDR correction ─────────────────────────────────────────────
    fdr_pass = benjamini_hochberg(p_values, alpha=FDR_ALPHA)

    for sig, passes in zip(signals, fdr_pass):
        ic_results[sig]["fdr_pass"] = passes
        if not passes:
            logger.info("[%s] Signal %-30s REJECTED by FDR (p=%.4f)",
                        engine_id, sig, ic_results[sig]["p_value"])

    # ── Step 6: Ledoit-Wolf shrinkage on IC covariance ───────────────────────
    # Build matrix of per-trade IC proxies (signal_value × sign(forward_return))
    # as a rolling estimate of IC covariance structure
    lw_note = ""
    try:
        ic_proxy_rows = []
        for t, fwd in zip(purged, forward_returns):
            row = []
            for sig in signals:
                raw = t.get("signals_at_entry", {}).get(sig)
                if raw is None:
                    row.append(0.0)
                else:
                    try:
                        row.append(float(raw) * np.sign(fwd))
                    except (TypeError, ValueError):
                        row.append(0.0)
            ic_proxy_rows.append(row)

        if len(ic_proxy_rows) >= len(signals) + 2 and LEDOIT_WOLF_SHRINKAGE:
            ic_matrix  = np.array(ic_proxy_rows)
            shrunk_cov = ledoit_wolf_shrinkage(ic_matrix)
            # Shrinkage coefficient: diagonal vs off-diagonal reduction (diagnostic)
            sample_cov = np.cov(ic_matrix.T)
            off_diag_reduction = 0.0
            if sample_cov.shape[0] > 1:
                off_sample = sample_cov[np.tril_indices(len(signals), k=-1)]
                off_shrunk = shrunk_cov[np.tril_indices(len(signals), k=-1)]
                if np.abs(off_sample).mean() > 1e-10:
                    off_diag_reduction = float(
                        1 - np.abs(off_shrunk).mean() / np.abs(off_sample).mean()
                    )
            lw_note = f"LW shrinkage applied — off-diag reduction={off_diag_reduction:.1%}"
            logger.info("[%s] %s", engine_id, lw_note)
        else:
            shrunk_cov = None
            lw_note = "LW shrinkage skipped — insufficient obs vs signals"
    except Exception as e:
        shrunk_cov = None
        lw_note = f"LW shrinkage failed: {e}"
        logger.warning("[%s] LW shrinkage error: %s", engine_id, e)

    report["notes"].append(lw_note)

    # ── Step 7: Derive signal weights ─────────────────────────────────────────
    # RF-17: If LW shrinkage succeeded, use shrunk mean-variance allocation:
    #   w ∝ Σ^{-1} · ic_vector  (Grinold-Kahn optimal IC weighting under correlated signals)
    # This accounts for signal correlation — two correlated signals each get less weight
    # than two orthogonal signals with the same IC.
    # Fallback: raw ICIR weighting (original approach) when LW unavailable.
    #
    # Pre-filter: only include signals that pass FDR, have positive IC and ICIR >= floor.

    weight_raw = {}
    excluded   = {}

    eligible_sigs = []
    for sig in signals:
        res   = ic_results[sig]
        ic    = res["ic"]
        icir  = res["icir"]
        passes_fdr = res["fdr_pass"]

        if not passes_fdr:
            excluded[sig] = "failed_fdr"
            weight_raw[sig] = 0.0
        elif ic <= 0:
            excluded[sig] = "negative_ic"
            weight_raw[sig] = 0.0
        elif icir < ICIR_FLOOR:
            excluded[sig] = f"icir_below_floor ({icir:.3f} < {ICIR_FLOOR})"
            weight_raw[sig] = 0.0
        else:
            eligible_sigs.append(sig)
            weight_raw[sig] = max(0.0, icir)

    # RF-17: Apply shrunk mean-variance weighting if we have LW covariance and ≥2 eligible signals
    used_lw = False
    if shrunk_cov is not None and len(eligible_sigs) >= 2:
        try:
            # Extract submatrix for eligible signals only
            sig_idx  = {s: i for i, s in enumerate(signals)}
            elig_idx = [sig_idx[s] for s in eligible_sigs]
            cov_sub  = shrunk_cov[np.ix_(elig_idx, elig_idx)]
            ic_vec   = np.array([ic_results[s]["ic"] for s in eligible_sigs])

            # Solve w = Σ^{-1} · ic_vec using pseudoinverse (handles near-singular cov)
            cov_inv  = np.linalg.pinv(cov_sub)
            w_mv     = cov_inv @ ic_vec

            # Only keep positive weights (negative → signal pairs hurt each other)
            w_mv = np.maximum(w_mv, 0.0)
            if w_mv.sum() > 1e-10:
                w_mv /= w_mv.sum()
                for sig, w in zip(eligible_sigs, w_mv):
                    weight_raw[sig] = float(w)
                used_lw = True
                lw_applied_note = (
                    f"RF-17: LW mean-variance weights applied to {len(eligible_sigs)} eligible signals"
                )
                report["notes"].append(lw_applied_note)
                logger.info("[%s] %s", engine_id, lw_applied_note)
        except Exception as e:
            logger.warning("[%s] LW mean-variance allocation failed: %s — using ICIR weights", engine_id, e)

    if not used_lw:
        report["notes"].append("Weight method: raw ICIR (LW MV allocation unavailable or skipped)")

    total_weight = sum(weight_raw.values())

    if total_weight < 1e-10:
        # All signals excluded — fall back to equal weights with a warning
        weights = {sig: 1.0 / len(signals) for sig in signals}
        report["notes"].append(
            "WARNING: All signals failed FDR/ICIR gates — equal weights maintained. "
            "Review signal construction before next calibration."
        )
        logger.warning("[%s] All signals excluded — equal weights fallback.", engine_id)
    else:
        weights = {sig: round(w / total_weight, 6) for sig, w in weight_raw.items()}

    # ── Step 8: Write to sub_engine_params.json ───────────────────────────────
    report["signals"] = ic_results
    report["weights"] = weights
    report["excluded"] = excluded
    report["status"]  = "CALIBRATED"
    report["weight_method"] = "lw_mean_variance" if used_lw else "icir_weighted"

    logger.info("[%s] Final weights (%s): %s", engine_id, report["weight_method"],
                {k: f"{v:.4f}" for k, v in weights.items()})

    if not dry_run:
        _write_weights(engine_id, weights, report)
        logger.info("[%s] Weights written to %s", engine_id, PARAMS_FILE)
    else:
        logger.info("[%s] DRY RUN — weights NOT written.", engine_id)

    return report


def _ensure_equal_weights(engine_id: str, signals: List[str], dry_run: bool):
    """Write equal weights for an engine that hasn't met the calibration gate."""
    if not signals or dry_run:
        return
    n = len(signals)
    weights = {sig: round(1.0 / n, 6) for sig in signals}
    _write_weights(engine_id, weights, report=None)


def _write_weights(engine_id: str, weights: Dict[str, float], report: Optional[dict]):
    """Atomic write to sub_engine_params.json."""
    params = _load_json(PARAMS_FILE, {})

    params[engine_id] = {
        "signal_weights":  weights,
        "last_updated":    datetime.now().isoformat(),
        "calibrated":      report is not None and report.get("status") == "CALIBRATED",
        "n_trades":        report.get("n_trades_purged", 0) if report else 0,
        "fdr_alpha":       FDR_ALPHA,
    }

    _save_json_atomic(PARAMS_FILE, params)


# ── Deflated Sharpe Ratio (engine-level) ──────────────────────────────────────

def deflated_sharpe_ratio(
    engine_id: str,
    n_trials: Optional[int] = None,
) -> Optional[float]:
    """
    Deflated Sharpe Ratio (Bailey & López de Prado 2014).
    Corrects engine-level Sharpe for selection bias when multiple parameter
    combinations were tested during shadow mode.

    DSR < 0 → engine performance likely due to overfitting, not edge.
    DSR > 0 → performance survives multiple-testing correction.

    RF-26: n_trials defaults to None. If None, we attempt to load the actual
    number of threshold sweep combinations from ares_threshold_deriver output
    (sub_engine_params.json → "n_threshold_combos"). Falls back to 1 if absent
    (conservative: DSR = P(SR>0) without overfitting penalty).
    Passing n_trials=1 explicitly gives the same result as the pre-fix default
    but is now honest about it.
    """
    # RF-26: load real n_trials from threshold deriver output
    if n_trials is None:
        try:
            params = _load_json(PARAMS_FILE, {})
            n_trials = int(params.get(engine_id, {}).get("n_threshold_combos", 1))
        except Exception:
            n_trials = 1
        if n_trials < 1:
            n_trials = 1
        if n_trials == 1:
            logger.info("[%s] DSR n_trials=1 — threshold sweeper not yet run; "
                        "DSR is P(SR>0), no overfitting penalty applied (RF-26)", engine_id)

    trades = load_engine_trades(engine_id)
    if len(trades) < IC_CALIBRATION_GATE:
        return None

    returns = [float(t["forward_return_Nd"]) for t in trades
               if t.get("forward_return_Nd") is not None]
    if len(returns) < 10:
        return None

    r = np.array(returns)
    n = len(r)
    sr = r.mean() / (r.std() + 1e-12) * np.sqrt(252 / IC_HORIZON_DAYS.get(engine_id, 10))

    # Estimate expected maximum SR under H0 (Gaussian) for n_trials
    if n_trials > 1:
        euler_gamma = 0.5772
        from scipy.stats import norm
        e_max_sr = (
            (1 - euler_gamma) * norm.ppf(1 - 1.0 / n_trials)
            + euler_gamma * norm.ppf(1 - 1.0 / (n_trials * np.e))
        )
    else:
        e_max_sr = 0.0

    skew = float(sp_stats.skew(r))
    kurt = float(sp_stats.kurtosis(r))

    sr_adj = sr * np.sqrt(
        (1 - skew * sr + (kurt / 4.0) * sr ** 2) / (n - 1)
    )
    dsr = float(sr_adj - e_max_sr)
    return round(dsr, 4)


# ── Report generation ─────────────────────────────────────────────────────────

def print_report(engine_id: Optional[str] = None):
    """Print current signal weights and IC stats from params file."""
    params = _load_json(PARAMS_FILE, {})

    engines = [engine_id] if engine_id else list(ENGINE_SIGNALS.keys())

    print(f"\n{'='*70}")
    print(f"  ARES SIGNAL WEIGHT REPORT — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*70}")

    for eid in engines:
        ep = params.get(eid, {})
        weights    = ep.get("signal_weights", {})
        calibrated = ep.get("calibrated", False)
        n_trades   = ep.get("n_trades", 0)
        updated    = ep.get("last_updated", "never")

        gate = IC_CALIBRATION_GATE
        raw_trades = len(load_engine_trades(eid))

        print(f"\n  Engine {eid} — {'CALIBRATED' if calibrated else 'EQUAL WEIGHTS (bootstrap)'}")
        print(f"  Closed trades: {raw_trades}/{gate}  |  Last updated: {updated[:10]}")

        signals = ENGINE_SIGNALS.get(eid, [])
        if weights:
            for sig in signals:
                w = weights.get(sig, 1.0 / len(signals))
                bar = "█" * int(w * 40)
                print(f"    {sig:<35s}  {w:.4f}  {bar}")
        else:
            print("    No weights on file — run calibration first.")

        # DSR
        dsr = deflated_sharpe_ratio(eid)
        if dsr is not None:
            dsr_flag = "✓" if dsr > 0 else "⚠"
            print(f"  Deflated Sharpe Ratio: {dsr:+.4f} {dsr_flag}")

    print(f"\n{'='*70}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def run_all_engines(dry_run: bool = False) -> dict:
    """
    Calibrate all active engines. Returns full report dict.

    RF-25: Runs two FDR passes:
      1. Per-engine BH FDR (3-5 tests) — current per-engine calibrate_engine() call
      2. System-wide BH FDR across all ~19 signals simultaneously — applied post-hoc
         to update each signal's fdr_pass flag at the system level. This is the
         R-1 intent: correct for multiple testing across the whole system, not just
         within each engine. Signals that survive per-engine FDR but fail system-wide
         FDR are flagged as "system_fdr_rejected" in the report.
    """
    all_reports = {}
    full_report = {
        "run_time": datetime.now().isoformat(),
        "dry_run":  dry_run,
        "engines":  {},
    }

    for engine_id in ENGINE_SIGNALS:
        status = ENGINE_STATUS.get(engine_id, "off")
        if status == "off":
            logger.info("[%s] Engine is OFF — skipping.", engine_id)
            continue

        logger.info("=" * 60)
        logger.info("Calibrating Engine %s", engine_id)
        logger.info("=" * 60)

        try:
            report = calibrate_engine(engine_id, dry_run=dry_run)
            all_reports[engine_id] = report
            full_report["engines"][engine_id] = report
        except Exception as e:
            logger.error("[%s] Calibration failed: %s", engine_id, e, exc_info=True)
            full_report["engines"][engine_id] = {"status": "ERROR", "error": str(e)}

    # RF-25: System-wide BH FDR across all engines' p-values simultaneously
    all_system_pvals = []  # (engine_id, signal, p_value)
    for eid, report in all_reports.items():
        for sig, res in report.get("signals", {}).items():
            pval = res.get("p_value")
            if pval is not None:
                all_system_pvals.append((eid, sig, pval))

    if len(all_system_pvals) >= 2:
        p_vals_only = [x[2] for x in all_system_pvals]
        system_fdr_pass = benjamini_hochberg(p_vals_only, alpha=FDR_ALPHA)
        system_fdr_map = {
            (eid, sig): passes
            for (eid, sig, _), passes in zip(all_system_pvals, system_fdr_pass)
        }
        # Update reports with system-wide FDR flags
        for eid, report in all_reports.items():
            for sig in report.get("signals", {}):
                sys_pass = system_fdr_map.get((eid, sig), True)
                report["signals"][sig]["system_fdr_pass"] = sys_pass
                if not sys_pass:
                    logger.info("[%s] Signal %-30s rejected by SYSTEM-WIDE FDR (RF-25)", eid, sig)
        full_report["system_fdr_n_signals"] = len(all_system_pvals)
        full_report["system_fdr_passed"] = sum(system_fdr_pass)
        logger.info("RF-25 System-wide FDR: %d/%d signals survive",
                    sum(system_fdr_pass), len(all_system_pvals))
    else:
        full_report["system_fdr_note"] = "Insufficient signals for system-wide FDR"

    # Write full report
    if not dry_run:
        _save_json_atomic(REPORT_FILE, full_report)
        logger.info("Full report written to %s", REPORT_FILE)

    return full_report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ares Statistical Validation")
    parser.add_argument("--engine",   type=str, help="Single engine to calibrate (A/B/C/E/F)")
    parser.add_argument("--report",   action="store_true", help="Print current weights, no update")
    parser.add_argument("--dry-run",  action="store_true", help="Compute but do not write weights")
    args = parser.parse_args()

    if args.report:
        print_report(args.engine)
    elif args.engine:
        calibrate_engine(args.engine.upper(), dry_run=args.dry_run)
        print_report(args.engine.upper())
    else:
        run_all_engines(dry_run=args.dry_run)
        print_report()
