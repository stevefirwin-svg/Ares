"""
hamilton_filter.py — Hamilton HMM Macro Regime Filter
=======================================================
Replaces the vote-count macro_context.py regime classifier with a
Hidden Markov Model (Hamilton, 1989). Output is a continuous probability
vector, not a discrete label — eliminates cliff-switching.

Model:
  4 hidden states: RISK_ON (0), NEUTRAL (1), RISK_OFF (2), CRISIS (3)
  4 observable features per day:
    - SPY 20-day return (trend signal)
    - VIX level (volatility/fear signal)
    - HYG/LQD ratio normalized to 60d mean (credit signal)
    - Sector breadth: % of sector ETFs above their 50d MA (breadth signal)

  HMM emission model: GaussianHMM with diagonal covariance matrices.
    Rationale: 'full' covariance on ~500 obs with 4 features produces near-one-hot
    posteriors via over-separation (RF-4). Diagonal covariance reduces in-sample
    separation → smoother posterior blending for ENGINE_REGIME_GATE.

  Transition matrix: initialized with domain-informed priors, refined by data.
    Diagonal = 0.97 (raised from 0.92) for stronger regime stickiness (RF-4).

  Posterior temperature smoothing: posterior logits divided by TEMP=2.5 before
    softmax to soften near-one-hot outputs (RF-4). Applied only in predict_probs.

Output:
  probability vector [P(RISK_ON), P(NEUTRAL), P(RISK_OFF), P(CRISIS)]
  Sum = 1.0. Used as a continuous weight in ENGINE_REGIME_GATE.

Example:
  P = [0.1, 0.3, 0.6, 0.0]
  Engine A gate (RISK_ON=1.0, NEUTRAL=0.75, RISK_OFF=0.0, CRISIS=0.0):
  effective_kelly = 0.1×1.0 + 0.3×0.75 + 0.6×0.0 + 0.0×0.0 = 0.325

  vs vote-count (snapped to RISK_OFF → 0.0 for Engine A):
  effective_kelly = 0.0  ← cliff effect eliminated

Cold start:
  No history → falls back to discrete vote-count for first 60 observations.
  Transitions to HMM once 60 trading days of observations are accumulated.

Implementation notes:
  - GaussianHMM n_components=4, n_iter=200, covariance_type='diag'  [RF-4]
  - Features normalized (z-score) before fitting to improve convergence
  - Transition matrix diagonal raised to 0.97 for regime stickiness  [RF-4]
  - Posterior temperature-smoothed (T=2.5) before ENGINE_REGIME_GATE  [RF-4]
  - Sector breadth added as 4th feature (RF-5)
  - State→regime mapping uses multi-feature stress rank  [RF-6]
  - Re-fitted weekly on accumulated observation history (max 504 days = 2yr)
  - Model persisted to hamilton_model.pkl via pickle; observations to
    hamilton_observations.json

Run standalone:
    python hamilton_filter.py              # update and print current probabilities
    python hamilton_filter.py --fit        # force model refit from observations
    python hamilton_filter.py --status     # print current regime probabilities
    python hamilton_filter.py --history N  # show last N days of regime evolution

Research basis:
    Hamilton (1989) — "A New Approach to the Economic Analysis of Nonstationary
      Time Series and the Business Cycle"
    Kim & Nelson (1999) — State-Space Models with Regime Switching
    Ang & Timmermann (2012) — Regime Changes and Financial Markets
"""

import json
import logging
import os
import pickle
import argparse
import warnings
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

import numpy as np
import yfinance as yf
from hmmlearn import hmm

logging.basicConfig(level=logging.INFO, format="%(asctime)s [HMM] %(message)s")
logger = logging.getLogger("ares.hamilton")

MODEL_FILE       = "hamilton_model.pkl"
OBS_FILE         = "hamilton_observations.json"
MACRO_FILE       = "macro_context.json"
MIN_OBS_FOR_HMM  = 60    # fall back to vote-count below this
MAX_OBS_WINDOW   = 504   # 2 years of trading days
REFIT_EVERY_DAYS = 5     # refit model weekly

N_STATES    = 4
STATE_NAMES = ["RISK_ON", "NEUTRAL", "RISK_OFF", "CRISIS"]

# ── Feature extraction ────────────────────────────────────────────────────────

