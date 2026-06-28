"""
engine_f.py — Ares Engine F: Structural Breakout
==================================================
Edge: Price consolidation is temporary equilibrium. Volume-confirmed breakout
      above the consolidation represents resolution of that equilibrium and the
      beginning of a directional move. Entry is at the origin, not mid-trend.
      Edwards & Magee (1948), Darvas box, IBD methodology.

Classification gate (ALL must pass):
  1. Symbol not owned by another engine (ownership check FIRST)
  2. Macro regime RISK_ON or NEUTRAL (F gates out in RISK_OFF/CRISIS)
  3. Consolidation detected: min 15 bars, range < 1.5 ATR/bar, volume declining
  4. Breakout bar: close > max(high[-N+1:]) — closes above entire consolidation
  5. Volume: today > 1.8 × 20d avg (institutional conviction — no exception)
  6. Close position > 0.75 (close in top quartile of bar range)
  7. No overhead HVN within 2.0 ATR above breakout price
  8. Engine score >= CLASSIFICATION_THRESHOLDS["F"]["engine_score_min"] (0.60)

Stop: STRUCTURAL — stop_price = consol_low - (0.10 × ATR)
  Engine F is the ONLY engine that does NOT use an ATR multiple for the stop.
  Base violated = thesis gone. The stop is derived from the consolidation
  structure itself, not from volatility. Rule from ARES_SKILL.md — immutable.

Sizing: risk-budget approach (not Kelly fraction of equity)
  position_$ = min(
      engine_risk_budget / (entry_price - stop_price),   # shares from risk
      engine_capital_remaining,
      buying_power × 0.95
  )
  engine_risk_budget = equity × KELLY_BOOTSTRAP_FRACTION × KELLY_SHADOW_MULTIPLIER × kelly_scale

Measured move (stored in entry_metadata, informational only):
  breakout_level = max(high[-N+1:])
  base_height    = breakout_level - consol_low
  measured_target = breakout_level + base_height
  Used as a trim trigger in exit_monitor_f.py — NOT a mechanical full exit.

Signals (all prefixed f_):
  f_consol_quality: clip(3.0 - range_atr, 0, 3.0)
    Convention: higher = tighter consolidation = more energy coiled.
    Range: [0, 3.0] | Unit: ATR compression score

  f_breakout_strength: close_position × log1p(vol_ratio - 1)
    Convention: higher = stronger breakout bar (close high + volume premium).
    Range: [0, inf] | Multiplicative of close quality and volume quality.

  f_overhead_clearance: nearest_hvn_above / ATR
    Convention: higher = more room above price = breakout can run further.
    Range: [0, inf] | Unit: ATR multiples above breakout

  f_vol_declining: 1.0 if volume declined during consolidation else 0.0
    Convention: 1.0 = energy coiled (declining volume = supply drying up).

  f_rel_strength_spy: (stock_20d_return / spy_20d_return) - 1
    Convention: positive = stock outperforming SPY = sector tailwind.

Shadow mode: ENGINE_STATUS["F"] == "shadow" → classify but do not trade.
All classifications written to shadow_classifications.json.
Gate: 30 shadow classifications before live capital.

Run:
    python engine_f.py --scan        # Run full scan
    python engine_f.py --scan --dry  # Dry run (no orders, no shadow write)
    python engine_f.py --status      # Show current F positions
"""

import os
import json
import logging
import argparse
import math
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
load_dotenv()

from ares_config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    ENGINE_STATUS, ENGINE_REGIME_GATE,
    INITIAL_ENGINE_WEIGHTS, TOTAL_EQUITY,
    KELLY_BOOTSTRAP_FRACTION, KELLY_SHADOW_MULTIPLIER,
    KELLY_ENGINE_CLIPS, ENGINE_STOP_PARAMS,
    CLASSIFICATION_THRESHOLDS, BARS_LOOKBACK,
    SHADOW_MIN_ENTRIES_FOR_LIVE, MAX_POSITIONS_PER_ENGINE,
)
from ledger import Ledger
from margin_guard import check_margin_safety

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [EngineF] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"logs/engine_f_{datetime.now():%Y%m%d}.log"),
    ],
)
logger = logging.getLogger("ares.engine_f")

ENGINE_ID     = "F"
SHADOW_FILE   = "shadow_classifications.json"
MACRO_FILE    = "macro_context.json"
UNIVERSE_FILE = "universe.json"

# Consolidation detection parameters
CONSOL_LOOKBACK_MIN  = 15   # minimum bars for a valid consolidation
CONSOL_LOOKBACK_PREF = 20   # preferred lookback
CONSOL_RANGE_ATR_MAX = 1.5  # max ATR-normalized range *per bar* for "tight" consolidation
                             # range_atr = (max_high - min_low) / ATR / lookback_bars
                             # 1.5 = stock moving ~1.5x its daily ATR per bar on average = trending
                             # TODO:DERIVE — calibrate from shadow data once 30 F-trades exist
                             # TODO:DERIVE from shadow data — 1.5 is theoretical


# ── Data helpers ──────────────────────────────────────────────────────────────

