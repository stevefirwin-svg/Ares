"""
hold_monitor_a.py — Engine A Hold Health Scoring
==================================================
Momentum-weighted 5-layer health score for Engine A positions.

Engine A hold logic is trend-thesis driven — the question is not "is price
reverting?" but "is the momentum trend still intact and strengthening?"

The five layers and their weights (bootstrap — TODO:DERIVE from IC):

  Layer 1: TREND CLUSTER HEALTH (30%)
    Is the trend structurally intact?
    Measured by ADX level and direction: ADX > 25 and not declining rapidly.
    score = clip(adx_val / 50, 0, 1) × (1 - adx_decline_penalty)
    adx_decline_penalty = 0.4 if ADX has dropped > 3 points over 3 bars else 0.

  Layer 2: PRICE MOMENTUM (25%)
    Is price momentum accelerating or decelerating?
    Measured by ROC acceleration (ROC_5 - ROC_20).
    Positive roc_accel = momentum speeding up = healthy.
    score = logistic(roc_accel × 100) → [0, 1]
    Positive roc_accel → score > 0.5; negative → score < 0.5.

  Layer 3: MA STACK INTEGRITY (20%)
    Is the EMA 8 > 21 > 50 stack still holding?
    Full stack bullish → 1.0; E8 < E21 → 0.0; partial → 0.5.
    Binary structural check — MA stack collapse is the primary bearish signal.

  Layer 4: VOLUME CONFIRMATION (15%)
    Is institutional accumulation continuing?
    Compare recent 5d average volume to 20d average.
    Above-average volume in an uptrend = healthy accumulation.
    score = clip(recent_5d_avg / avg_20d, 0.5, 2.0) normalized to [0, 1]

  Layer 5: TIME REMAINING (10%)
    Days remaining as fraction of hold_target.
    score = max(0, 1 - days/hold_target)
    Decays linearly to 0 at hold_target.

ADX SLOPE PENALTY: Applied to final score.
  If ADX has dropped > 3 points over 3 bars: health -= 0.20
  This is the earliest warning signal for Engine A — ADX rolling over
  precedes price breakdown (and exit A-3) by 1-2 bars.

Total health score ∈ [-1, +1] via weighted sum normalized to [-1, 1].
Tiers: STRENGTHENING (> 0.20) / STABLE (0 to 0.20) / DECAYING (< 0) / EXHAUSTED (< -0.30)

Usage:
    from hold_monitor_a import score_engine_a_positions
    scores = score_engine_a_positions(positions, bars_map)

Not run standalone — called from ares_hold_monitor.py engine dispatch.
"""

import math
import logging
from datetime import datetime
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("ares.hold_a")

ENGINE_ID = "A"

LAYER_WEIGHTS = {
    "trend_cluster":    0.30,
    "price_momentum":   0.25,
    "ma_stack":         0.20,
    "volume_confirm":   0.15,
    "time_remaining":   0.10,
}

# ADX slope penalty — applied after weighted sum
ADX_SLOPE_LOOKBACK = 3          # bars over which to measure ADX change
ADX_SLOPE_PENALTY_THRESHOLD = -3.0   # ADX decline that triggers the penalty
ADX_SLOPE_HEALTH_PENALTY    = 0.20   # subtracted from final [0,1] score

TIER_STRENGTHENING = 0.20
TIER_STABLE        = 0.0
TIER_EXHAUSTED     = -0.30


# ── Helpers ───────────────────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, period: int = 14) -> float:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs()
    ], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if not math.isnan(val) else 0.0


