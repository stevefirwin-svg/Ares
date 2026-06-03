"""
hold_monitor_f.py — Engine F Hold Health Scoring
==================================================
Breakout-weighted 5-layer health score for Engine F positions.

Engine F hold logic is structure-based — the question is:
"Is the breakout still valid and making progress toward its measured target?"

The five layers and their weights (bootstrap — TODO:DERIVE from IC):

  Layer 1: MEASURED MOVE PROGRESS (35%)
    How far has price traveled toward the measured target?
    progress = (price - entry_price) / (measured_target - entry_price)
    score = clip(progress, 0, 1.5) / 1.5  → [0, 1]
    >1.0 progress = overshot target (trim should have fired by now).

  Layer 2: BREAKOUT LEVEL HOLD (25%)
    Is price still above the breakout level (which should now be support)?
    (price - breakout_level) / ATR
    score = logistic((price - breakout_level) / ATR × 2) → [0, 1]
    Price at breakout level → 0.5; well above → >0.5; below → <0.5.

  Layer 3: VOLUME CONTINUATION (20%)
    Is volume expanding as the breakout continues?
    recent_3d avg vs avg_20d at entry.
    Expanding volume = institutional participation continuing.
    score = clip((recent_3d / avg_20d - 0.8) / 1.2, 0, 1)

  Layer 4: PRICE MOMENTUM (12%)
    ROC_5 > 0 means breakout is still accelerating.
    score = logistic(roc_5 × 50) → [0, 1]
    Positive ROC → score > 0.5; flat/negative → < 0.5.

  Layer 5: TIME REMAINING (8%)
    Days remaining as fraction of hold_target.
    score = max(0, 1 - days/hold_target)
    Lower weight than other engines — F's exit triggers are structural,
    not time-based (time_stop is a backstop, not the primary exit).

Total health score ∈ [-1, +1] via weighted sum normalized to [-1, 1].
Tiers: STRENGTHENING (> 0.20) / STABLE (0 to 0.20) / DECAYING (< 0) / EXHAUSTED (< -0.30)

Usage:
    from hold_monitor_f import score_engine_f_positions
    scores = score_engine_f_positions(positions, bars_map)

Not run standalone — called from ares_hold_monitor.py engine dispatch.
"""

import math
import logging
from datetime import datetime
from typing import Dict, List

import numpy as np
import pandas as pd

logger = logging.getLogger("ares.hold_f")

ENGINE_ID = "F"

