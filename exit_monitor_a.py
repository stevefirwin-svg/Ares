"""
exit_monitor_a.py — Engine A Exit Logic
=========================================
Engine A exits are trend-thesis driven. The question is always:
"Is the momentum trend still intact?" — not "how far did price fall?"

Exit conditions (checked in priority order):

  EXIT A-1: HARD STOP
    price <= stop_price stored in ledger metadata (entry_price - stop_mult × ATR)
    stop_mult ∈ [2.8, 3.5] — vol-adjusted at entry. Wider than B: momentum trends
    need room. A hard stop here means the trend thesis is structurally broken.

  EXIT A-2: MA STACK COLLAPSE
    EMA 8 crosses below EMA 21 AND days_held > 50% of hold_target.
    MA stack collapse is the structural confirmation that the trend is over.
    Only fires after the first half of hold_target (filters early noise).

  EXIT A-3: TREND EXHAUSTION
    ADX was > 30 AND has declined > 5 points over the last 3 bars AND position
    is in profit. ADX rolling over from a high reading = trend losing steam.
    Only exits if profitable — no reason to exit at a loss on ADX noise alone.

  EXIT A-4: MOMENTUM BREAK
    ROC_5 (5-bar rate of change) flips negative AND pnl > 3%.
    Short-term momentum has reversed while we're in profit — take the gain.
    Gate: pnl > 3% prevents premature exit on early volatility noise.

  EXIT A-5: TRAIL STOP
    Activates once price > entry_price + 1.0 ATR (position has a cushion).
    trail_stop = high_water - (2.5 × trail_scale × ATR)
    trail_scale from entry metadata ∈ [1.0, 1.4] — higher conviction = wider trail.
    Engine A trail is wider than B: momentum positions need room to breathe.

  EXIT A-6: TIME STOP
    days_held >= hold_target_days (ADX-derived at entry, range 7–25).
    Momentum trades have a shelf life — if trend hasn't paid by target, exit.

Usage:
    from exit_monitor_a import check_engine_a_exits
    exits = check_engine_a_exits(positions, bars_map, ledger, alpaca_key,
                                  alpaca_secret, base_url, dry_run=False)

Not run standalone — called from ares_exit_monitor.py engine dispatch.
"""

import math
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ares_config import ENGINE_STOP_PARAMS
from ledger import Ledger

logger = logging.getLogger("ares.exit_a")

ENGINE_ID = "A"

# Trail base multiplier for Engine A.
# trail_stop = high_water - (A_TRAIL_BASE_ATR × trail_scale × atr)
# trail_scale ∈ [1.0, 1.4] from entry metadata → effective trail 2.5–3.5 ATR
# Wider than Engine B (1.5 ATR): momentum positions need room during retracements.
A_TRAIL_BASE_ATR = 2.5

# Trail only activates once price has risen this many ATRs above entry
A_TRAIL_ACTIVATION_ATR = 1.0

# ADX exhaustion thresholds
A_ADX_MIN_FOR_EXHAUSTION = 30.0   # ADX must have been this high to flag exhaustion
A_ADX_DECLINE_THRESHOLD  = 5.0   # ADX must drop this many points over 3 bars

# Momentum break thresholds
A_ROC5_NEGATIVE_THRESHOLD = 0.0    # ROC_5 flips negative
A_MOMENTUM_BREAK_MIN_PNL  = 0.03   # only exit on momentum break if > 3% profit


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


def _ema(series: pd.Series, span: int) -> float:
    return float(series.ewm(span=span, adjust=False).mean().iloc[-1])