def _compute_adx_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute ADX series for slope calculation."""
    if len(df) < period * 3:
        return pd.Series(dtype=float)

    h = df["high"].values
    l = df["low"].values
    c = df["close"].values

    plus_dm  = np.where((h[1:] - h[:-1]) > (l[:-1] - l[1:]),
                         np.maximum(h[1:] - h[:-1], 0), 0)
    minus_dm = np.where((l[:-1] - l[1:]) > (h[1:] - h[:-1]),
                         np.maximum(l[:-1] - l[1:], 0), 0)
    tr_arr = np.maximum(
        h[1:] - l[1:],
        np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1]))
    )

    def _smooth(arr, n):
        out = np.zeros(len(arr))
        if len(arr) < n:
            return out
        out[n-1] = arr[:n].sum()
        for i in range(n, len(arr)):
            out[i] = out[i-1] - out[i-1] / n + arr[i]
        return out

    atr_s = _smooth(tr_arr, period)
    pdi_s = _smooth(plus_dm, period)
    mdi_s = _smooth(minus_dm, period)

    with np.errstate(divide='ignore', invalid='ignore'):
        pdi = np.where(atr_s > 0, 100 * pdi_s / atr_s, 0)
        mdi = np.where(atr_s > 0, 100 * mdi_s / atr_s, 0)
        dx  = np.where((pdi + mdi) > 0,
                        100 * np.abs(pdi - mdi) / (pdi + mdi), 0)

    adx_arr = _smooth(dx, period)
    return pd.Series(adx_arr)


def _adx_now_and_slope(df: pd.DataFrame, lookback: int = ADX_SLOPE_LOOKBACK) -> Tuple[float, float]:
    """Returns (adx_now, change_over_lookback_bars). Negative = declining."""
    adx_series = _compute_adx_series(df)
    if len(adx_series) < lookback + 1:
        return 25.0, 0.0   # neutral fallback — assume trending
    adx_now  = float(adx_series.iloc[-1])
    adx_then = float(adx_series.iloc[-(lookback + 1)])
    return adx_now, adx_now - adx_then


def _roc_accel(df: pd.DataFrame) -> float:
    """ROC_5 - ROC_20. Positive = momentum accelerating."""
    c = df["close"]
    if len(c) < 22:
        return 0.0
    roc5  = float(c.iloc[-1] / c.iloc[-6])  - 1.0
    roc20 = float(c.iloc[-1] / c.iloc[-21]) - 1.0
    return roc5 - roc20


def _days_held(entry_date_str: str) -> int:
    try:
        entry = datetime.strptime(str(entry_date_str)[:10], "%Y-%m-%d").date()
        return (datetime.now().date() - entry).days
    except Exception:
        return 0


# ── Layer scorers ─────────────────────────────────────────────────────────────

def _score_trend_cluster(adx_val: float, adx_change: float) -> float:
    """
    ADX level normalized to [0,1], with a penalty if ADX is declining.

    Base: clip(adx_val / 50, 0, 1)
      ADX=25 → 0.50 (minimum viable trend)
      ADX=50 → 1.00 (very strong trend)

    Decline penalty: if ADX has dropped > 3pts in 3 bars, apply 0.4× reduction.
    This is a leading indicator — ADX rollover precedes price breakdown.
    The penalty is applied at the layer level (exit_monitor uses the slope for exit).
    """
    base_score = float(np.clip(adx_val / 50.0, 0.0, 1.0))
    if adx_change < ADX_SLOPE_PENALTY_THRESHOLD:
        base_score *= 0.6   # reduce but don't zero — may be a brief pause
    return round(base_score, 4)


def _score_price_momentum(roc_accel_val: float) -> float:
    """
    ROC acceleration → [0, 1] via logistic transform.

    roc_accel > 0: momentum accelerating → score > 0.5
    roc_accel = 0: neutral → score = 0.5
    roc_accel < 0: decelerating → score < 0.5

    Scale factor of 100: typical roc_accel values are in [-0.05, +0.05] range
    (percentage points). Multiplying by 100 puts them in [-5, +5] which gives
    a meaningful logistic spread.
    """
    z = float(np.clip(roc_accel_val * 100, -5.0, 5.0))
    return round(float(1.0 / (1.0 + math.exp(-z))), 4)


def _score_ma_stack(df: pd.DataFrame) -> float:
    """
    EMA stack integrity: E8 > E21 > E50.

    Full stack bullish → 1.0 (ideal momentum structure)
    E8 < E21 only → 0.0 (stack broken — primary warning)
    E8 > E21 but E21 < E50 → 0.5 (partial — trend weakening)
    """
    c   = df["close"]
    e8  = float(c.ewm(span=8,  adjust=False).mean().iloc[-1])
    e21 = float(c.ewm(span=21, adjust=False).mean().iloc[-1])
    e50 = float(c.ewm(span=50, adjust=False).mean().iloc[-1])

    if e8 > e21 > e50:
        return 1.0
    elif e8 < e21:
        return 0.0   # primary collapse signal
    else:
        return 0.5   # partial — E8 > E21 but E21 <= E50


def _score_volume_confirm(df: pd.DataFrame) -> float:
    """
    Recent 5d average volume vs 20d average.
    Above-average volume in uptrend = institutional participation continuing.

    score = clip((ratio - 0.5) / 1.5, 0, 1)
      ratio = recent_5d / avg_20d
      ratio = 0.5 (half the average) → score = 0
      ratio = 1.0 (at average)       → score = 0.33
      ratio = 2.0 (double average)   → score = 1.0

    We reward above-average volume and penalize very low volume, but
    don't penalize at the extremes — massive vol spikes can be capitulation,
    not accumulation. The clip at 2.0 prevents blow-off tops from looking healthy.
    """
    if len(df) < 22:
        return 0.5
    avg_20d    = float(df["volume"].iloc[-21:-1].mean())
    recent_5d  = float(df["volume"].iloc[-5:].mean())
    if avg_20d <= 0:
        return 0.5
    ratio = float(np.clip(recent_5d / avg_20d, 0.5, 2.0))
    return round(float(np.clip((ratio - 0.5) / 1.5, 0.0, 1.0)), 4)


def _score_time_remaining(days: int, hold_target: int) -> float:
    """
    Time remaining as fraction of hold_target.
    score = max(0, 1 - days/hold_target)
    Decays linearly to 0 at hold_target.
    """
    if hold_target <= 0:
        return 0.5
    return round(max(0.0, 1.0 - days / hold_target), 4)


def _classify_tier(health_score: float) -> str:
    if health_score >= TIER_STRENGTHENING:
        return "STRENGTHENING"
    elif health_score >= TIER_STABLE:
        return "STABLE"
    elif health_score >= TIER_EXHAUSTED:
        return "DECAYING"
    else:
        return "EXHAUSTED"


# ── Main scorer ───────────────────────────────────────────────────────────────

def score_engine_a_positions(
    positions: List[dict],
    bars_map:  Dict[str, pd.DataFrame],
) -> List[dict]:
    """
    Score all open Engine A positions on the 5-layer momentum health model.

    Args:
        positions — from ledger.get_positions("A")
        bars_map  — {symbol: DataFrame}

    Returns list of health dicts per position, sorted by health_score ascending
    (worst positions first for easy review).
    """
    health_records = []

    for pos in positions:
        symbol      = pos.get("symbol")
        entry_price = float(pos.get("entry_price", 0))
        meta        = pos.get("metadata", {})
        entry_date  = pos.get("entry_date", "")

        if not symbol or entry_price <= 0:
            continue

        df = bars_map.get(symbol)
        if df is None or len(df) < 22:
            health_records.append({
                "symbol":    symbol,
                "engine_id": ENGINE_ID,
                "tier":      "INSUFFICIENT_DATA",
                "score":     None,
                "layers":    {},
            })
            continue

        price       = float(df["close"].iloc[-1])
        atr         = _atr(df)
        days        = _days_held(entry_date)
        hold_target = int(meta.get("hold_target_days", 15))

        adx_val, adx_change = _adx_now_and_slope(df)
        roc_accel_val       = _roc_accel(df)

        # Layer scores
        l1 = _score_trend_cluster(adx_val, adx_change)
        l2 = _score_price_momentum(roc_accel_val)
        l3 = _score_ma_stack(df)
        l4 = _score_volume_confirm(df)
        l5 = _score_time_remaining(days, hold_target)

        layers = {
            "trend_cluster":  l1,
            "price_momentum": l2,
            "ma_stack":       l3,
            "volume_confirm": l4,
            "time_remaining": l5,
        }

        # Weighted sum ∈ [0, 1]
        raw = sum(LAYER_WEIGHTS[k] * v for k, v in layers.items())

        # ADX slope penalty — applied before centering
        # Separate from layer 1 to ensure it's visible in the score delta
        adx_penalty = 0.0
        if adx_change < ADX_SLOPE_PENALTY_THRESHOLD:
            adx_penalty = ADX_SLOPE_HEALTH_PENALTY
            raw = max(0.0, raw - adx_penalty)

        # Center to [-1, +1]
        health_score = round((raw - 0.5) * 2.0, 4)

        tier    = _classify_tier(health_score)
        pnl_pct = round(((price / entry_price) - 1) * 100, 2)

        record = {
            "symbol":       symbol,
            "engine_id":    ENGINE_ID,
            "tier":         tier,
            "score":        health_score,
            "pnl_pct":      pnl_pct,
            "days_held":    days,
            "hold_target":  hold_target,
            "price":        round(price, 4),
            "atr":          round(atr, 4),
            "adx":          round(adx_val, 2),
            "adx_change":   round(adx_change, 2),
            "adx_penalty":  round(adx_penalty, 3),
            "roc_accel":    round(roc_accel_val, 6),
            "layers":       layers,
            "scored_at":    datetime.now().isoformat(),
        }
        health_records.append(record)

        logger.info(
            "HOLD A | %s | tier=%s | score=%+.3f | pnl=%.2f%% | days=%d/%d | "
            "trend=%.2f | mom=%.2f | stack=%.2f | vol=%.2f | time=%.2f | "
            "adx=%.1f (Δ%.1f)",
            symbol, tier, health_score, pnl_pct, days, hold_target,
            l1, l2, l3, l4, l5, adx_val, adx_change
        )

    # Worst health first — makes daily review easier
    health_records.sort(key=lambda r: r["score"] if r["score"] is not None else 1.0)
    return health_records