def _load_json(path: str, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _save_json_atomic(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


from bars_fetch import fetch_bars as _bars_fetch_shared

def fetch_bars(symbols: List[str], lookback: int = BARS_LOOKBACK) -> Dict[str, pd.DataFrame]:
    """
    Fetch daily OHLCV bars via yfinance.

    Thin wrapper around the shared implementation in bars_fetch.py
    (consolidated 2026-06-28 — see that module's docstring for why).
    """
    return _bars_fetch_shared(symbols, lookback=lookback, min_bars=CONSOL_LOOKBACK_PREF + 5)


def get_account() -> dict:
    import requests as req
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    resp = req.get(f"{ALPACA_BASE_URL}/v2/account", headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_positions() -> List[dict]:
    import requests as req
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    resp = req.get(f"{ALPACA_BASE_URL}/v2/positions", headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def submit_order(symbol: str, qty: int, client_order_id: str,
                 limit_price: float, dry_run: bool = False) -> Optional[dict]:
    if dry_run:
        logger.info("[DRY] Would buy %d shares of %s @ %.2f", qty, symbol, limit_price)
        return {"id": "dry-run", "symbol": symbol, "qty": qty}

    import requests as req
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type":        "application/json",
    }
    body = {
        "symbol":          symbol,
        "qty":             str(qty),
        "side":            "buy",
        "type":            "market",
        "time_in_force":   "day",
        "client_order_id": client_order_id,
    }
    try:
        resp = req.post(f"{ALPACA_BASE_URL}/v2/orders", headers=headers,
                        json=body, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Order submit failed for %s: %s", symbol, e)
        return None


# ── ATR ───────────────────────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, period: int = 14) -> float:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs()
    ], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if not math.isnan(val) else 0.0


# ── Volume profile / HVN ──────────────────────────────────────────────────────

def _compute_volume_profile(df: pd.DataFrame, lookback: int = 20,
                             bins: int = 20) -> dict:
    """
    Simplified volume profile over recent `lookback` bars.
    Divides the price range into `bins` buckets, sums volume in each.
    Returns the nearest High Volume Node (HVN) above current price.

    HVN = top 20% of buckets by volume (above-average volume concentration).
    If no HVN is found above current price, returns inf (no resistance).

    Research: Volume Profile / Market Profile (Steidlmayer, 1984).
    HVN above a breakout = wall of supply = lower breakout success rate.
    """
    window = df.tail(lookback)
    if len(window) < 5:
        return {"nearest_hvn_above": float("inf")}

    price_min = float(window["low"].min())
    price_max = float(window["high"].max())
    current   = float(df["close"].iloc[-1])

    if price_max <= price_min:
        return {"nearest_hvn_above": float("inf")}

    bin_size  = (price_max - price_min) / bins
    bin_vols  = np.zeros(bins)

    for _, row in window.iterrows():
        bar_vol = float(row["volume"])
        bar_lo  = float(row["low"])
        bar_hi  = float(row["high"])
        # Distribute bar volume uniformly across the bins it spans
        lo_bin = int((bar_lo - price_min) / bin_size)
        hi_bin = int((bar_hi - price_min) / bin_size)
        lo_bin = max(0, min(lo_bin, bins - 1))
        hi_bin = max(0, min(hi_bin, bins - 1))
        span = max(1, hi_bin - lo_bin + 1)
        for b in range(lo_bin, hi_bin + 1):
            bin_vols[b] += bar_vol / span

    # HVN = bins above the 80th percentile of volume
    hvn_threshold = np.percentile(bin_vols, 80)
    hvn_prices = []
    for i, vol in enumerate(bin_vols):
        if vol >= hvn_threshold:
            bin_price = price_min + (i + 0.5) * bin_size
            if bin_price > current:
                hvn_prices.append(bin_price)

    nearest_hvn_above = min(hvn_prices) if hvn_prices else float("inf")
    return {"nearest_hvn_above": nearest_hvn_above}


# ── Signal computation ────────────────────────────────────────────────────────

def f_consol_quality(df: pd.DataFrame, atr: float,
                     lookback: int = CONSOL_LOOKBACK_PREF) -> Tuple[float, float, float]:
    """
    Consolidation quality score and derived geometry.

    Measures how tight the consolidation was over the lookback period
    (excluding today's breakout bar).

    range_atr = (max_high - min_low) / ATR over bars [-lookback:-1]
    consol_quality = clip(3.0 - range_atr, 0, 3.0)
      range_atr = 1.0 (very tight) → score = 2.0
      range_atr = 1.5 (gate threshold) → score = 1.5
      range_atr = 3.0 (loose) → score = 0.0

    Convention: higher = tighter consolidation = more energy coiled.
    Range: [0, 3.0]

    Returns (consol_quality, consol_range_atr, consol_low)
    """
    consol = df.iloc[-(lookback + 1):-1]  # exclude today's bar
    if len(consol) < CONSOL_LOOKBACK_MIN or atr <= 0:
        return float("nan"), float("nan"), float("nan")

    consol_high = float(consol["high"].max())
    consol_low  = float(consol["low"].min())
    # Per-bar normalization — consistent with is_valid_consolidation gate.
    # range_atr_per_bar: 0.0 = no movement (perfect base), 1.5 = gate boundary
    range_atr   = (consol_high - consol_low) / atr / len(consol)

    # quality = 3.0 - range_atr_per_bar, clipped to [0, 3]:
    # range_atr=0.0 → quality=3.0 (perfectly flat base)
    # range_atr=1.5 → quality=1.5 (at the gate — just passes)
    # range_atr≥3.0 → quality=0.0 (trending, should be gated out)
    quality = float(np.clip(3.0 - range_atr, 0.0, 3.0))
    return round(quality, 4), round(range_atr, 4), round(consol_low, 4)


def f_breakout_strength(df: pd.DataFrame, avg_vol_20d: float) -> Tuple[float, float, float]:
    """
    Breakout bar quality: close_position × log1p(vol_ratio - 1).

    close_position = (close - low) / (high - low)
      0 = closed at the low (weak close), 1 = closed at the high (strong close)
      Gate: > 0.75

    vol_ratio = today_vol / avg_vol_20d
      Gate: > 1.8 (institutional confirmation required, no exceptions)

    breakout_strength = close_position × log1p(vol_ratio - 1)
      Multiplicative: weak close OR weak volume → low score
      Both strong → exponential benefit (log dampens extremes)

    Convention: higher = stronger breakout bar.
    Range: [0, inf]

    Returns (breakout_strength, close_position, vol_ratio)
    """
    bar   = df.iloc[-1]
    h_bar = float(bar["high"])
    l_bar = float(bar["low"])
    c_bar = float(bar["close"])
    v_bar = float(bar["volume"])

    bar_range = h_bar - l_bar
    if bar_range < 1e-6 or avg_vol_20d <= 0:
        return 0.0, 0.5, 1.0

    close_pos  = float((c_bar - l_bar) / bar_range)
    vol_ratio  = float(v_bar / avg_vol_20d)
    strength   = float(close_pos * math.log1p(max(0, vol_ratio - 1)))

    return round(strength, 6), round(close_pos, 4), round(vol_ratio, 4)


def f_vol_declining(df: pd.DataFrame,
                    lookback: int = CONSOL_LOOKBACK_PREF) -> float:
    """
    1.0 if volume trended down during the consolidation period, else 0.0.

    Declining volume during consolidation = supply drying up = energy coiling.
    OLS slope on volume over bars [-lookback:-1] (exclude today's breakout bar).

    Convention: 1.0 = volume declined = stronger setup.
    """
    consol_vol = df["volume"].iloc[-(lookback + 1):-1]
    if len(consol_vol) < 5:
        return 0.0
    slope = float(np.polyfit(np.arange(len(consol_vol)), consol_vol.values, 1)[0])
    return 1.0 if slope < 0 else 0.0


def f_overhead_clearance(df: pd.DataFrame, atr: float,
                         lookback: int = CONSOL_LOOKBACK_PREF) -> float:
    """
    nearest_hvn_above / ATR — room above the breakout price.

    Higher = more room for the breakout to run before hitting resistance.
    Gate: must be >= CLASSIFICATION_THRESHOLDS["F"]["overhead_hvn_atr"] (2.0)

    Convention: higher = more overhead clearance = stronger breakout candidate.
    Range: [0, inf] | Unit: ATR multiples

    Returns inf if no HVN is found above (no resistance = best case).
    """
    if atr <= 0:
        return 0.0
    profile  = _compute_volume_profile(df, lookback=lookback)
    hvn_dist = profile.get("nearest_hvn_above", float("inf"))
    current  = float(df["close"].iloc[-1])
    if hvn_dist == float("inf"):
        return float("inf")
    return round((hvn_dist - current) / atr, 4)


def f_rel_strength_spy(df: pd.DataFrame,
                        spy_df: Optional[pd.DataFrame],
                        period: int = 20) -> float:
    """
    Stock 20d return relative to SPY 20d return.
    (stock_ret / spy_ret) - 1

    Convention: positive = outperforming SPY = sector tailwind.
    Range: [-inf, +inf]
    Falls back to 0.0 if SPY bars unavailable.
    """
    if spy_df is None or len(df) < period + 1 or len(spy_df) < period + 1:
        return 0.0
    stock_ret = float(df["close"].iloc[-1] / df["close"].iloc[-(period + 1)]) - 1.0
    spy_ret   = float(spy_df["close"].iloc[-1] / spy_df["close"].iloc[-(period + 1)]) - 1.0
    if abs(spy_ret) < 1e-8:
        return 0.0
    return round((stock_ret / (1 + spy_ret)) - 1.0, 6)


# ── Breakout level and measured move ─────────────────────────────────────────

def compute_breakout_geometry(df: pd.DataFrame,
                               lookback: int = CONSOL_LOOKBACK_PREF) -> dict:
    """
    Compute breakout_level, base_height, consol_low, and measured_target.

    breakout_level = max(high over bars [-lookback:-1])  — resistance just broken
    consol_low     = min(low over bars [-lookback:-1])   — base of consolidation
    base_height    = breakout_level - consol_low
    measured_target = breakout_level + base_height       — Edwards & Magee

    These are stored in entry_metadata and used by exit_monitor_f.py.
    The measured_target is informational — trim trigger, not mechanical full exit.
    """
    consol = df.iloc[-(lookback + 1):-1]
    if len(consol) < CONSOL_LOOKBACK_MIN:
        return {}

    breakout_level  = float(consol["high"].max())
    consol_low      = float(consol["low"].min())
    base_height     = breakout_level - consol_low
    measured_target = breakout_level + base_height

    return {
        "breakout_level":  round(breakout_level, 4),
        "consol_low":      round(consol_low, 4),
        "base_height":     round(base_height, 4),
        "measured_target": round(measured_target, 4),
        "consol_bars":     len(consol),
    }


# ── Consolidation gate ────────────────────────────────────────────────────────

def is_valid_consolidation(df: pd.DataFrame, atr: float,
                            lookback: int = CONSOL_LOOKBACK_PREF) -> bool:
    """
    Returns True if the recent lookback bars (excluding today) form a valid
    consolidation: range < 1.5 ATR/bar on average.

    range_atr = (max_high - min_low) / ATR
    Gate: range_atr < CONSOL_RANGE_ATR_MAX (1.5)

    A tight consolidation means price was moving less than 1.5 ATR total over
    the entire period — energy coiled, not trending.
    """
    consol = df.iloc[-(lookback + 1):-1]
    if len(consol) < CONSOL_LOOKBACK_MIN or atr <= 0:
        return False
    consol_range = float(consol["high"].max() - consol["low"].min())
    # Normalize by lookback: range_atr = total_range / ATR / n_bars
    # This measures average ATR-units of movement per bar during consolidation.
    # A trending stock moves ~1.0 ATR/bar; a consolidating stock moves <0.5 ATR/bar.
    # Gate of 1.5 filters out stocks that are still directionally moving.
    range_atr_per_bar = (consol_range / atr) / len(consol)
    return range_atr_per_bar < CONSOL_RANGE_ATR_MAX


def is_breakout_bar(df: pd.DataFrame, lookback: int = CONSOL_LOOKBACK_PREF) -> bool:
    """
    Returns True if today's bar closes above the entire consolidation high.
    close[-1] > max(high[-lookback-1:-1])
    """
    consol_high = float(df["high"].iloc[-(lookback + 1):-1].max())
    today_close = float(df["close"].iloc[-1])
    return today_close > consol_high


# ── Engine score ──────────────────────────────────────────────────────────────

def compute_engine_score(
    consol_quality:     float,
    breakout_strength:  float,
    overhead_clearance: float,
    vol_declining:      float,
    rel_strength_spy:   float,
) -> float:
    """
    Engine F composite score ∈ [0, 1].

    Components:
      1. f_consol_quality:     clip(quality/3.0, 0, 1)
      2. f_breakout_strength:  logistic of log1p(strength × 5)
      3. f_overhead_clearance: clip(clearance/5.0, 0, 1) — cap at 5 ATR
      4. f_vol_declining:      1.1× multiplier on final score if True
      5. f_rel_strength_spy:   logistic of (rs_spy × 20) → [0,1]

    RF-14: loads IC-calibrated weights for the 5 declared signals from
    sub_engine_params.json. Falls back to equal (0.25 each excluding vol_declining
    which acts as a multiplier).
    """
    from ares_weights import load_signal_weights

    # Component 1: consolidation quality [0, 1]
    c1 = float(np.clip(consol_quality / 3.0, 0.0, 1.0)) if not math.isnan(consol_quality) else 0.5

    # Component 2: breakout strength → logistic [0, 1]
    if math.isnan(breakout_strength):
        c2 = 0.5
    else:
        z2 = float(np.clip(math.log1p(breakout_strength * 5) * 2 - 1, -3, 3))
        c2 = float(1 / (1 + math.exp(-z2)))

    # Component 3: overhead clearance [0, 1] — cap at 5 ATR
    if overhead_clearance == float("inf"):
        c3 = 1.0
    elif math.isnan(overhead_clearance) or overhead_clearance <= 0:
        c3 = 0.0
    else:
        c3 = float(np.clip(overhead_clearance / 5.0, 0.0, 1.0))

    # Component 5: rel strength vs SPY → logistic [0, 1]
    if math.isnan(rel_strength_spy):
        c5 = 0.5
    else:
        z5 = float(np.clip(rel_strength_spy * 20, -3, 3))
        c5 = float(1 / (1 + math.exp(-z5)))

    # RF-14: IC-calibrated weights for continuous signals
    weights  = load_signal_weights("F")
    w1 = weights.get("f_consol_quality",     0.25)
    w2 = weights.get("f_breakout_strength",  0.25)
    w3 = weights.get("f_overhead_clearance", 0.25)
    # f_vol_declining is binary → acts as multiplier (not in weighted sum)
    w5 = weights.get("f_rel_strength_spy",   0.25)
    total_w = w1 + w2 + w3 + w5
    if total_w < 1e-10:
        total_w = 1.0
    raw = (w1 * c1 + w2 * c2 + w3 * c3 + w5 * c5) / total_w

    # Component 4: vol_declining multiplier (1.1× if vol dried up — conservative)
    if vol_declining == 1.0:
        raw = min(1.0, raw * 1.1)

    return round(float(np.clip(raw, 0.0, 1.0)), 4)


# ── Entry parameters and structural stop ─────────────────────────────────────

def compute_entry_params(df: pd.DataFrame, atr: float, engine_score: float,
                          consol_low: float) -> dict:
    """
    Engine F entry parameters. All derived.

    Stop: STRUCTURAL (not ATR-based — immutable rule).
      stop_price = consol_low - (stop_buffer_atr × ATR)
      stop_buffer_atr = 0.10 (from ENGINE_STOP_PARAMS["F"])
      Base violated = thesis gone. The buffer prevents whipsaw on the exact low.

    Sizing: risk-budget approach.
      shares = engine_risk_budget / (entry_price - stop_price)
      This is the ONLY correct way to size Engine F — the structural stop
      defines the dollar risk per share, so shares derive from the risk budget.

    Hold target: score-scaled in [10, 20] days.
      hold = clip(10 + engine_score × 10, 10, 20)
      Higher score = stronger setup = longer time to reach measured target.
      TODO:DERIVE from shadow data.

    Kelly scale: engine_score as conviction proxy ∈ [0.5, 1.0].
      Clipped at 0.5 floor — Engine F setups are binary; no sub-half Kelly.

    Trail scale: 1.1 (slightly wider than a neutral 1.0 — let breakout run).
    """
    from ares_config import ENGINE_STOP_PARAMS
    stop_buffer = ENGINE_STOP_PARAMS[ENGINE_ID].get("stop_buffer_atr", 0.10)
    stop_price  = round(consol_low - stop_buffer * atr, 4)

    hold_target = int(np.clip(10 + engine_score * 10, 10, 20))
    kelly_scale = round(float(np.clip(engine_score, 0.5, 1.0)), 4)
    trail_scale = 1.1

    return {
        "stop_price":       stop_price,
        "stop_buffer_atr":  stop_buffer,
        "hold_target_days": hold_target,
        "kelly_scale":      kelly_scale,
        "trail_scale":      trail_scale,
    }


# ── Position sizing ───────────────────────────────────────────────────────────

def compute_position_size(equity: float, kelly_scale: float,
                           stop_price: float, entry_price: float,
                           engine_capital_remaining: float,
                           buying_power: float) -> Tuple[float, int]:
    """
    Engine F position sizing — risk-budget method.

    Engine F stop is structural, not a fixed ATR multiple. So the correct
    approach is to define a dollar risk budget and derive shares from it:

      risk_budget_$ = equity × KELLY_BOOTSTRAP_FRACTION × KELLY_SHADOW_MULTIPLIER × kelly_scale
      risk_per_share = entry_price - stop_price
      shares = risk_budget_$ / risk_per_share

    position_$ = shares × entry_price, clipped by:
      - engine_capital_remaining
      - buying_power × 0.95
      - KELLY_ENGINE_CLIPS["F"] floor/ceiling

    Returns (position_dollar, shares).
    """
    from ares_config import KELLY_APPLY_FLOOR_IN_BOOTSTRAP
    from ares_weights import is_calibrated
    min_pct, max_pct = KELLY_ENGINE_CLIPS[ENGINE_ID]

    risk_budget = equity * KELLY_BOOTSTRAP_FRACTION * KELLY_SHADOW_MULTIPLIER * kelly_scale
    # RF-13: only apply min_pct floor post-calibration
    if KELLY_APPLY_FLOOR_IN_BOOTSTRAP or is_calibrated(ENGINE_ID):
        risk_budget = max(equity * min_pct, risk_budget)
    risk_budget = min(equity * max_pct * 0.5, risk_budget)
    # Note: 0.5× cap_fraction for risk budget — position_$ will be larger than risk_budget

    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        return 0.0, 0

    shares_from_risk = int(risk_budget / risk_per_share)
    if shares_from_risk <= 0:
        return 0.0, 0

    position_dollar = shares_from_risk * entry_price

    # Hard caps
    max_position = min(engine_capital_remaining, buying_power * 0.95,
                       equity * max_pct)
    if position_dollar > max_position:
        shares_from_risk = max(1, int(max_position / entry_price))
        position_dollar  = shares_from_risk * entry_price

    return round(position_dollar, 2), shares_from_risk


# ── Macro gate ────────────────────────────────────────────────────────────────

def get_macro_multiplier() -> Tuple[float, str]:
    """
    Load macro_context.json and return Engine F's regime multiplier.
    Engine F: RISK_ON=1.0, NEUTRAL=0.75, RISK_OFF=0.0, CRISIS=0.0
    Breakouts fail at high rates in risk-off — gate is strict.

    RF-16: uses probability BLEND from regime_probs if available, not cliff-switched argmax.
    """
    macro  = _load_json(MACRO_FILE, {})
    regime = macro.get("macro_regime", "NEUTRAL")
    probs  = macro.get("regime_probs")
    gates  = ENGINE_REGIME_GATE[ENGINE_ID]

    if probs:
        multiplier = (
            probs.get("RISK_ON",  0) * gates.get("RISK_ON",  1.0) +
            probs.get("NEUTRAL",  0) * gates.get("NEUTRAL",  0.75) +
            probs.get("RISK_OFF", 0) * gates.get("RISK_OFF", 0.0) +
            probs.get("CRISIS",   0) * gates.get("CRISIS",   0.0)
        )
    else:
        multiplier = gates.get(regime, 0.0)

    return round(multiplier, 4), regime


# ── Shadow logging ────────────────────────────────────────────────────────────

def log_shadow(symbol: str, entry_price: float, engine_score: float,
               signals: dict, macro_regime: str, would_trade: bool,
               skip_reason: Optional[str] = None):
    """Append one shadow classification record to shadow_classifications.json.

    Dedup guard: if engine F already logged this symbol today, skip to prevent
    duplicate records when Task Scheduler fires the engine multiple times in one day.
    """
    shadow_data = _load_json(SHADOW_FILE, {"_schema": {}, "classifications": []})
    today = datetime.now().strftime("%Y-%m-%d")

    # Dedup: skip if already logged same engine + symbol + date (non-UNC records only)
    already = any(
        c.get("engine_id") == ENGINE_ID
        and c.get("symbol") == symbol
        and c.get("date") == today
        and "UNC" not in c.get("shadow_id", "")
        for c in shadow_data.get("classifications", [])
    )
    if already:
        logger.debug("DEDUP SKIP F | %s already logged today — not writing duplicate", symbol)
        return

    record = {
        "shadow_id":         f"F-{symbol}-{datetime.now():%Y%m%d-%H%M%S}",
        "engine_id":         ENGINE_ID,
        "symbol":            symbol,
        "date":              today,
        "scan_time":         datetime.now().strftime("%H:%M"),
        "entry_price":       round(entry_price, 4),
        "macro_regime":      macro_regime,
        "engine_score":      engine_score,
        "signals":           signals,
        "would_have_traded": would_trade,
        "skip_reason":       skip_reason,
        "outcome_tagged":    False,
        "forward_return_Nd": None,
    }
    shadow_data["classifications"].append(record)
    _save_json_atomic(SHADOW_FILE, shadow_data)


def log_unconditioned_shadow(symbol: str, entry_price: float, engine_score: float,
                             signals: dict, macro_regime: str, owned_by: Optional[str]):
    """RF-10 mitigation: log symbols that pass signal gates regardless of ownership."""
    shadow_data = _load_json(SHADOW_FILE, {"_schema": {}, "classifications": []})
    record = {
        "shadow_id":              f"F-UNC-{symbol}-{datetime.now():%Y%m%d-%H%M%S}",
        "engine_id":              ENGINE_ID,
        "symbol":                 symbol,
        "date":                   datetime.now().strftime("%Y-%m-%d"),
        "scan_time":              datetime.now().strftime("%H:%M"),
        "entry_price":            round(entry_price, 4),
        "macro_regime":           macro_regime,
        "engine_score":           engine_score,
        "signals":                signals,
        "conditioned_by_ownership": False,
        "owned_by":               owned_by,
        "outcome_tagged":         False,
        "forward_return_Nd":      None,
        "note":                   "RF-10 unconditioned log — signal passed without ownership gate",
    }
    shadow_data["classifications"].append(record)
    _save_json_atomic(SHADOW_FILE, shadow_data)


def count_shadow_entries() -> int:
    shadow = _load_json(SHADOW_FILE, {"classifications": []})
    return sum(1 for c in shadow["classifications"] if c.get("engine_id") == ENGINE_ID)


# ── Core scan ─────────────────────────────────────────────────────────────────

def scan(dry_run: bool = False) -> List[dict]:
    """
    Full Engine F scan.

    1. Macro gate: bail if RISK_OFF or CRISIS.
    2. Load universe, ledger, account state, SPY bars.
    3. Per symbol: ownership → consolidation → breakout bar → volume gate →
       close position gate → overhead clearance gate → score → entry.
    4. Shadow mode: log all passing candidates, no orders.
    5. Live mode: submit orders up to MAX_POSITIONS_PER_ENGINE["F"].
    """
    status = ENGINE_STATUS.get(ENGINE_ID, "off")
    if status == "off":
        logger.info("Engine F is OFF — skipping scan.")
        return []

    macro_mult, macro_regime = get_macro_multiplier()
    if macro_mult == 0.0:
        logger.info("Engine F gated out by macro regime: %s", macro_regime)
        return []

    logger.info("Engine F scan | regime=%s | mult=%.2f | mode=%s",
                macro_regime, macro_mult, status)

    universe_data = _load_json(UNIVERSE_FILE, {})
    symbols = list(universe_data.get("symbols", []))
    if not symbols:
        logger.warning("Universe file empty or missing — aborting scan.")
        return []

    # Fetch SPY bars for relative strength computation
    spy_bars_map = fetch_bars(["SPY"], lookback=30)
    spy_df = spy_bars_map.get("SPY")

    ledger    = Ledger()
    ownership = ledger._load_ownership()

    margin_ok, margin_max_new, margin_reason = True, 10_000, "shadow mode — margin guard not enforced"
    try:
        acct         = get_account()
        equity       = float(acct.get("equity", TOTAL_EQUITY))
        buying_power = float(acct.get("buying_power", 0))
        if status == "live":
            try:
                positions = get_positions()
                margin_ok, margin_max_new, margin_reason = check_margin_safety(acct, positions)
                logger.info("Margin guard: %s", margin_reason)
            except Exception as mg_e:
                margin_ok, margin_max_new = False, 0
                margin_reason = f"margin guard check failed (fail closed): {mg_e}"
                logger.error(margin_reason)
    except Exception as e:
        if status == "live":
            logger.error("Account fetch failed in LIVE mode: %s — aborting scan (no fabricated $)", e)
            return []
        logger.warning("Account fetch failed (shadow mode): %s — using config equity", e)
        equity       = TOTAL_EQUITY
        buying_power = equity * 0.5

    engine_weight    = INITIAL_ENGINE_WEIGHTS[ENGINE_ID]
    engine_ceiling   = equity * engine_weight
    engine_deployed  = ledger.get_engine_capital_deployed(ENGINE_ID)
    engine_remaining = engine_ceiling - engine_deployed
    open_positions   = len(ledger.get_positions(ENGINE_ID))
    max_positions    = MAX_POSITIONS_PER_ENGINE[ENGINE_ID]

    logger.info("Engine F capital: ceiling=$%.0f deployed=$%.0f remaining=$%.0f  "
                "positions=%d/%d",
                engine_ceiling, engine_deployed, engine_remaining,
                open_positions, max_positions)

    thresholds = CLASSIFICATION_THRESHOLDS[ENGINE_ID]
    score_min  = thresholds.get("engine_score_min", 0.60)
    vol_gate   = thresholds.get("breakout_vol_ratio", 1.80)
    close_gate = thresholds.get("close_position_min", 0.75)
    hvn_gate   = thresholds.get("overhead_hvn_atr", 2.0)

    classified        = []
    entries_this_scan = 0

    logger.info("Fetching bars for %d symbols...", len(symbols))
    bars_map = fetch_bars(symbols)
    logger.info("Bars returned for %d symbols", len(bars_map))

    for symbol, df in bars_map.items():

        # GATE 1: Ownership
        if symbol in ownership:
            continue

        if len(df) < CONSOL_LOOKBACK_PREF + 5:
            continue

        price = float(df["close"].iloc[-1])
        if price <= 0:
            continue

        atr = _atr(df)
        if atr <= 0:
            continue

        avg_vol_20d = float(df["volume"].iloc[-21:-1].mean()) if len(df) >= 21 else 0.0
        if avg_vol_20d <= 0:
            continue

        # GATE 2: Consolidation structure
        if not is_valid_consolidation(df, atr):
            continue

        # GATE 3: Breakout bar — close above consolidation high
        if not is_breakout_bar(df):
            continue

        # ── Signals ───────────────────────────────────────────────────────────
        quality, range_atr, consol_low = f_consol_quality(df, atr)
        strength, close_pos, vol_ratio = f_breakout_strength(df, avg_vol_20d)
        vol_decl   = f_vol_declining(df)
        clearance  = f_overhead_clearance(df, atr)
        rs_spy     = f_rel_strength_spy(df, spy_df)
        geometry   = compute_breakout_geometry(df)

        if math.isnan(quality) or math.isnan(consol_low):
            continue

        # GATE 4: Volume must confirm (no exception — blueprint hard gate)
        if vol_ratio < vol_gate:
            continue

        # GATE 5: Close position gate (must be in top quartile)
        if close_pos < close_gate:
            continue

        # GATE 6: No overhead HVN within hvn_gate ATRs
        if clearance != float("inf") and clearance < hvn_gate:
            continue

        # ── Engine score ──────────────────────────────────────────────────────
        engine_score = compute_engine_score(
            quality, strength, clearance, vol_decl, rs_spy
        )

        if engine_score < score_min:
            continue

        # ── Entry parameters ──────────────────────────────────────────────────
        entry_p = compute_entry_params(df, atr, engine_score, consol_low)
        stop_price = entry_p["stop_price"]

        # Sanity check: stop must be below current price
        if stop_price >= price:
            logger.warning("SKIP %s — stop_price %.2f >= entry %.2f (consol_low too high)",
                           symbol, stop_price, price)
            continue

        signals = {
            "f_consol_quality":     quality,
            "f_consol_range_atr":   range_atr,
            "f_breakout_strength":  strength,
            "f_close_position":     close_pos,
            "f_vol_ratio":          vol_ratio,
            "f_overhead_clearance": clearance if clearance != float("inf") else 99.0,
            "f_vol_declining":      vol_decl,
            "f_rel_strength_spy":   rs_spy,
            "f_stop_price":         stop_price,
            "f_consol_low":         consol_low,
            "f_breakout_level":     geometry.get("breakout_level"),
            "f_base_height":        geometry.get("base_height"),
            "f_measured_target":    geometry.get("measured_target"),
            "f_hold_target_days":   entry_p["hold_target_days"],
            "f_kelly_scale":        entry_p["kelly_scale"],
            "atr":                  round(atr, 4),
            "price":                round(price, 4),
        }

        # ── Capacity check ────────────────────────────────────────────────────
        would_trade = True
        skip_reason = None

        if open_positions >= max_positions:
            would_trade = False
            skip_reason = f"at_position_limit ({open_positions}/{max_positions})"

        if engine_remaining < equity * 0.02:
            would_trade = False
            skip_reason = f"engine_capital_exhausted (remaining=${engine_remaining:.0f})"

        # ── Shadow log ────────────────────────────────────────────────────────
        # BUG FIX: previously used `would_trade and (status == "shadow")` which
        # always wrote False once engine is live, silently losing all would_trade=True
        # records and starving IC calibration of valid signal-outcome pairs.
        log_shadow(symbol, price, engine_score, signals, macro_regime,
                   would_trade,
                   skip_reason=skip_reason)

        classified.append({
            "symbol":       symbol,
            "engine_score": engine_score,
            "signals":      signals,
            "would_trade":  would_trade,
            "hold_target":  entry_p["hold_target_days"],
            "stop_price":   stop_price,
            "entry_params": entry_p,
        })

        logger.info(
            "CLASSIFY F | %s | score=%.3f | quality=%.2f | strength=%.3f | "
            "vol_ratio=%.2fx | clearance=%.1fATR | vol_decl=%.0f | "
            "stop=%.2f | measured=%.2f | %s",
            symbol, engine_score, quality, strength, vol_ratio,
            clearance if clearance != float("inf") else 99.0,
            vol_decl, stop_price,
            geometry.get("measured_target", 0),
            "WOULD_TRADE" if would_trade else f"SKIP:{skip_reason}"
        )

        # ── Live entry ────────────────────────────────────────────────────────
        if status == "live" and would_trade and not dry_run:
            if entries_this_scan >= max_positions:
                break

            if not margin_ok:
                logger.info("SKIP %s — margin guard active: %s", symbol, margin_reason)
                continue
            if entries_this_scan >= margin_max_new:
                logger.info("Margin guard cap reached (%d new entries) — stopping entries this scan", margin_max_new)
                break

            position_dollar, qty = compute_position_size(
                equity, entry_p["kelly_scale"], stop_price, price,
                engine_remaining, buying_power
            )

            if qty <= 0:
                logger.warning("SKIP %s — position size computed 0 shares", symbol)
                continue

            limit_price     = round(price * 1.001, 2)
            client_order_id = f"F_entry_{symbol}_{datetime.now():%Y%m%d%H%M%S}"

            # Race-condition guard
            if ledger.is_symbol_owned(symbol):
                logger.info("SKIP %s — claimed between check and order", symbol)
                continue

            order = submit_order(symbol, qty, client_order_id, limit_price, dry_run=dry_run)
            if order and order.get("id"):
                entry_date = datetime.now().strftime("%Y-%m-%d")
                ledger.record_entry(
                    engine_id   = ENGINE_ID,
                    symbol      = symbol,
                    shares      = qty,
                    entry_price = price,
                    date        = entry_date,
                    metadata    = {
                        "engine_score":          engine_score,
                        "kelly_scale":           entry_p["kelly_scale"],
                        "stop_price":            stop_price,
                        "hold_target_days":      entry_p["hold_target_days"],
                        "trail_scale":           entry_p["trail_scale"],
                        "breakout_level":        geometry.get("breakout_level"),
                        "consol_low":            consol_low,
                        "base_height":           geometry.get("base_height"),
                        "measured_target":       geometry.get("measured_target"),
                        "measured_move_trimmed": False,
                        "macro_regime":          macro_regime,
                        "signals_at_entry":      signals,
                    }
                )
                logger.info(
                    "ENTRY F | %s | qty=%d | price=%.2f | stop=%.2f (structural) | "
                    "measured=%.2f | hold=%dd | score=%.3f",
                    symbol, qty, price, stop_price,
                    geometry.get("measured_target", 0),
                    entry_p["hold_target_days"], engine_score
                )
                entries_this_scan += 1
                engine_remaining  -= qty * price
                open_positions    += 1

    shadow_count = count_shadow_entries()
    logger.info(
        "Engine F scan complete | classified=%d | entries=%d | shadow_total=%d/%d",
        len(classified), entries_this_scan, shadow_count, SHADOW_MIN_ENTRIES_FOR_LIVE
    )

    # ── RF-10: Unconditioned pass — score owned symbols for IC bias correction ─
    unc_logged = 0
    for symbol in list(ownership.keys()):
        df = bars_map.get(symbol)
        if df is None or len(df) < 60:
            continue
        price = float(df["close"].iloc[-1])
        if price <= 0:
            continue
        atr = _atr(df)
        if atr <= 0:
            continue
        avg_vol_20d = float(df["volume"].iloc[-21:-1].mean()) if len(df) >= 21 else 0.0
        if avg_vol_20d <= 0:
            continue
        if not is_valid_consolidation(df, atr):
            continue
        if not is_breakout_bar(df):
            continue
        quality, range_atr, consol_low = f_consol_quality(df, atr)
        strength, close_pos, vol_ratio = f_breakout_strength(df, avg_vol_20d)
        vol_decl  = f_vol_declining(df)
        clearance = f_overhead_clearance(df, atr)
        rs_spy    = f_rel_strength_spy(df, spy_df)
        if math.isnan(quality) or math.isnan(consol_low):
            continue
        if vol_ratio < vol_gate or close_pos < close_gate:
            continue
        if clearance != float("inf") and clearance < hvn_gate:
            continue
        engine_score_unc = compute_engine_score(quality, strength, clearance, vol_decl, rs_spy)
        if engine_score_unc < score_min:
            continue
        sigs_unc = {
            "f_consol_quality": quality, "f_breakout_strength": strength,
            "f_vol_ratio": vol_ratio, "f_overhead_clearance": clearance if clearance != float("inf") else 99.0,
            "f_vol_declining": vol_decl, "f_rel_strength_spy": rs_spy,
            "f_consol_low": consol_low, "atr": round(atr, 4), "price": round(price, 4),
        }
        log_unconditioned_shadow(symbol, price, engine_score_unc, sigs_unc,
                                 macro_regime, owned_by=ownership[symbol])
        unc_logged += 1
    if unc_logged:
        logger.info("RF-10: logged %d unconditioned Engine F candidates (owned by others)", unc_logged)

    if status == "shadow" and shadow_count >= SHADOW_MIN_ENTRIES_FOR_LIVE:
        logger.info(
            "ENGINE F SHADOW GATE MET (%d/%d) — update ENGINE_STATUS['F'] = 'live' to enable.",
            shadow_count, SHADOW_MIN_ENTRIES_FOR_LIVE
        )

    return classified


# ── Status ────────────────────────────────────────────────────────────────────

def print_status():
    ledger       = Ledger()
    positions    = ledger.get_positions(ENGINE_ID)
    shadow_count = count_shadow_entries()
    status       = ENGINE_STATUS.get(ENGINE_ID, "off")

    print(f"\n{'='*60}")
    print(f"  ENGINE F STATUS — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")
    print(f"  Mode:            {status.upper()}")
    print(f"  Shadow entries:  {shadow_count}/{SHADOW_MIN_ENTRIES_FOR_LIVE}")
    print(f"  Open positions:  {len(positions)}")
    for p in positions:
        meta = p.get("metadata", {})
        print(f"    {p['symbol']:8s}  {p['shares']} shares @ ${p['entry_price']:.2f}"
              f"  stop=${meta.get('stop_price', '?')}  "
              f"target=${meta.get('measured_target', '?')}  ({p['entry_date']})")
    print(f"{'='*60}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ares Engine F — Structural Breakout")
    parser.add_argument("--scan",   action="store_true", help="Run full scan")
    parser.add_argument("--dry",    action="store_true", help="Dry run — no orders")
    parser.add_argument("--status", action="store_true", help="Show current F positions")
    args = parser.parse_args()

    if args.status:
        print_status()
    elif args.scan:
        results = scan(dry_run=args.dry)
        print(f"\n[Engine F] Scan complete — {len(results)} classification(s)")
    else:
        parser.print_help()
