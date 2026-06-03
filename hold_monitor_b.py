"""
hold_monitor_b.py — Engine B Hold Health Scoring
==================================================
MR-weighted 5-layer health score for Engine B positions.

Engine B hold logic is OU-specific — the question is not "is this still
a good stock" but "is the mean reversion thesis still intact?"

The five layers and their weights:

  Layer 1: AVWAP proximity (30%)
    How close is price to AVWAP (the target)?
    Progress = (price - entry_price) / (avwap - entry_price)
    score = clip(progress, 0, 1.5) / 1.5
    >1.0 progress = overshot AVWAP (exit signal handled by exit_monitor_b)

  Layer 2: OU reversion speed (25%)
    Is the reversion occurring on schedule?
    Expected price at this day count = entry + (avwap - entry) × (1 - exp(-theta × days))
    score = clip(actual_progress / expected_progress, 0, 1.5) / 1.5
    <0.5 = lagging badly → degrading health

  Layer 3: Volume profile (20%)
    Is volume drying up (normal for MR) or re-escalating (bad)?
    Compare last 3d avg volume to entry-day volume.
    Drying up = healthy. Re-escalating = thesis stress.
    score = clip(1.0 - (recent_avg / entry_vol), -1, 1) normalized to [0,1]

  Layer 4: Stochastic exit zone (15%)
    Is %K approaching overbought (60-80 = reversion zone)?
    Score rises as %K climbs from oversold toward 60-80.
    score = clip((stoch_k - 20) / 60, 0, 1)

  Layer 5: Time decay (10%)
    Days remaining as fraction of hold_target.
    score = max(0, 1 - days/hold_target)
    Decays to 0 at hold_target regardless of price.

Total health score ∈ [-1, +1] via weighted sum normalized to [-1, 1].
Tiers: STRENGTHENING (> 0.20) / STABLE (0 to 0.20) / DECAYING (< 0) / EXHAUSTED (< -0.30)

Usage:
    from hold_monitor_b import score_engine_b_positions
    scores = score_engine_b_positions(positions, bars_map)

Not run standalone — called from hold_monitor.py engine dispatch.
"""

import math
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("ares.hold_b")

ENGINE_ID = "B"

LAYER_WEIGHTS = {
    "avwap_proximity":    0.30,
    "ou_reversion_speed": 0.25,
    "volume_profile":     0.20,
    "stoch_exit_zone":    0.15,
    "time_decay":         0.10,
}

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


def _compute_avwap(df: pd.DataFrame, lookback: int = 20) -> float:
    window = df.tail(lookback)
    if window.empty or window["volume"].sum() == 0:
        return float(df["close"].iloc[-1])
    if "vwap" in window.columns and window["vwap"].notna().all():
        vwap_col = window["vwap"]
    else:
        vwap_col = (window["high"] + window["low"] + window["close"]) / 3.0
    return float((vwap_col * window["volume"]).sum() / window["volume"].sum())


def _stoch_k(df: pd.DataFrame, k_period: int = 14) -> float:
    if len(df) < k_period:
        return 50.0
    low_k  = df["low"].rolling(k_period).min()
    high_k = df["high"].rolling(k_period).max()
    denom  = (high_k - low_k).replace(0, np.nan)
    k      = ((df["close"] - low_k) / denom * 100).fillna(50)
    return float(k.iloc[-1])


def _days_held(entry_date_str: str) -> int:
    try:
        entry = datetime.strptime(str(entry_date_str)[:10], "%Y-%m-%d").date()
        return (datetime.now().date() - entry).days
    except Exception:
        return 0


# ── Layer scorers ─────────────────────────────────────────────────────────────

def _score_avwap_proximity(price: float, entry_price: float, avwap: float) -> float:
    """
    Progress toward AVWAP target.
    progress = (price - entry) / (avwap - entry)
    score = clip(progress, 0, 1.5) / 1.5  → [0, 1]

    0 = no progress, 1 = at or beyond AVWAP.
    Negative progress (price moved further away) → 0.
    """
    distance_to_target = avwap - entry_price
    if abs(distance_to_target) < 1e-6:
        return 0.5  # already at AVWAP — neutral
    progress = (price - entry_price) / distance_to_target
    return round(min(1.0, max(0.0, progress / 1.5)), 4)


def _score_ou_reversion_speed(price: float, entry_price: float, avwap: float,
                                theta: float, days: int) -> float:
    """
    Compares actual reversion progress to OU model expectation.
    Expected fraction reverted at day t: 1 - exp(-theta × t)
    score = clip(actual_fraction / expected_fraction, 0, 1.5) / 1.5

    0 = lagging model badly, 1 = on or ahead of model schedule.
    Falls back to neutral (0.5) when theta is unknown.
    """
    if theta <= 0 or days <= 0:
        return 0.5

    target_distance = avwap - entry_price
    if abs(target_distance) < 1e-6:
        return 0.5

    actual_fraction   = (price - entry_price) / target_distance
    expected_fraction = 1.0 - math.exp(-theta * days)

    if expected_fraction <= 0:
        return 0.5

    speed_ratio = actual_fraction / expected_fraction
    return round(min(1.0, max(0.0, speed_ratio / 1.5)), 4)