LAYER_WEIGHTS = {
    "measured_progress":   0.35,
    "breakout_level_hold": 0.25,
    "volume_continuation": 0.20,
    "price_momentum":      0.12,
    "time_remaining":      0.08,
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


def _days_held(entry_date_str: str) -> int:
    try:
        entry = datetime.strptime(str(entry_date_str)[:10], "%Y-%m-%d").date()
        return (datetime.now().date() - entry).days
    except Exception:
        return 0


# ── Layer scorers ─────────────────────────────────────────────────────────────

def _score_measured_progress(price: float, entry_price: float,
                               measured_target: float) -> float:
    """
    Progress toward the Edwards & Magee measured move target.
    progress = (price - entry) / (target - entry)
    score = clip(progress, 0, 1.5) / 1.5  → [0, 1]

    0 = no progress (price hasn't moved from entry)
    1 = at or beyond measured target
    Negative progress (price below entry) → 0

    Note: score = 1.0 when price reaches or exceeds target.
    Exit F-3 (measured move trim) should fire before health hits 1.0,
    but health score still reflects thesis completion.
    """
    target_distance = measured_target - entry_price
    if abs(target_distance) < 1e-6:
        return 0.5
    progress = (price - entry_price) / target_distance
    return round(min(1.0, max(0.0, progress / 1.5)), 4)


def _score_breakout_level_hold(price: float, breakout_level: float, atr: float) -> float:
    """
    How far is price above the breakout level (which should now be support)?
    (price - breakout_level) / ATR → logistic → [0, 1]

    price at breakout_level → normalized z = 0 → score = 0.5
    price 1 ATR above      → z = 2 → score ≈ 0.88
    price at or below      → z < 0 → score < 0.5 (stress signal)

    Scale factor of 2: typical meaningful deviations are 0.5–1.5 ATR.
    """
    if atr <= 0:
        return 0.5
    z = float(np.clip((price - breakout_level) / atr * 2, -4, 4))
    return round(float(1.0 / (1.0 + math.exp(-z))), 4)


def _score_volume_continuation(df: pd.DataFrame) -> float:
    """
    Recent 3d average volume vs 20d average.
    Expanding post-breakout volume = institutional participation continuing.

    score = clip((recent_3d / avg_20d - 0.8) / 1.2, 0, 1)
      ratio = 0.8 (slightly below avg) → score = 0.0
      ratio = 1.0 (at average)         → score = 0.17
      ratio = 2.0 (double)             → score = 1.0

    Note: Engine F rewards volume continuation more than Engine A because
    the breakout itself was volume-confirmed — continuation of that conviction
    is a strong health signal.
    """
    if len(df) < 22:
        return 0.5
    avg_20d   = float(df["volume"].iloc[-21:-1].mean())
    recent_3d = float(df["volume"].iloc[-3:].mean())
    if avg_20d <= 0:
        return 0.5
    ratio = float(np.clip(recent_3d / avg_20d, 0.5, 3.0))
    return round(float(np.clip((ratio - 0.8) / 1.2, 0.0, 1.0)), 4)


def _score_price_momentum(df: pd.DataFrame) -> float:
    """
    ROC_5 = (close[-1] / close[-6]) - 1 → logistic(roc_5 × 50) → [0, 1]

    roc_5 > 0: price accelerating → score > 0.5
    roc_5 = 0: flat              → score = 0.5
    roc_5 < 0: price slowing    → score < 0.5

    Scale factor of 50: typical 5d ROC values for breakout follow-through
    are in [-0.05, +0.05]. ×50 puts them in [-2.5, +2.5] logistic range.
    """
    c = df["close"]
    if len(c) < 6:
        return 0.5
    roc5 = float(c.iloc[-1] / c.iloc[-6]) - 1.0
    z = float(np.clip(roc5 * 50, -4, 4))
    return round(float(1.0 / (1.0 + math.exp(-z))), 4)


def _score_time_remaining(days: int, hold_target: int) -> float:
    """Days remaining as fraction of hold_target. Linear decay to 0."""
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

def score_engine_f_positions(
    positions: List[dict],
    bars_map:  Dict[str, pd.DataFrame],
) -> List[dict]:
    """
    Score all open Engine F positions on the 5-layer breakout health model.

    Args:
        positions — from ledger.get_positions("F")
        bars_map  — {symbol: DataFrame}

    Returns list of health dicts per position, sorted by health_score ascending.
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
        days        = _days_held(entry_date)
        hold_target = int(meta.get("hold_target_days", 15))

        # Breakout geometry from entry metadata
        measured_target = meta.get("measured_target")
        breakout_level  = meta.get("breakout_level")

        # Fallbacks if metadata missing (should not happen after correct entry recording)
        if measured_target is None:
            measured_target = entry_price * 1.10  # rough 10% proxy
            logger.warning("HOLD F: %s missing measured_target — using proxy %.2f",
                           symbol, measured_target)
        if breakout_level is None:
            breakout_level = entry_price
            logger.warning("HOLD F: %s missing breakout_level — using entry_price", symbol)

        measured_target = float(measured_target)
        breakout_level  = float(breakout_level)

        # Layer scores
        l1 = _score_measured_progress(price, entry_price, measured_target)
        l2 = _score_breakout_level_hold(price, breakout_level, atr)
        l3 = _score_volume_continuation(df)
        l4 = _score_price_momentum(df)
        l5 = _score_time_remaining(days, hold_target)

        layers = {
            "measured_progress":   l1,
            "breakout_level_hold": l2,
            "volume_continuation": l3,
            "price_momentum":      l4,
            "time_remaining":      l5,
        }

        # Weighted sum ∈ [0, 1] → center to [-1, +1]
        raw          = sum(LAYER_WEIGHTS[k] * v for k, v in layers.items())
        health_score = round((raw - 0.5) * 2.0, 4)

        tier    = _classify_tier(health_score)
        pnl_pct = round(((price / entry_price) - 1) * 100, 2)

        # Progress toward measured target (for display)
        progress_pct = round(
            (price - entry_price) / max(measured_target - entry_price, 1e-6) * 100, 1
        )

        record = {
            "symbol":           symbol,
            "engine_id":        ENGINE_ID,
            "tier":             tier,
            "score":            health_score,
            "pnl_pct":          pnl_pct,
            "days_held":        days,
            "hold_target":      hold_target,
            "price":            round(price, 4),
            "atr":              round(atr, 4),
            "breakout_level":   round(breakout_level, 4),
            "measured_target":  round(measured_target, 4),
            "progress_pct":     progress_pct,
            "trimmed":          bool(meta.get("measured_move_trimmed", False)),
            "layers":           layers,
            "scored_at":        datetime.now().isoformat(),
        }
        health_records.append(record)

        logger.info(
            "HOLD F | %s | tier=%s | score=%+.3f | pnl=%.2f%% | "
            "progress=%.1f%% toward target | days=%d/%d | "
            "progress=%.2f | bl_hold=%.2f | vol=%.2f | mom=%.2f | time=%.2f",
            symbol, tier, health_score, pnl_pct, progress_pct,
            days, hold_target, l1, l2, l3, l4, l5
        )

    health_records.sort(key=lambda r: r["score"] if r["score"] is not None else 1.0)
    return health_records
