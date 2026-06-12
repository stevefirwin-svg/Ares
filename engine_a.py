"""
engine_a.py — Ares Engine A: Momentum Continuation
====================================================
Edge: Established uptrend with institutional accumulation. Price is making new highs
      or approaching them; momentum is accelerating. The statistical edge is trend
      persistence — Jegadeesh-Titman (1993) 12-1 momentum anomaly.

Classification gate (ALL must pass):
  1. Symbol not owned by another engine (ownership check FIRST)
  2. Macro regime RISK_ON or NEUTRAL (A gates out in RISK_OFF/CRISIS)
  3. SPY above its 200-day MA (broad market must be in uptrend)
  4. ADX > 25 AND ADX direction positive (trend is established and strengthening)
  5. EMA 8 > EMA 21 > EMA 50 (MA stack bullish)
  6. a_mom_12_1 > 0 (positive 12-1 momentum — not a reversal)
  7. MR score below ceiling (< 0.5) — deeply oversold belongs to Engine B
  8. Engine score >= CLASSIFICATION_THRESHOLDS["A"]["engine_score_min"] (0.60)

Sizing:
  position_$ = min(
      equity × KELLY_BOOTSTRAP_FRACTION × KELLY_SHADOW_MULTIPLIER × kelly_scale,
      engine_capital_remaining,
      buying_power × 0.95
  )
  kelly_scale ∈ [0.5, 1.0] — derived from engine_score percentile vs shadow history

Hold target:
  ADX-based: hold_target_days = clip(5 + (adx - 20) × 0.5, 7, 25)
  Stronger trend = longer hold. (Jegadeesh-Titman momentum half-life ~6 months,
  but swing context caps at 25 days.)

Signals (all prefixed a_):
  a_mom_12_1: (price[-21] / price[-252]) - 1
    Convention: higher = stronger momentum. Positive = upward 12-1 momentum.
    Range: [-1, +inf] | Gate: > 0 (must have positive formation-period return)
    Research: Jegadeesh & Titman (1993) — one of the most replicated anomalies

  a_hi52_proximity: (52w_high - close) / ATR
    Convention: LOWER is better for Engine A (closer to/above 52w high = stronger signal).
    Range: [0, +inf] | Inverted in scoring (proximity ≈ 0 = AT the high)
    Research: George & Hwang (2004) — anchoring bias creates order flow at highs

  a_roc_accel: ROC_5 - ROC_20
    Convention: positive = momentum accelerating (short > long = 2nd derivative positive).
    Range: [-inf, +inf] | Positive means: momentum is speeding up
    Research: ROC acceleration as leading indicator of continued momentum persistence

Shadow mode: ENGINE_STATUS["A"] == "shadow" → classify but do not trade.
All classifications written to shadow_classifications.json.
Gate: 30 shadow classifications before live capital.

Run:
    python engine_a.py --scan        # Run full scan
    python engine_a.py --scan --dry  # Dry run (no orders, no shadow write)
    python engine_a.py --status      # Show current A positions
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
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

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

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [EngineA] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"logs/engine_a_{datetime.now():%Y%m%d}.log"),
    ],
)
logger = logging.getLogger("ares.engine_a")

ENGINE_ID     = "A"
SHADOW_FILE   = "shadow_classifications.json"
MACRO_FILE    = "macro_context.json"
UNIVERSE_FILE = "universe.json"

SPY_LOOKBACK  = 252   # bars needed for SPY 200MA check


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


def fetch_bars(symbols: List[str], lookback: int = BARS_LOOKBACK) -> Dict[str, pd.DataFrame]:
    """Fetch daily OHLCV bars from Alpaca for a list of symbols."""
    import requests as req
    from datetime import timedelta

    end   = datetime.now()
    start = end - timedelta(days=int(lookback * 1.6))  # buffer for weekends/holidays

    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    bars_out = {}

    for i in range(0, len(symbols), 200):
        batch = symbols[i:i+200]
        params = {
            "symbols":   ",".join(batch),
            "timeframe": "1Day",
            "start":     start.strftime("%Y-%m-%d"),
            "end":       end.strftime("%Y-%m-%d"),
            "limit":     10000,
            "feed":      BAR_FEED,
        }
        try:
            resp = req.get(
                "https://data.alpaca.markets/v2/stocks/bars",
                headers=headers, params=params, timeout=30
            )
            resp.raise_for_status()
            data = resp.json().get("bars", {})
            for sym, bar_list in data.items():
                if not bar_list:
                    continue
                df = pd.DataFrame(bar_list)
                df.rename(columns={"o":"open","h":"high","l":"low","c":"close",
                                   "v":"volume","vw":"vwap","t":"timestamp"}, inplace=True)
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df = df.sort_values("timestamp").tail(lookback)
                if len(df) >= 60:
                    bars_out[sym] = df
        except Exception as e:
            logger.warning("Bar fetch batch failed: %s", e)

    return bars_out


def fetch_spy_bars(lookback: int = SPY_LOOKBACK) -> Optional[pd.DataFrame]:
    """Fetch SPY bars separately for the 200MA gate check."""
    bars = fetch_bars(["SPY"], lookback=lookback)
    return bars.get("SPY")


def get_account() -> dict:
    import requests as req
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    resp = req.get(f"{ALPACA_BASE_URL}/v2/account", headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_buying_power() -> float:
    try:
        acct = get_account()
        return float(acct.get("buying_power", 0))
    except Exception:
        return 0.0


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
        "type":            "limit",
        "time_in_force":   "day",
        "limit_price":     str(round(limit_price, 2)),
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


# ── ATR + ATR percentile ──────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, period: int = 14) -> float:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs()
    ], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if not math.isnan(val) else 0.0


def _atr_pct_0to1(df: pd.DataFrame, period: int = 14, lookback: int = 60) -> float:
    """
    ATR percentile over the last `lookback` bars, range [0, 1].
    0 = lowest vol in window (calm), 1 = highest vol (volatile).

    NOTE: This is NOT the signals.py atr_pctile which returns [-1,+1] with
    NEGATIVE = high vol. This helper is freshly defined per Blueprint v1.1 A.10.
    Convention: 0=calm, 1=volatile. All stop formulas in Engine A use this.
    """
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs()
    ], axis=1).max(axis=1)
    atr_series = tr.rolling(period).mean().dropna()

    if len(atr_series) < 2:
        return 0.5  # neutral fallback

    window = atr_series.iloc[-lookback:]
    current = atr_series.iloc[-1]
    lo, hi  = window.min(), window.max()

    if hi == lo:
        return 0.5
    return float(np.clip((current - lo) / (hi - lo), 0.0, 1.0))


# ── SPY regime gate ───────────────────────────────────────────────────────────

def spy_above_200ma(spy_df: Optional[pd.DataFrame]) -> bool:
    """
    Returns True if SPY close is above its 200-day simple moving average.
    Engine A requires a bull market regime — no momentum trades in a bear market.
    Falls back to True (permissive) if SPY data unavailable, logs warning.
    """
    if spy_df is None or len(spy_df) < 200:
        logger.warning("SPY bars insufficient for 200MA check — defaulting permissive")
        return True
    ma200 = float(spy_df["close"].rolling(200).mean().iloc[-1])
    spy_close = float(spy_df["close"].iloc[-1])
    return spy_close > ma200


# ── ADX ───────────────────────────────────────────────────────────────────────

def compute_adx(df: pd.DataFrame, period: int = 14) -> Tuple[float, float]:
    """
    Compute ADX and directional bias (+DI > -DI means bullish).

    Returns (adx_value, adx_direction)
      adx_value: 0-100 (> 25 = trending)
      adx_direction: +1 if +DI > -DI (bullish trend), -1 if bearish

    ADX does not indicate direction, only trend strength.
    +DI vs -DI gives direction.
    """
    if len(df) < period * 2 + 5:
        return 0.0, 0.0

    h = df["high"].values
    l = df["low"].values
    c = df["close"].values

    plus_dm  = np.where((h[1:] - h[:-1]) > (l[:-1] - l[1:]),
                         np.maximum(h[1:] - h[:-1], 0), 0)
    minus_dm = np.where((l[:-1] - l[1:]) > (h[1:] - h[:-1]),
                         np.maximum(l[:-1] - l[1:], 0), 0)

    tr = np.maximum(
        h[1:] - l[1:],
        np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1]))
    )

    def _smooth(arr, n):
        out = np.zeros(len(arr))
        out[n-1] = arr[:n].sum()
        for i in range(n, len(arr)):
            out[i] = out[i-1] - out[i-1] / n + arr[i]
        return out

    atr_s     = _smooth(tr, period)
    plus_di_s = _smooth(plus_dm, period)
    minus_di_s = _smooth(minus_dm, period)

    with np.errstate(divide='ignore', invalid='ignore'):
        plus_di  = np.where(atr_s > 0, 100 * plus_di_s / atr_s, 0)
        minus_di = np.where(atr_s > 0, 100 * minus_di_s / atr_s, 0)
        dx       = np.where((plus_di + minus_di) > 0,
                             100 * np.abs(plus_di - minus_di) / (plus_di + minus_di), 0)

    adx_s = _smooth(dx, period)

    # Final values
    adx_val  = float(adx_s[-1]) if len(adx_s) > 0 else 0.0
    pdi_last = float(plus_di[-1]) if len(plus_di) > 0 else 0.0
    mdi_last = float(minus_di[-1]) if len(minus_di) > 0 else 0.0
    adx_dir  = 1.0 if pdi_last > mdi_last else -1.0

    return round(adx_val, 2), adx_dir


# ── EMA stack ─────────────────────────────────────────────────────────────────

def compute_ema_stack(df: pd.DataFrame) -> Tuple[float, float, float, bool]:
    """
    Compute EMA 8, 21, 50. Returns (e8, e21, e50, stack_bullish).
    stack_bullish = True when EMA8 > EMA21 > EMA50.

    Rationale: MA stack is the structural backbone of a momentum trend.
    Any collapse (E8 below E21) is an early warning for Engine A exit logic.
    """
    c = df["close"]
    e8  = float(c.ewm(span=8,  adjust=False).mean().iloc[-1])
    e21 = float(c.ewm(span=21, adjust=False).mean().iloc[-1])
    e50 = float(c.ewm(span=50, adjust=False).mean().iloc[-1])
    return e8, e21, e50, (e8 > e21 > e50)


# ── Signal computation ────────────────────────────────────────────────────────

def a_mom_12_1(df: pd.DataFrame) -> float:
    """
    12-1 time-series momentum: (price[-21] / price[-252]) - 1.

    Excludes the reversal month (last 21 bars) to avoid short-term mean reversion
    contaminating the momentum signal. Formation period = bars [-252:-21].

    Convention: higher = stronger upward momentum. Gate: > 0.
    Range: [-1, +inf]
    Research: Jegadeesh & Titman (1993), Cross-Sectional Momentum.
    """
    c = df["close"]
    if len(c) < 252:
        return float("nan")
    return round(float(c.iloc[-21] / c.iloc[-252]) - 1.0, 6)


def a_hi52_proximity(df: pd.DataFrame, atr: float) -> float:
    """
    (52-week high - current close) / ATR.

    Convention: LOWER is better for Engine A — closer to/above the 52-week high
    means price is breaking out, not lagging. Scoring inverts this so that lower
    proximity maps to a higher component score.

    Range: [0, +inf] (always non-negative — price can't exceed rolling 252-bar high
    in the denominator, but single bars can exceed prior rolling window; clipped at 0)
    Research: George & Hwang (2004) — reference point anchoring at 52w high
    drives predictable institutional order flow on breakouts.
    """
    if atr <= 0 or len(df) < 252:
        return float("nan")
    hi52  = float(df["high"].rolling(252).max().iloc[-1])
    close = float(df["close"].iloc[-1])
    return round(max(0.0, (hi52 - close) / atr), 4)


def a_roc_accel(df: pd.DataFrame) -> float:
    """
    ROC acceleration: ROC_5 - ROC_20.

    ROC_5  = (close[-1] / close[-6])  - 1   (5-bar rate of change)
    ROC_20 = (close[-1] / close[-21]) - 1   (20-bar rate of change)

    Convention: positive = short-term momentum faster than medium-term (accelerating).
    Negative = momentum plateauing or decelerating.
    Range: [-inf, +inf]
    Rationale: Acceleration is a leading indicator — momentum that is speeding up
    tends to persist longer than momentum that has already plateaued.
    """
    c = df["close"]
    if len(c) < 22:
        return float("nan")
    roc_5  = float(c.iloc[-1] / c.iloc[-6])  - 1.0
    roc_20 = float(c.iloc[-1] / c.iloc[-21]) - 1.0
    return round(roc_5 - roc_20, 6)


def compute_mr_score_proxy(df: pd.DataFrame, atr: float) -> float:
    """
    Lightweight MR proxy score for the routing ceiling check (Blueprint A.4).

    Engine A must NOT fire on symbols that are deeply oversold — those belong to
    Engine B. Gate: mr_proxy < 0.5 (0 = not oversold, 1 = deeply oversold).

    Computed from stochastic %K: normalized to [0,1] where 1 = deeply oversold.
    This is a routing guard, not a tradeable signal — no prefix needed.
    """
    if len(df) < 20:
        return 0.0
    h = df["high"].rolling(14).max()
    l = df["low"].rolling(14).min()
    denom = h - l
    denom = denom.replace(0, float("nan"))
    stoch_k = ((df["close"] - l) / denom * 100).fillna(50)
    k_val = float(stoch_k.iloc[-1])
    # k < 20 → deeply oversold → high MR score; k > 80 → overbought → low MR
    # Normalize: low stoch = high MR proxy
    return round(float(np.clip((100 - k_val) / 100, 0.0, 1.0)), 4)


# ── Cross-sectional normalization helpers ─────────────────────────────────────

def _zscore_clip(value: float, universe_vals: List[float], clip: float = 3.0) -> float:
    """
    Cross-sectional z-score of value within universe_vals, clipped to [-clip, +clip].
    Maps to [0,1] via logistic transform for consistent component scoring.
    Returns 0.5 (neutral) if universe is empty or std is zero.
    """
    if not universe_vals or len(universe_vals) < 5:
        return 0.5
    arr = np.array(universe_vals, dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) < 5:
        return 0.5
    mu, sigma = float(arr.mean()), float(arr.std())
    if sigma == 0:
        return 0.5
    z = float(np.clip((value - mu) / sigma, -clip, clip))
    return float(1 / (1 + math.exp(-z)))  # logistic → [0,1]


# ── Engine A scoring ──────────────────────────────────────────────────────────

def compute_engine_score(
    adx_val:       float,
    stack_bullish: bool,
    mom_12_1:      float,
    hi52_prox:     float,
    roc_accel:     float,
    universe_mom:  List[float],
    universe_prox: List[float],
    universe_roc:  List[float],
) -> float:
    """
    Engine A composite score ∈ [0, 1].

    Components:
      1. ADX strength score:   adx_val clipped [20,50] → [0,1]
      2. MA stack bonus:       1.0 if stack_bullish else 0.0
      3. mom_12_1 score:       cross-sectional z-score vs universe → [0,1]
      4. hi52_proximity score: inverted cross-sectional z-score
      5. roc_accel score:      cross-sectional z-score vs universe → [0,1]

    Weighting (RF-14): loads calibrated IC weights from sub_engine_params.json via
    ares_weights.load_signal_weights("A"). Falls back to equal weights (0.20 each)
    when calibration is unavailable (bootstrap phase).

    The IC-calibrated weights cover signals 3-5 (the three declared Engine A signals).
    ADX (1) and stack (2) are structural gates that contribute to score equally at
    0.20 each alongside the signal components; their weights are not IC-calibrated
    since they are entry conditions, not predictive features.
    """
    from ares_weights import load_signal_weights

    # Component 1: ADX strength (25 = minimum trend, 50 = very strong)
    adx_score = float(np.clip((adx_val - 20) / 30, 0.0, 1.0))

    # Component 2: MA stack (structural confirmation — binary)
    stack_score = 1.0 if stack_bullish else 0.0

    # Component 3: 12-1 momentum — cross-sectional z-score
    if math.isnan(mom_12_1):
        mom_score = 0.5
    else:
        mom_score = _zscore_clip(mom_12_1, universe_mom)

    # Component 4: 52-week proximity — inverted (lower = closer to high = better)
    if math.isnan(hi52_prox):
        prox_score = 0.5
    else:
        neg_prox = -hi52_prox
        prox_score = _zscore_clip(neg_prox, [-x for x in universe_prox if not math.isnan(x)])

    # Component 5: ROC acceleration — cross-sectional z-score
    if math.isnan(roc_accel):
        roc_score = 0.5
    else:
        roc_score = _zscore_clip(roc_accel, universe_roc)

    # RF-14: Load IC-calibrated weights for the three signal components
    weights = load_signal_weights("A")
    w_mom  = weights.get("a_mom_12_1",       1.0 / 3)
    w_prox = weights.get("a_hi52_proximity",  1.0 / 3)
    w_roc  = weights.get("a_roc_accel",       1.0 / 3)

    # Structural components (ADX, stack) get fixed 0.20 each; signals share remaining 0.60
    signal_score = w_mom * mom_score + w_prox * prox_score + w_roc * roc_score
    score = 0.20 * adx_score + 0.20 * stack_score + 0.60 * signal_score

    return round(float(np.clip(score, 0.0, 1.0)), 4)


# ── Entry parameters ──────────────────────────────────────────────────────────

def compute_entry_params(df: pd.DataFrame, atr: float, adx_val: float,
                          engine_score: float) -> dict:
    """
    Engine A entry parameters — all derived, none chosen.

    stop_mult: linear interpolation between 3.5 (calm) and 2.8 (volatile)
      based on atr_pct_0to1. Inverse relationship: wider trend = tighter stop
      because high-vol trends have more noise — momentum must be cleaner.
      stop_mult = 3.5 - (0.7 × atr_pct_0to1)

    trail_scale: conviction-scaled. Higher score = wider trail (let winners run).
      score 0.60 → trail_scale 1.0; score 0.90 → trail_scale 1.4
      trail_scale = 1.0 + 0.4 × (score - 0.60) / 0.30, clipped [1.0, 1.4]
      Note: trail_scale is Engine A's own policy, applied inside _trail_mult_v2.
      See Blueprint v1.1 A.7 — no external trail_scale post-multiplier.

    hold_target_days: ADX-based. Stronger trend = longer hold.
      hold = clip(5 + (adx - 20) × 0.5, 7, 25)
      Research: Jegadeesh-Titman momentum half-life ~6 months;
      swing context caps at 25 trading days.

    kelly_scale ∈ [0.5, 1.0]: linear mapping from score percentile.
      score 0.60 → 0.5; score 1.0 → 1.0
    """
    atr_pctile = _atr_pct_0to1(df)

    stop_mult = round(3.5 - (0.7 * atr_pctile), 3)
    stop_mult = float(np.clip(stop_mult, 2.8, 3.5))

    trail_scale = 1.0 + 0.4 * float(np.clip((engine_score - 0.60) / 0.30, 0.0, 1.0))
    trail_scale = round(float(np.clip(trail_scale, 1.0, 1.4)), 3)

    adx_hold  = 5 + (adx_val - 20) * 0.5
    hold_target_days = int(np.clip(adx_hold, 7, 25))

    kelly_scale = 0.5 + float(np.clip((engine_score - 0.60) / 0.40, 0.0, 0.5))
    kelly_scale = round(float(np.clip(kelly_scale, 0.5, 1.0)), 4)

    return {
        "stop_mult":        stop_mult,
        "trail_scale":      trail_scale,
        "hold_target_days": hold_target_days,
        "kelly_scale":      kelly_scale,
        "atr_pct_0to1":     round(atr_pctile, 4),
    }


# ── Position sizing ───────────────────────────────────────────────────────────

def compute_position_size(equity: float, kelly_scale: float,
                           engine_capital_remaining: float,
                           buying_power: float) -> Tuple[float, float]:
    """
    Engine A position sizing.

    position_$ = min(
        equity × KELLY_BOOTSTRAP_FRACTION × KELLY_SHADOW_MULTIPLIER × kelly_scale,
        engine_capital_remaining,
        buying_power × 0.95
    )

    RF-13: min_pct floor is NOT applied during bootstrap (KELLY_APPLY_FLOOR_IN_BOOTSTRAP=False).
    This allows conviction (kelly_scale) and macro_mult to actually affect sizing.
    Only max_pct clips the ceiling. Floor re-engages post-calibration.
    Returns (position_dollar, kelly_scale).
    """
    from ares_config import KELLY_APPLY_FLOOR_IN_BOOTSTRAP
    from ares_weights import is_calibrated
    min_pct, max_pct = KELLY_ENGINE_CLIPS[ENGINE_ID]

    kelly_dollar = equity * KELLY_BOOTSTRAP_FRACTION * KELLY_SHADOW_MULTIPLIER * kelly_scale

    # RF-13: only apply floor post-calibration
    if KELLY_APPLY_FLOOR_IN_BOOTSTRAP or is_calibrated(ENGINE_ID):
        kelly_dollar = max(equity * min_pct, kelly_dollar)
    kelly_dollar = min(equity * max_pct, kelly_dollar)

    position_dollar = min(kelly_dollar, engine_capital_remaining, buying_power * 0.95)
    return position_dollar, kelly_scale


# ── Macro gate ────────────────────────────────────────────────────────────────

def get_macro_multiplier() -> Tuple[float, str]:
    """
    Load macro_context.json and return Engine A's regime multiplier.
    Engine A: RISK_ON=1.0, NEUTRAL=0.75, RISK_OFF=0.0, CRISIS=0.0

    RF-16: uses probability BLEND from regime_probs if available, not cliff-switched argmax.
    Identical pattern to Engine C/E (which already used blend). Rule 17 compliance.
    Falls back to discrete label only when probs are absent (cold start).
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
    """Append one shadow classification record to shadow_classifications.json."""
    shadow_data = _load_json(SHADOW_FILE, {"_schema": {}, "classifications": []})

    record = {
        "shadow_id":         f"A-{symbol}-{datetime.now():%Y%m%d-%H%M%S}",
        "engine_id":         ENGINE_ID,
        "symbol":            symbol,
        "date":              datetime.now().strftime("%Y-%m-%d"),
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
    """
    RF-10 mitigation: log symbols that pass ALL signal gates regardless of ownership.
    Written to shadow_classifications.json with conditioned_by_ownership=False.
    Used for unconditioned IC estimation — measuring signal power on the full
    opportunity set, not just the scan-order-dependent subset.

    owned_by: the engine that owns this symbol ('B', 'F', etc.) or None if unowned.
    Only call this for symbols that would have been classified if ownership were ignored.
    """
    shadow_data = _load_json(SHADOW_FILE, {"_schema": {}, "classifications": []})

    record = {
        "shadow_id":              f"A-UNC-{symbol}-{datetime.now():%Y%m%d-%H%M%S}",
        "engine_id":              ENGINE_ID,
        "symbol":                 symbol,
        "date":                   datetime.now().strftime("%Y-%m-%d"),
        "scan_time":              datetime.now().strftime("%H:%M"),
        "entry_price":            round(entry_price, 4),
        "macro_regime":           macro_regime,
        "engine_score":           engine_score,
        "signals":                signals,
        "conditioned_by_ownership": False,  # RF-10: unconditioned observation
        "owned_by":               owned_by,
        "outcome_tagged":         False,
        "forward_return_Nd":      None,
        "note":                   "RF-10 unconditioned log — signal passed without ownership gate",
    }
    shadow_data["classifications"].append(record)
    _save_json_atomic(SHADOW_FILE, shadow_data)



    shadow = _load_json(SHADOW_FILE, {"classifications": []})
    return sum(1 for c in shadow["classifications"] if c.get("engine_id") == ENGINE_ID)


# ── Core scan ─────────────────────────────────────────────────────────────────

def scan(dry_run: bool = False) -> List[dict]:
    """
    Full Engine A scan.

    1. Macro gate: bail if RISK_OFF or CRISIS.
    2. SPY 200MA gate: bail if broad market is in a bear market.
    3. Load universe, ledger, account state.
    4. Build cross-sectional universe stats (z-scoring requires universe-wide distributions).
    5. Per symbol: ownership check → signal compute → gate check → score → entry.
    6. Shadow mode: log all passing candidates, no orders.
    7. Live mode: submit orders up to MAX_POSITIONS_PER_ENGINE["A"].
    """
    status = ENGINE_STATUS.get(ENGINE_ID, "off")
    if status == "off":
        logger.info("Engine A is OFF — skipping scan.")
        return []

    macro_mult, macro_regime = get_macro_multiplier()
    if macro_mult == 0.0:
        logger.info("Engine A gated out by macro regime: %s (multiplier=0)", macro_regime)
        return []

    logger.info("Engine A scan | regime=%s | mult=%.2f | mode=%s",
                macro_regime, macro_mult, status)

    # ── SPY 200MA gate ────────────────────────────────────────────────────────
    logger.info("Fetching SPY bars for 200MA gate...")
    spy_df = fetch_spy_bars()
    if not spy_above_200ma(spy_df):
        logger.info("Engine A gated out: SPY below 200MA — no momentum trades in bear market.")
        return []
    logger.info("SPY 200MA gate: PASS")

    # ── Universe ──────────────────────────────────────────────────────────────
    universe_data = _load_json(UNIVERSE_FILE, {})
    symbols = list(universe_data.get("symbols", []))
    if not symbols:
        logger.warning("Universe file empty or missing — aborting scan.")
        return []
    logger.info("Universe size: %d symbols", len(symbols))

    # ── Ledger + account ──────────────────────────────────────────────────────
    ledger    = Ledger()
    ownership = ledger._load_ownership()

    try:
        acct         = get_account()
        equity       = float(acct.get("equity", TOTAL_EQUITY))
        buying_power = float(acct.get("buying_power", 0))
    except Exception as e:
        if status == "live":
            # RF-19: Never fabricate account state in live mode — abort scan
            logger.error("Account fetch failed in LIVE mode: %s — aborting scan (no fabricated $)", e)
            return []
        # Shadow mode: safe to use config values (no orders will be submitted)
        logger.warning("Account fetch failed (shadow mode): %s — using config equity", e)
        equity       = TOTAL_EQUITY
        buying_power = equity * 0.5

    engine_weight    = INITIAL_ENGINE_WEIGHTS[ENGINE_ID]
    engine_ceiling   = equity * engine_weight
    engine_deployed  = ledger.get_engine_capital_deployed(ENGINE_ID)
    engine_remaining = engine_ceiling - engine_deployed
    open_positions   = len(ledger.get_positions(ENGINE_ID))
    max_positions    = MAX_POSITIONS_PER_ENGINE[ENGINE_ID]

    logger.info("Engine A capital: ceiling=$%.0f deployed=$%.0f remaining=$%.0f  "
                "positions=%d/%d",
                engine_ceiling, engine_deployed, engine_remaining,
                open_positions, max_positions)

    thresholds  = CLASSIFICATION_THRESHOLDS[ENGINE_ID]
    stop_params = ENGINE_STOP_PARAMS[ENGINE_ID]
    score_min   = thresholds.get("engine_score_min", 0.60)
    mr_ceiling  = thresholds.get("mr_ceiling", 0.50)
    classified  = []
    entries_this_scan = 0

    # ── Fetch all bars in one batch ───────────────────────────────────────────
    logger.info("Fetching bars for %d symbols...", len(symbols))
    bars_map = fetch_bars(symbols)
    logger.info("Bars returned for %d symbols", len(bars_map))

    # ── Pass 1: compute universe-wide signal distributions for z-scoring ──────
    # Engine A scoring is cross-sectional — signals must be z-scored vs the universe.
    # We compute all three signals for all eligible symbols first, then score.
    logger.info("Computing universe signal distributions for cross-sectional z-scoring...")
    universe_mom  = []
    universe_prox = []
    universe_roc  = []

    for sym, df in bars_map.items():
        if sym in ownership:
            continue
        if len(df) < 252:
            continue
        atr = _atr(df)
        if atr <= 0:
            continue
        m = a_mom_12_1(df)
        p = a_hi52_proximity(df, atr)
        r = a_roc_accel(df)
        if not math.isnan(m):
            universe_mom.append(m)
        if not math.isnan(p):
            universe_prox.append(p)
        if not math.isnan(r):
            universe_roc.append(r)

    logger.info("Universe distribution sizes: mom=%d  prox=%d  roc=%d",
                len(universe_mom), len(universe_prox), len(universe_roc))

    # ── Pass 2: classify ──────────────────────────────────────────────────────
    for symbol, df in bars_map.items():

        # GATE 1: Ownership (first — cheapest check)
        if symbol in ownership:
            continue

        if len(df) < 252:
            continue   # need full year for mom_12_1

        price = float(df["close"].iloc[-1])
        if price <= 0:
            continue

        atr = _atr(df)
        if atr <= 0:
            continue

        # ── Signals ───────────────────────────────────────────────────────────
        mom    = a_mom_12_1(df)
        prox   = a_hi52_proximity(df, atr)
        roc    = a_roc_accel(df)
        adx_val, adx_dir = compute_adx(df)
        e8, e21, e50, stack_bullish = compute_ema_stack(df)
        mr_proxy = compute_mr_score_proxy(df, atr)

        signals = {
            "a_mom_12_1":       mom,
            "a_hi52_proximity": prox,
            "a_roc_accel":      roc,
            "a_adx":            adx_val,
            "a_adx_dir":        adx_dir,
            "a_ema8":           round(e8, 4),
            "a_ema21":          round(e21, 4),
            "a_ema50":          round(e50, 4),
            "a_stack_bullish":  stack_bullish,
            "a_mr_proxy":       mr_proxy,
            "atr":              round(atr, 4),
            "price":            round(price, 4),
        }

        # GATE 2: Minimum data for 12-1 momentum
        if math.isnan(mom):
            continue

        # GATE 3: Positive 12-1 momentum — must be an uptrend, not a reversal
        if mom <= 0:
            continue

        # GATE 4: ADX trend strength and direction
        if adx_val < 25:
            continue   # not trending
        if adx_dir < 0:
            continue   # bearish trend — Engine A is long-only

        # GATE 5: MA stack must be bullish
        if not stack_bullish:
            continue

        # GATE 6: MR routing ceiling (Blueprint A.4)
        # If deeply oversold, belongs to Engine B — do not double-classify
        if mr_proxy >= mr_ceiling:
            continue

        # ── Engine score ──────────────────────────────────────────────────────
        engine_score = compute_engine_score(
            adx_val, stack_bullish, mom, prox, roc,
            universe_mom, universe_prox, universe_roc,
        )

        if engine_score < score_min:
            continue

        # ── Capacity check ────────────────────────────────────────────────────
        would_trade = True
        skip_reason = None

        if open_positions >= max_positions:
            would_trade = False
            skip_reason = f"at_position_limit ({open_positions}/{max_positions})"

        if engine_remaining < equity * 0.02:
            would_trade = False
            skip_reason = f"engine_capital_exhausted (remaining=${engine_remaining:.0f})"

        # ── Entry parameters (computed for every classification, live and shadow) ──
        entry_p = compute_entry_params(df, atr, adx_val, engine_score)

        signals.update({
            "a_stop_mult":        entry_p["stop_mult"],
            "a_trail_scale":      entry_p["trail_scale"],
            "a_hold_target_days": entry_p["hold_target_days"],
            "a_kelly_scale":      entry_p["kelly_scale"],
            "a_atr_pct_0to1":     entry_p["atr_pct_0to1"],
        })

        # ── Shadow log (always, regardless of mode) ───────────────────────────
        log_shadow(symbol, price, engine_score, signals, macro_regime,
                   would_trade and (status == "shadow"),
                   skip_reason=skip_reason)

        classified.append({
            "symbol":       symbol,
            "engine_score": engine_score,
            "signals":      signals,
            "would_trade":  would_trade,
            "hold_target":  entry_p["hold_target_days"],
            "entry_params": entry_p,
        })

        logger.info(
            "CLASSIFY A | %s | score=%.3f | mom12_1=%+.2f%% | "
            "prox=%.1fATR | roc_accel=%+.4f | adx=%.1f | hold=%dd | %s",
            symbol, engine_score, mom * 100, prox, roc, adx_val,
            entry_p["hold_target_days"],
            "WOULD_TRADE" if would_trade else f"SKIP:{skip_reason}"
        )

        # ── Live entry ────────────────────────────────────────────────────────
        if status == "live" and would_trade and not dry_run:
            if entries_this_scan >= max_positions:
                break

            position_dollar, kelly_scale = compute_position_size(
                equity, entry_p["kelly_scale"], engine_remaining, buying_power
            )

            stop_price = round(price - entry_p["stop_mult"] * atr, 4)
            qty        = max(1, int(position_dollar / price))
            limit_price = round(price * 1.001, 2)  # 10bps above last close for fill certainty

            client_order_id = f"A_entry_{symbol}_{datetime.now():%Y%m%d%H%M%S}"

            # Race-condition guard — re-check ownership before order
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
                        "engine_score":     engine_score,
                        "kelly_scale":      entry_p["kelly_scale"],
                        "stop_price":       stop_price,
                        "hold_target_days": entry_p["hold_target_days"],
                        "trail_scale":      entry_p["trail_scale"],
                        "stop_mult":        entry_p["stop_mult"],
                        "macro_regime":     macro_regime,
                        "signals_at_entry": signals,
                        "ema8":             round(e8, 4),
                        "ema21":            round(e21, 4),
                        "ema50":            round(e50, 4),
                    }
                )
                logger.info(
                    "ENTRY A | %s | qty=%d | price=%.2f | stop=%.2f (%.2f ATR) | "
                    "hold=%dd | score=%.3f | kelly=%.3f",
                    symbol, qty, price, stop_price, entry_p["stop_mult"],
                    entry_p["hold_target_days"], engine_score, entry_p["kelly_scale"]
                )
                entries_this_scan += 1
                engine_remaining  -= qty * price
                open_positions    += 1

    shadow_count = count_shadow_entries()
    logger.info(
        "Engine A scan complete | classified=%d | entries=%d | shadow_total=%d/%d",
        len(classified), entries_this_scan, shadow_count, SHADOW_MIN_ENTRIES_FOR_LIVE
    )

    # ── RF-10: Unconditioned pass — score owned symbols for IC bias correction ─
    # Log signals for symbols currently owned by OTHER engines but that would have
    # passed Engine A's gates. Used to compute unconditioned IC (no selection bias).
    # Does NOT change ownership, does NOT trigger orders.
    unc_logged = 0
    for symbol, df in bars_map.items():
        if symbol not in ownership:
            continue  # only care about symbols BLOCKED by ownership
        if len(df) < 252:
            continue

        price = float(df["close"].iloc[-1])
        if price <= 0:
            continue
        atr = _atr(df)
        if atr <= 0:
            continue

        mom   = a_mom_12_1(df)
        prox  = a_hi52_proximity(df, atr)
        roc   = a_roc_accel(df)
        adx_val, adx_dir = compute_adx(df)
        _, _, _, stack_bullish = compute_ema_stack(df)
        mr_proxy = compute_mr_score_proxy(df, atr)

        if math.isnan(mom) or mom <= 0:
            continue
        if adx_val < 25 or adx_dir < 0:
            continue
        if not stack_bullish:
            continue
        if mr_proxy >= thresholds.get("mr_ceiling", 0.50):
            continue

        sigs_unc = {
            "a_mom_12_1": mom, "a_hi52_proximity": prox, "a_roc_accel": roc,
            "a_adx": adx_val, "a_stack_bullish": stack_bullish, "atr": round(atr, 4),
            "price": round(price, 4),
        }
        engine_score_unc = compute_engine_score(
            adx_val, stack_bullish, mom, prox, roc,
            universe_mom, universe_prox, universe_roc,
        )
        if engine_score_unc < score_min:
            continue

        log_unconditioned_shadow(symbol, price, engine_score_unc, sigs_unc,
                                 macro_regime, owned_by=ownership[symbol])
        unc_logged += 1

    if unc_logged:
        logger.info("RF-10: logged %d unconditioned Engine A candidates (owned by others)", unc_logged)

    if status == "shadow" and shadow_count >= SHADOW_MIN_ENTRIES_FOR_LIVE:
        logger.info(
            "ENGINE A SHADOW GATE MET (%d/%d) — update ENGINE_STATUS['A'] = 'live' to enable.",
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
    print(f"  ENGINE A STATUS — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")
    print(f"  Mode:            {status.upper()}")
    print(f"  Shadow entries:  {shadow_count}/{SHADOW_MIN_ENTRIES_FOR_LIVE}")
    print(f"  Open positions:  {len(positions)}")
    for p in positions:
        print(f"    {p['symbol']:8s}  {p['shares']} shares @ ${p['entry_price']:.2f}"
              f"  ({p['entry_date']})")
    print(f"{'='*60}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ares Engine A — Momentum Continuation")
    parser.add_argument("--scan",   action="store_true", help="Run full scan")
    parser.add_argument("--dry",    action="store_true", help="Dry run — no orders")
    parser.add_argument("--status", action="store_true", help="Show current A positions")
    args = parser.parse_args()

    if args.status:
        print_status()
    elif args.scan:
        results = scan(dry_run=args.dry)
        print(f"\n[Engine A] Scan complete — {len(results)} classification(s)")
    else:
        parser.print_help()