def _compute_adx_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Return a Series of ADX values. Used to detect ADX slope over recent bars.
    Requires at least period*3 bars for a reliable series tail.
    """
    if len(df) < period * 3:
        return pd.Series(dtype=float)

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
        if len(arr) < n:
            return out
        out[n-1] = arr[:n].sum()
        for i in range(n, len(arr)):
            out[i] = out[i-1] - out[i-1] / n + arr[i]
        return out

    atr_s      = _smooth(tr, period)
    pdi_s      = _smooth(plus_dm, period)
    mdi_s      = _smooth(minus_dm, period)

    with np.errstate(divide='ignore', invalid='ignore'):
        pdi  = np.where(atr_s > 0, 100 * pdi_s / atr_s, 0)
        mdi  = np.where(atr_s > 0, 100 * mdi_s / atr_s, 0)
        dx   = np.where((pdi + mdi) > 0,
                         100 * np.abs(pdi - mdi) / (pdi + mdi), 0)

    adx_arr = _smooth(dx, period)
    return pd.Series(adx_arr)


def _adx_slope(df: pd.DataFrame, lookback: int = 3) -> Tuple[float, float]:
    """
    Returns (adx_now, adx_change_over_lookback_bars).
    adx_change < 0 = ADX declining (trend losing strength).
    """
    adx_series = _compute_adx_series(df)
    if len(adx_series) < lookback + 1:
        return 0.0, 0.0
    adx_now  = float(adx_series.iloc[-1])
    adx_then = float(adx_series.iloc[-(lookback + 1)])
    return adx_now, adx_now - adx_then


def _roc5(df: pd.DataFrame) -> float:
    """5-bar rate of change: (close[-1] / close[-6]) - 1."""
    c = df["close"]
    if len(c) < 6:
        return 0.0
    return float(c.iloc[-1] / c.iloc[-6]) - 1.0


def _high_water(df: pd.DataFrame, entry_date_str: str, entry_price: float) -> float:
    """
    Max close since entry date from available bars.
    Falls back to max(entry_price, current_close) if bars don't cover entry date.
    """
    try:
        entry_dt = datetime.strptime(str(entry_date_str)[:10], "%Y-%m-%d").date()
        if "timestamp" in df.columns:
            df_copy = df.copy()
            df_copy["ts_date"] = pd.to_datetime(df_copy["timestamp"]).dt.date
            since_entry = df_copy[df_copy["ts_date"] >= entry_dt]
            if not since_entry.empty:
                return float(since_entry["close"].max())
    except Exception:
        pass
    return max(float(df["close"].iloc[-1]), entry_price)


def _days_held(entry_date_str: str) -> int:
    try:
        entry = datetime.strptime(str(entry_date_str)[:10], "%Y-%m-%d").date()
        return (datetime.now().date() - entry).days
    except Exception:
        return 0


def submit_sell(symbol: str, qty: int, reason: str,
                alpaca_key: str, alpaca_secret: str, base_url: str,
                dry_run: bool = False) -> Optional[dict]:
    if dry_run:
        logger.info("[DRY] Would sell %d %s — %s", qty, symbol, reason)
        return {"id": "dry-run", "symbol": symbol, "qty": qty, "reason": reason}

    import requests as req
    headers = {
        "APCA-API-KEY-ID":     alpaca_key,
        "APCA-API-SECRET-KEY": alpaca_secret,
        "Content-Type":        "application/json",
    }
    client_order_id = f"A_{reason}_{symbol}_{datetime.now():%Y%m%d%H%M%S}"
    body = {
        "symbol":          symbol,
        "qty":             str(qty),
        "side":            "sell",
        "type":            "market",
        "time_in_force":   "day",
        "client_order_id": client_order_id,
    }
    try:
        resp = req.post(f"{base_url}/v2/orders", headers=headers, json=body, timeout=10)
        resp.raise_for_status()
        order = resp.json()
        logger.info("SELL ORDER A | %s | qty=%d | reason=%s | order_id=%s",
                    symbol, qty, reason, order.get("id", "?")[:8])
        return order
    except Exception as e:
        logger.error("Sell order failed for %s: %s", symbol, e)
        return None


# ── Exit logic ────────────────────────────────────────────────────────────────

def check_engine_a_exits(
    positions:     List[dict],
    bars_map:      Dict[str, pd.DataFrame],
    ledger:        Ledger,
    alpaca_key:    str,
    alpaca_secret: str,
    base_url:      str,
    dry_run:       bool = False,
) -> List[dict]:
    """
    Check all open Engine A positions against exit conditions.

    Args:
        positions     — list of open position dicts from ledger.get_positions("A")
        bars_map      — {symbol: DataFrame} from bar fetch
        ledger        — Ares Ledger instance
        dry_run       — if True, log exits but don't submit orders

    Returns list of exit records [{symbol, exit_reason, price, pnl_pct}, ...]
    """
    exits_executed = []
    stop_params    = ENGINE_STOP_PARAMS[ENGINE_ID]

    for pos in positions:
        symbol      = pos.get("symbol")
        entry_price = float(pos.get("entry_price", 0))
        shares      = pos.get("shares", 0)
        meta        = pos.get("metadata", {})
        entry_date  = pos.get("entry_date", "")

        if not symbol or entry_price <= 0 or shares <= 0:
            continue

        df = bars_map.get(symbol)
        if df is None or len(df) < 10:
            logger.warning("EXIT A: no bars for %s — skipping", symbol)
            continue

        price       = float(df["close"].iloc[-1])
        atr         = _atr(df)
        days        = _days_held(entry_date)
        hold_target = int(meta.get("hold_target_days", 15))
        trail_scale = float(meta.get("trail_scale", 1.0))

        # Pull stop_price from entry metadata; fall back to vol-adjusted ATR stop
        stop_price_stored = meta.get("stop_price")
        if stop_price_stored is None:
            stop_mult  = stop_params.get("stop_mult_low_vol", 3.5)
            stop_price = entry_price - stop_mult * atr
        else:
            stop_price = float(stop_price_stored)

        pnl_pct = round(((price / entry_price) - 1) * 100, 4) if entry_price else 0.0

        # EMA stack for collapse detection
        c         = df["close"]
        e8        = _ema(c, 8)
        e21       = _ema(c, 21)
        stack_ok  = e8 > e21

        # ADX slope for exhaustion detection
        adx_now, adx_change = _adx_slope(df, lookback=3)

        # ROC_5 for momentum break detection
        roc5_val = _roc5(df)

        # High water mark and trail stop
        hw        = _high_water(df, entry_date, entry_price)
        trail_atr = A_TRAIL_BASE_ATR * trail_scale
        trail_stop = hw - (trail_atr * atr)

        exit_reason = None
        exit_detail = ""

        # ── EXIT A-1: Hard stop ───────────────────────────────────────────────
        if price <= stop_price:
            exit_reason = "hard_stop"
            exit_detail = f"price={price:.2f} <= stop={stop_price:.2f}"
            logger.info("EXIT A-1 HARD_STOP | %s | %s | pnl=%.2f%%",
                        symbol, exit_detail, pnl_pct)

        # ── EXIT A-2: MA stack collapse ───────────────────────────────────────
        elif not stack_ok and days > hold_target * 0.5:
            # EMA8 < EMA21: structural momentum support has failed
            # Only after first half of hold_target to filter early-period noise
            exit_reason = "ma_stack_collapse"
            exit_detail = f"EMA8={e8:.2f} < EMA21={e21:.2f} | days={days}/{hold_target}"
            logger.info("EXIT A-2 MA_STACK_COLLAPSE | %s | %s | pnl=%.2f%%",
                        symbol, exit_detail, pnl_pct)

        # ── EXIT A-3: Trend exhaustion (ADX rollover) ─────────────────────────
        elif (adx_now > A_ADX_MIN_FOR_EXHAUSTION and
              adx_change < -A_ADX_DECLINE_THRESHOLD and
              pnl_pct > 0):
            # ADX was high and is now rolling over — trend losing steam
            # Only exit if profitable: no reason to take an ADX-noise loss
            exit_reason = "trend_exhaustion"
            exit_detail = (f"ADX={adx_now:.1f} declined {adx_change:.1f} over 3 bars "
                           f"from >{A_ADX_MIN_FOR_EXHAUSTION}")
            logger.info("EXIT A-3 TREND_EXHAUSTION | %s | %s | pnl=%.2f%%",
                        symbol, exit_detail, pnl_pct)

        # ── EXIT A-4: Momentum break ──────────────────────────────────────────
        elif roc5_val < A_ROC5_NEGATIVE_THRESHOLD and pnl_pct > A_MOMENTUM_BREAK_MIN_PNL * 100:
            # Short-term momentum has reversed while we're in meaningful profit
            exit_reason = "momentum_break"
            exit_detail = f"ROC5={roc5_val:.4f} negative | pnl={pnl_pct:.2f}%"
            logger.info("EXIT A-4 MOMENTUM_BREAK | %s | %s",
                        symbol, exit_detail)

        # ── EXIT A-5: Trail stop ──────────────────────────────────────────────
        elif price > entry_price + A_TRAIL_ACTIVATION_ATR * atr:
            # Trail only activates once position has a 1 ATR cushion above entry
            if price <= trail_stop:
                exit_reason = "trail_stop"
                exit_detail = (f"price={price:.2f} <= trail={trail_stop:.2f} "
                               f"(hw={hw:.2f} - {trail_atr:.2f}×ATR)")
                logger.info("EXIT A-5 TRAIL_STOP | %s | %s | pnl=%.2f%%",
                            symbol, exit_detail, pnl_pct)

        # ── EXIT A-6: Time stop ───────────────────────────────────────────────
        elif days >= hold_target:
            exit_reason = "time_stop"
            exit_detail = f"days={days} >= hold_target={hold_target}"
            logger.info("EXIT A-6 TIME_STOP | %s | %s | pnl=%.2f%%",
                        symbol, exit_detail, pnl_pct)

        if exit_reason:
            order = submit_sell(
                symbol, shares, exit_reason,
                alpaca_key, alpaca_secret, base_url,
                dry_run=dry_run
            )
            if order or dry_run:
                exit_date = datetime.now().strftime("%Y-%m-%d")
                if not dry_run:
                    ledger.record_exit(ENGINE_ID, symbol, price, exit_date, exit_reason)

                exits_executed.append({
                    "symbol":      symbol,
                    "exit_reason": exit_reason,
                    "exit_detail": exit_detail,
                    "price":       price,
                    "pnl_pct":     pnl_pct,
                    "days_held":   days,
                })

    return exits_executed
