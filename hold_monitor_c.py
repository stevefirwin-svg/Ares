"""
hold_monitor_c.py — Engine C Hold Health Scoring
==================================================
Squeeze position health. Very different from other engines —
Engine C positions are short-lived (3-8 bars) so health degrades fast.

Layers:

  Layer 1: Momentum strength (40%)
    TTM momentum value — is it positive and growing?
    score = clip((mom_now + abs(mom_threshold)) / scale, 0, 1)
    More positive = healthier. Turning negative = emergency.

  Layer 2: Price direction (30%)
    Is price above entry? Above entry + 0.5×ATR = good.
    score = clip((price - entry) / ATR, 0, 1.5) / 1.5

  Layer 3: Time urgency (30%)
    Squeeze edge expires fast. Aggressive decay.
    score = max(0, 1 - days/max_hold_days)
    At 4 of 8 days = 0.50 score. At 7 of 8 = 0.125.

Note: No volume layer (squeeze fires on volatility expansion, not volume surge).
Note: No MA layer (hold is too short for MAs to be meaningful).
"""

import logging
import math
from datetime import datetime
from typing import Dict, List

import numpy as np
import pandas as pd

logger = logging.getLogger("ares.hold_c")
ENGINE_ID = "C"

LAYER_WEIGHTS = {
    "momentum_strength": 0.40,
    "price_direction":   0.30,
    "time_urgency":      0.30,
}

TIER_STRENGTHENING = 0.20
TIER_STABLE        = 0.0
TIER_EXHAUSTED     = -0.15  # tightest threshold — stalled squeezes die fast


def _atr(df, period=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h-l, (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if not math.isnan(val) else 0.0


def _ttm_momentum(df, period=20):
    if len(df) < period + 2:
        return 0.0
    bb_mid  = df["close"].rolling(period).mean()
    highest = df["high"].rolling(period).max()
    lowest  = df["low"].rolling(period).min()
    mid_hl  = (highest + lowest) / 2
    mom     = df["close"] - (mid_hl + bb_mid) / 2
    return float(mom.iloc[-1])


def _days_held(entry_date_str):
    try:
        entry = datetime.strptime(str(entry_date_str)[:10], "%Y-%m-%d").date()
        return (datetime.now().date() - entry).days
    except Exception:
        return 0


def _classify_tier(score):
    if score >= TIER_STRENGTHENING:
        return "STRENGTHENING"
    elif score >= TIER_STABLE:
        return "STABLE"
    elif score >= TIER_EXHAUSTED:
        return "DECAYING"
    else:
        return "EXHAUSTED"


def score_engine_c_positions(positions, bars_map):
    health_records = []

    for pos in positions:
        symbol      = pos.get("symbol")
        entry_price = float(pos.get("entry_price", 0))
        meta        = pos.get("metadata", {})
        entry_date  = pos.get("entry_date", "")

        if not symbol or entry_price <= 0:
            continue

        df = bars_map.get(symbol)
        if df is None or len(df) < 5:
            health_records.append({
                "symbol": symbol, "engine_id": ENGINE_ID,
                "tier": "INSUFFICIENT_DATA", "score": None, "layers": {},
            })
            continue

        price       = float(df["close"].iloc[-1])
        atr         = _atr(df)
        days        = _days_held(entry_date)
        max_hold    = int(meta.get("max_hold_days", 8))
        mom_now     = _ttm_momentum(df)
        pnl_pct     = round(((price / entry_price) - 1) * 100, 2)

        # Momentum strength score
        # mom_now range is unbounded; normalize by ATR proxy
        mom_scale = atr * 0.5 if atr > 0 else 1.0
        l1 = min(1.0, max(0.0, (mom_now + mom_scale) / (2 * mom_scale)))

        # Price direction
        l2 = min(1.0, max(0.0, (price - entry_price) / (1.5 * atr))) if atr > 0 else 0.5

        # Time urgency (aggressive decay)
        l3 = max(0.0, 1.0 - days / max_hold) if max_hold > 0 else 0.0

        layers = {
            "momentum_strength": round(l1, 4),
            "price_direction":   round(l2, 4),
            "time_urgency":      round(l3, 4),
        }

        raw          = sum(LAYER_WEIGHTS[k] * v for k, v in layers.items())
        health_score = round((raw - 0.5) * 2.0, 4)
        tier         = _classify_tier(health_score)

        record = {
            "symbol":    symbol,
            "engine_id": ENGINE_ID,
            "tier":      tier,
            "score":     health_score,
            "pnl_pct":   pnl_pct,
            "days_held": days,
            "max_hold":  max_hold,
            "price":     round(price, 4),
            "mom_now":   round(mom_now, 6),
            "layers":    layers,
            "scored_at": datetime.now().isoformat(),
        }
        health_records.append(record)

        logger.info("HOLD C | %s | tier=%s | score=%+.3f | pnl=%.2f%% | days=%d/%d | mom=%+.4f",
                    symbol, tier, health_score, pnl_pct, days, max_hold, mom_now)

    return health_records