def _score_volume_profile(df: pd.DataFrame, entry_date_str: str) -> float:
    """
    Volume drying up after capitulation = healthy MR.
    Recent 3d avg vs entry-day volume.

    score = clip(1.0 - (recent_avg / entry_vol), -0.5, 1.0) → normalized to [0,1]
    recent_avg < entry_vol → drying up → high score.
    recent_avg > entry_vol × 1.5 → escalating → low score (potential thesis stress).
    """
    if len(df) < 5:
        return 0.5

    # Entry day volume — find the bar closest to entry date
    try:
        entry_dt = datetime.strptime(str(entry_date_str)[:10], "%Y-%m-%d")
        if "timestamp" in df.columns:
            df_ts = df.copy()
            df_ts["ts_date"] = pd.to_datetime(df_ts["timestamp"]).dt.date
            row = df_ts[df_ts["ts_date"] == entry_dt.date()]
            entry_vol = float(row["volume"].iloc[0]) if not row.empty else float(df["volume"].iloc[-10])
        else:
            entry_vol = float(df["volume"].iloc[-10])
    except Exception:
        entry_vol = float(df["volume"].iloc[-10])

    recent_avg = float(df["volume"].iloc[-3:].mean())

    if entry_vol <= 0:
        return 0.5

    ratio = recent_avg / entry_vol
    # ratio < 1 = drying up (good), ratio > 1.5 = escalating (bad)
    raw_score = 1.0 - ratio
    return round(min(1.0, max(0.0, (raw_score + 0.5) / 1.5)), 4)


def _score_stoch_exit_zone(stoch_k_val: float) -> float:
    """
    %K moving toward reversion zone (60–80) = good progress.
    score = clip((stoch_k - 20) / 60, 0, 1)

    %K = 20 (where we entered) → score = 0
    %K = 80 → score = 1 (reversion zone reached, near exit)
    """
    return round(min(1.0, max(0.0, (stoch_k_val - 20.0) / 60.0)), 4)


def _score_time_decay(days: int, hold_target: int) -> float:
    """
    Time remaining as fraction of hold target.
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

def score_engine_b_positions(
    positions: List[dict],
    bars_map:  Dict[str, pd.DataFrame],
) -> List[dict]:
    """
    Score all open Engine B positions on the 5-layer MR health model.

    Args:
        positions — from ledger.get_positions("B")
        bars_map  — {symbol: DataFrame}

    Returns list of health dicts per position.
    """
    health_records = []

    for pos in positions:
        symbol      = pos.get("symbol")
        entry_price = float(pos.get("entry_price", 0))
        shares      = pos.get("shares", 0)
        meta        = pos.get("metadata", {})
        entry_date  = pos.get("entry_date", "")

        if not symbol or entry_price <= 0:
            continue

        df = bars_map.get(symbol)
        if df is None or len(df) < 10:
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
        avwap       = _compute_avwap(df)
        stoch_k_val = _stoch_k(df)
        days        = _days_held(entry_date)
        theta       = float(meta.get("b_ou_theta", 0) or meta.get("signals_at_entry", {}).get("b_ou_theta", 0))
        hold_target = int(meta.get("hold_target_days", 10))

        # Layer scores
        l1 = _score_avwap_proximity(price, entry_price, avwap)
        l2 = _score_ou_reversion_speed(price, entry_price, avwap, theta, days)
        l3 = _score_volume_profile(df, entry_date)
        l4 = _score_stoch_exit_zone(stoch_k_val)
        l5 = _score_time_decay(days, hold_target)

        layers = {
            "avwap_proximity":    l1,
            "ou_reversion_speed": l2,
            "volume_profile":     l3,
            "stoch_exit_zone":    l4,
            "time_decay":         l5,
        }

        # Weighted sum, normalized to [-1, +1]
        raw = sum(LAYER_WEIGHTS[k] * v for k, v in layers.items())
        # raw ∈ [0, 1] since all layers ∈ [0, 1]; center to [-1, +1]
        health_score = round((raw - 0.5) * 2.0, 4)

        tier = _classify_tier(health_score)
        pnl_pct = round(((price / entry_price) - 1) * 100, 2)

        record = {
            "symbol":        symbol,
            "engine_id":     ENGINE_ID,
            "tier":          tier,
            "score":         health_score,
            "pnl_pct":       pnl_pct,
            "days_held":     days,
            "hold_target":   hold_target,
            "price":         round(price, 4),
            "avwap":         round(avwap, 4),
            "atr":           round(atr, 4),
            "stoch_k":       round(stoch_k_val, 2),
            "layers":        layers,
            "scored_at":     datetime.now().isoformat(),
        }
        health_records.append(record)

        logger.info(
            "HOLD B | %s | tier=%s | score=%+.3f | pnl=%.2f%% | days=%d/%d | "
            "avwap_prox=%.2f | ou_speed=%.2f | vol=%.2f | stoch=%.2f | time=%.2f",
            symbol, tier, health_score, pnl_pct, days, hold_target,
            l1, l2, l3, l4, l5
        )

    return health_records