def fetch_features(lookback_days: int = 504) -> Optional[np.ndarray]:
    """
    Build observation matrix for the HMM.

    Features (4 columns):
      0: SPY 20-day return          — trend signal
      1: VIX level (log)            — fear/vol signal
      2: HYG/LQD ratio (norm)       — credit signal
      3: Sector breadth (norm)      — % sector ETFs above 50d MA, z-scored  [RF-5]

    Returns (n_days, 4) array, most recent last.
    Returns None on data failure.
    """
    # Sector ETFs for breadth computation  [RF-5]
    SECTOR_ETFS = ["XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]

    try:
        logger.info("Fetching HMM features (SPY, VIX, HYG, LQD, sector breadth)...")
        import pandas as pd

        def _fetch(ticker):
            s = yf.Ticker(ticker).history(period="3y")["Close"]
            # Strip timezone so series align cleanly on date
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            return s

        spy = _fetch("SPY")
        vix = _fetch("^VIX")
        hyg = _fetch("HYG")
        lqd = _fetch("LQD")

        # Fetch sector ETFs for breadth  [RF-5]
        sector_closes = {}
        for etf in SECTOR_ETFS:
            try:
                s = yf.Ticker(etf).history(period="3y")["Close"]
                s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
                sector_closes[etf] = s
            except Exception:
                pass  # partial set is acceptable; breadth degrades gracefully

        # Align to common dates
        df = pd.DataFrame({
            "spy": spy, "vix": vix, "hyg": hyg, "lqd": lqd
        }).dropna().tail(lookback_days)

        if len(df) < MIN_OBS_FOR_HMM:
            logger.warning("Only %d days of data — need %d", len(df), MIN_OBS_FOR_HMM)
            return None

        # Feature 1: SPY 20-day return
        spy_ret20 = df["spy"].pct_change(20).fillna(0)

        # Feature 2: VIX level (log-transformed for normality)
        vix_log = np.log(df["vix"].clip(lower=8.0))

        # Feature 3: HYG/LQD normalized ratio
        ratio      = df["hyg"] / df["lqd"]
        ratio_mean = ratio.rolling(60, min_periods=20).mean()
        ratio_norm = (ratio / ratio_mean).fillna(1.0)

        # Feature 4: Sector breadth — % of sector ETFs above their 50d MA  [RF-5]
        # Normalized as z-score vs 60d rolling mean to remove level drift
        if len(sector_closes) >= 4:
            breadth_series = []
            for etf, s in sector_closes.items():
                s_aligned = s.reindex(df.index, method="ffill")
                ma50 = s_aligned.rolling(50, min_periods=20).mean()
                above = (s_aligned > ma50).astype(float)
                breadth_series.append(above)
            breadth_raw = pd.concat(breadth_series, axis=1).mean(axis=1)
            breadth_mean = breadth_raw.rolling(60, min_periods=20).mean().fillna(0.5)
            breadth_std  = breadth_raw.rolling(60, min_periods=20).std().fillna(0.15)
            breadth_std  = breadth_std.clip(lower=1e-4)
            breadth_norm = ((breadth_raw - breadth_mean) / breadth_std).fillna(0.0)
            logger.info("Sector breadth computed from %d ETFs", len(sector_closes))
        else:
            # Graceful fallback: breadth = 0 (z-scored, neutral)  [RF-5]
            logger.warning("Only %d sector ETFs fetched — breadth feature set to zero", len(sector_closes))
            breadth_norm = pd.Series(0.0, index=df.index)

        features = np.column_stack([
            spy_ret20.values,
            vix_log.values,
            ratio_norm.values,
            breadth_norm.reindex(df.index).fillna(0.0).values,
        ])
        # Remove any rows with NaN (from rolling windows)
        mask = ~np.isnan(features).any(axis=1)
        features = features[mask]

        logger.info("Features built: %d observations × %d features", *features.shape)
        return features

    except Exception as e:
        logger.error("Feature fetch failed: %s", e)
        return None


def normalize_features(X: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Z-score normalize features. Returns (X_norm, means, stds)."""
    means = X.mean(axis=0)
    stds  = X.std(axis=0)
    stds  = np.where(stds < 1e-8, 1.0, stds)
    return (X - means) / stds, means, stds


# ── Model initialization ──────────────────────────────────────────────────────

def _init_transition_matrix() -> np.ndarray:
    """
    Domain-informed initial transition matrix.
    Regimes are sticky (persist for weeks/months), hence high diagonal.
    Transitions lean toward adjacent regimes (RISK_ON ↔ NEUTRAL ↔ RISK_OFF ↔ CRISIS).

    Rows = from-state, Cols = to-state.
    RISK_ON(0) → NEUTRAL(1) most likely transition out.
    CRISIS(3) → RISK_OFF(2) most likely recovery path.

    TODO:DERIVE — transition priors from empirical regime duration analysis
    (e.g., NBER recession dating, VIX spike duration distributions).
    Current values are informed guesses, not statistically derived.
    """
    # RF-4: Diagonal raised to 0.97 (from 0.92) — stickier priors reduce EM
    # over-separation that causes near-one-hot posteriors.
    A = np.array([
        #  RISK_ON  NEUTRAL  RISK_OFF  CRISIS
        [0.97,    0.02,    0.008,    0.002],  # from RISK_ON
        [0.02,    0.97,    0.009,    0.001],  # from NEUTRAL
        [0.005,   0.015,   0.97,     0.01 ],  # from RISK_OFF
        [0.002,   0.005,   0.023,    0.97 ],  # from CRISIS
    ])
    # Normalize rows to sum to 1.0 (floating point safety)
    return A / A.sum(axis=1, keepdims=True)


def _init_means_for_states() -> np.ndarray:
    """
    Initial emission means per state.
    Based on known empirical behavior of SPY returns, VIX, credit spreads,
    and sector breadth across market regimes.

    Features: [SPY 20d ret, log(VIX), HYG/LQD norm, sector breadth z-score]

    Note: These are z-scored features — values here are pre-normalization intuitions.
    We correct for this in fit() by normalizing before fitting but leaving means in
    normalized space for hmmlearn.
    """
    # SPY 20d ret:  RISK_ON ≈ +4%, NEUTRAL ≈ +1%, RISK_OFF ≈ -2%, CRISIS ≈ -8%
    # log(VIX):     RISK_ON ≈ 2.7 (VIX~15), NEUTRAL ≈ 2.9 (~18), RISK_OFF ≈ 3.2 (~25), CRISIS ≈ 3.6 (~37)
    # HYG/LQD norm: RISK_ON ≈ 1.02, NEUTRAL ≈ 1.00, RISK_OFF ≈ 0.97, CRISIS ≈ 0.93
    # Breadth z:    RISK_ON ≈ +1.0 (broad advance), NEUTRAL ≈ 0.0, RISK_OFF ≈ -0.8, CRISIS ≈ -1.5  [RF-5]
    return np.array([
        [ 0.04,  2.70,  1.02,  1.0],   # RISK_ON
        [ 0.01,  2.90,  1.00,  0.0],   # NEUTRAL
        [-0.02,  3.20,  0.97, -0.8],   # RISK_OFF
        [-0.08,  3.60,  0.93, -1.5],   # CRISIS
    ])


# ── Model fit and predict ─────────────────────────────────────────────────────

def fit_model(X: np.ndarray) -> hmm.GaussianHMM:
    """
    Fit a 4-state GaussianHMM on normalized features.
    Uses domain-informed initialization to help EM find the right solution.
    """
    X_norm, means, stds = normalize_features(X)

    model = hmm.GaussianHMM(
        n_components     = N_STATES,
        covariance_type  = "diag",   # RF-4: 'full' → 'diag' to reduce over-separation
        n_iter           = 200,
        tol              = 1e-4,
        random_state     = 42,
        init_params      = "",    # we set params manually
        params           = "mct", # optimize means, covars, transmat
    )

    # Set informed initialization
    model.startprob_   = np.array([0.40, 0.40, 0.15, 0.05])
    model.transmat_    = _init_transition_matrix()

    # Normalize the initial means into feature space
    init_means = _init_means_for_states()
    init_means_norm = (init_means - means) / stds
    model.means_       = init_means_norm

    # Initialize covariances to ones (diagonal format: n_states × n_features)
    # hmmlearn expects (n_components, n_features) for diag covariance
    model.covars_      = np.ones((N_STATES, X_norm.shape[1]))

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model.fit(X_norm)

    logger.info("HMM fit complete. Converged=%s  Score=%.2f",
                "yes" if model.monitor_.converged else "no",
                model.score(X_norm))

    # Persist normalization params in model for later use
    model._ares_feature_means = means
    model._ares_feature_stds  = stds

    return model


def _state_order_to_regime(model: hmm.GaussianHMM) -> List[int]:
    """
    Map HMM state indices to regime indices based on emission means.
    Regime ordering: RISK_ON=0 (lowest stress) → CRISIS=3 (highest stress).

    RF-6 fix: Sort by composite stress rank = z(−SPY_ret) + z(log_VIX) + z(−credit)
    rather than VIX alone. VIX-only sorting can confuse RISK_ON vs RISK_OFF when
    both regimes share similar volatility levels (e.g. low-vol grind-down).

    Feature indices: 0=SPY_ret, 1=log_VIX, 2=HYG/LQD_norm, 3=breadth_z
    """
    means = model.means_   # (n_states, n_features) — already in normalized space

    # Normalize each feature's state means to z-score across states
    def _col_z(col):
        s = col.std()
        if s < 1e-8:
            return np.zeros_like(col)
        return (col - col.mean()) / s

    spy_z     = _col_z(means[:, 0])   # higher = more RISK_ON → negate for stress
    vix_z     = _col_z(means[:, 1])   # higher = more stressed
    credit_z  = _col_z(means[:, 2])   # higher ratio = tighter credit → negate for stress
    # Feature 3 (breadth) may not exist if model was fit on 3 features (backward compat)
    if means.shape[1] >= 4:
        breadth_z = _col_z(means[:, 3])  # higher breadth = more RISK_ON → negate for stress
        stress_rank = -spy_z + vix_z - credit_z - breadth_z
    else:
        stress_rank = -spy_z + vix_z - credit_z

    # States sorted by ascending stress → RISK_ON(0) to CRISIS(3)
    order = np.argsort(stress_rank)
    mapping = np.empty(N_STATES, dtype=int)
    for regime_idx, state_idx in enumerate(order):
        mapping[state_idx] = regime_idx
    return mapping.tolist()


POSTERIOR_TEMPERATURE = 2.5  # RF-4: Soften near-one-hot posteriors
# Temperature > 1 flattens the posterior distribution.
# Derivation: empirical calibration target — no regime should exceed 0.80 in steady state.
# TODO:DERIVE — tune from live regime history once 90+ days of posteriors accumulate.


def predict_probs(model: hmm.GaussianHMM, X_latest: np.ndarray) -> np.ndarray:
    """
    Given the latest observation(s), return posterior state probabilities.
    Uses forward algorithm posteriors.

    RF-4 fix: applies temperature smoothing to prevent near-one-hot posteriors
    that recreate cliff-switching despite the probability blending design.

    Returns array of shape (4,) = [P(RISK_ON), P(NEUTRAL), P(RISK_OFF), P(CRISIS)].
    """
    means = getattr(model, "_ares_feature_means", X_latest.mean(axis=0))
    stds  = getattr(model, "_ares_feature_stds",  X_latest.std(axis=0))
    stds  = np.where(stds < 1e-8, 1.0, stds)

    X_norm = (X_latest - means) / stds

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        posteriors = model.predict_proba(X_norm)  # (n_obs, n_states)

    # Last row = most recent observation
    state_probs_raw = posteriors[-1]

    # RF-4: Temperature-smooth the posterior logits before softmax
    # log(p) / T then softmax — flattens over-confident distributions
    log_probs = np.log(np.clip(state_probs_raw, 1e-12, 1.0))
    log_probs_tempered = log_probs / POSTERIOR_TEMPERATURE
    log_probs_tempered -= log_probs_tempered.max()  # numerical stability
    state_probs = np.exp(log_probs_tempered)
    state_probs /= state_probs.sum()

    # Remap to [RISK_ON, NEUTRAL, RISK_OFF, CRISIS]
    regime_mapping = _state_order_to_regime(model)
    regime_probs   = np.zeros(N_STATES)
    for state_idx, regime_idx in enumerate(regime_mapping):
        regime_probs[regime_idx] += state_probs[state_idx]

    regime_probs /= regime_probs.sum()   # normalize for float safety
    return regime_probs


# ── Persistence ───────────────────────────────────────────────────────────────

def load_model() -> Optional[hmm.GaussianHMM]:
    if not os.path.exists(MODEL_FILE):
        return None
    try:
        with open(MODEL_FILE, "rb") as f:
            return pickle.load(f)
    except Exception as e:
        logger.warning("Model load failed: %s", e)
        return None


def save_model(model: hmm.GaussianHMM):
    tmp = MODEL_FILE + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(model, f)
    os.replace(tmp, MODEL_FILE)
    logger.info("HMM model saved → %s", MODEL_FILE)


def _model_age_days() -> float:
    if not os.path.exists(MODEL_FILE):
        return float("inf")
    import time
    return (time.time() - os.path.getmtime(MODEL_FILE)) / 86400


# ── Vote-count fallback (cold start) ─────────────────────────────────────────

def _vote_count_probs(spy_ret20: float, vix: float,
                       credit_norm: float) -> np.ndarray:
    """
    Fallback to vote-count regime classification.
    Returns soft probability vector (not binary) by scoring each regime.
    Used when fewer than MIN_OBS_FOR_HMM days of history exist.
    """
    score = 0
    if spy_ret20 > 0.02:
        score += 2
    elif spy_ret20 > 0:
        score += 1
    elif spy_ret20 < -0.02:
        score -= 2
    else:
        score -= 1

    if vix < 18:
        score += 1
    elif vix > 30:
        score -= 2
    elif vix > 25:
        score -= 1

    if credit_norm > 1.01:
        score += 1
    elif credit_norm < 0.96:
        score -= 2
    elif credit_norm < 0.98:
        score -= 1

    # Convert to soft probabilities (temperature-scaled)
    if vix > 35:
        return np.array([0.02, 0.05, 0.20, 0.73])   # CRISIS
    if score >= 3:
        return np.array([0.70, 0.22, 0.07, 0.01])   # RISK_ON
    elif score >= 1:
        return np.array([0.30, 0.55, 0.13, 0.02])   # NEUTRAL
    elif score >= -1:
        return np.array([0.10, 0.25, 0.57, 0.08])   # RISK_OFF
    else:
        return np.array([0.03, 0.10, 0.45, 0.42])   # CRISIS


# ── Main update ───────────────────────────────────────────────────────────────

def update_macro_context(force_refit: bool = False) -> dict:
    """
    Fetch latest macro features, run HMM inference, write macro_context.json.

    Returns the updated macro context dict.
    """
    X = fetch_features(lookback_days=MAX_OBS_WINDOW)

    if X is None or len(X) < 5:
        logger.error("Feature fetch failed — cannot update macro context")
        return {}

    n_obs  = len(X)
    regime_probs = None

    if n_obs < MIN_OBS_FOR_HMM:
        logger.warning("Cold start: %d obs < %d required for HMM — using vote-count fallback",
                       n_obs, MIN_OBS_FOR_HMM)
        # Use last row for vote-count
        last = X[-1]
        spy_ret20   = last[0]
        vix_level   = float(np.exp(last[1]))  # un-log
        credit_norm = last[2]
        regime_probs = _vote_count_probs(spy_ret20, vix_level, credit_norm)
        method = "vote_count_fallback"

    else:
        # Load or refit model
        model = load_model()
        needs_refit = (model is None) or force_refit or (_model_age_days() >= REFIT_EVERY_DAYS)

        if needs_refit:
            logger.info("Fitting HMM on %d observations...", n_obs)
            model = fit_model(X)
            save_model(model)

        regime_probs = predict_probs(model, X)
        method = "hmm_gaussian"

    # Regime label = argmax of probability vector
    regime_idx   = int(np.argmax(regime_probs))
    regime_label = STATE_NAMES[regime_idx]

    # macro_score: weighted sum on [-1, +1] scale
    weights = np.array([1.0, 0.33, -0.33, -1.0])
    macro_score = float(np.dot(regime_probs, weights))

    # Agent summary
    prob_str = "  ".join(f"{STATE_NAMES[i]}={regime_probs[i]:.3f}" for i in range(N_STATES))
    agent_summary = f"MACRO (HMM): {regime_label} | {prob_str} | score={macro_score:+.3f}"
    logger.info(agent_summary)

    # Pull raw signal values for logging
    last = X[-1]
    spy_ret20    = float(last[0]) * 100  # to percent
    vix_level    = float(np.exp(last[1]))
    credit_norm  = float(last[2])
    breadth_z    = float(last[3]) if len(last) >= 4 else 0.0  # RF-5

    context = {
        "timestamp":            datetime.now(timezone.utc).isoformat(),
        "macro_regime":         regime_label,
        "macro_score":          round(macro_score, 4),
        "regime_probs": {
            "RISK_ON":  round(float(regime_probs[0]), 4),
            "NEUTRAL":  round(float(regime_probs[1]), 4),
            "RISK_OFF": round(float(regime_probs[2]), 4),
            "CRISIS":   round(float(regime_probs[3]), 4),
        },
        "method":               method,
        "n_observations":       n_obs,
        "signals": {
            "spy_ret_20d_pct":  round(spy_ret20, 2),
            "vix_level":        round(vix_level, 2),
            "credit_norm":      round(credit_norm, 4),
            "breadth_z":        round(breadth_z, 4),   # RF-5: now in model
        },
        "agent_summary":        agent_summary,
    }

    tmp = MACRO_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(context, f, indent=2)
    os.replace(tmp, MACRO_FILE)

    logger.info("macro_context.json updated → regime=%s  score=%+.4f",
                regime_label, macro_score)
    return context


def print_status():
    if not os.path.exists(MACRO_FILE):
        print("macro_context.json not found. Run without --status first.")
        return
    with open(MACRO_FILE) as f:
        ctx = json.load(f)

    probs = ctx.get("regime_probs", {})
    print(f"\n{'='*60}")
    print(f"  HAMILTON FILTER STATUS")
    print(f"  {ctx.get('timestamp','?')[:19]} UTC")
    print(f"{'='*60}")
    print(f"  Regime:      {ctx.get('macro_regime','?')}")
    print(f"  Score:       {ctx.get('macro_score',0):+.4f}")
    print(f"  Method:      {ctx.get('method','?')}  ({ctx.get('n_observations','?')} obs)")
    print()
    print(f"  Regime probabilities:")
    for regime, prob in probs.items():
        bar = "█" * int(prob * 30)
        print(f"    {regime:<10} {prob:.4f}  {bar}")
    print()
    print(f"  Raw signals:")
    for k, v in ctx.get("signals", {}).items():
        print(f"    {k:<26}: {v}")
    print(f"{'='*60}\n")


def print_history(n_days: int = 20):
    """Replay HMM on recent history to show regime evolution."""
    X = fetch_features(lookback_days=max(n_days + 60, 120))
    if X is None or len(X) < MIN_OBS_FOR_HMM:
        print("Not enough data for history.")
        return

    model = load_model()
    if model is None:
        print("No model saved. Run update first.")
        return

    means = getattr(model, "_ares_feature_means", X.mean(axis=0))
    stds  = getattr(model, "_ares_feature_stds",  X.std(axis=0))
    stds  = np.where(stds < 1e-8, 1.0, stds)
    X_norm = (X - means) / stds

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        posteriors = model.predict_proba(X_norm)

    regime_mapping = _state_order_to_regime(model)

    print(f"\n  {'Day':>4}  {'RISK_ON':>8}  {'NEUTRAL':>8}  {'RISK_OFF':>8}  {'CRISIS':>8}  Regime")
    print("  " + "-"*56)
    for i in range(-min(n_days, len(posteriors)), 0):
        state_probs   = posteriors[i]
        regime_probs  = np.zeros(N_STATES)
        for si, ri in enumerate(regime_mapping):
            regime_probs[ri] += state_probs[si]
        regime_idx   = int(np.argmax(regime_probs))
        regime_label = STATE_NAMES[regime_idx]
        print(f"  {i:>4}  {regime_probs[0]:>8.4f}  {regime_probs[1]:>8.4f}  "
              f"{regime_probs[2]:>8.4f}  {regime_probs[3]:>8.4f}  {regime_label}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ares Hamilton Filter")
    parser.add_argument("--fit",     action="store_true", help="Force model refit")
    parser.add_argument("--status",  action="store_true", help="Print current regime probabilities")
    parser.add_argument("--history", type=int, default=0, help="Show N days of regime history")
    args = parser.parse_args()

    if args.status:
        print_status()
    elif args.history > 0:
        print_history(args.history)
    else:
        update_macro_context(force_refit=args.fit)
        print_status()
