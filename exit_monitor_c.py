"""
exit_monitor_c.py — Engine C Exit Logic
=========================================
Squeeze exits are fast and decisive. If the squeeze doesn't follow through
in 3 bars, it's not working. No patience for stalled squeezes.

Exit conditions:

  EXIT C-1: HARD STOP
    price <= stop_price (1.5× ATR below entry)
    Wrong direction immediately. Exit.

  EXIT C-2: WRONG DIRECTION (phase check)
    After 1 bar: if close < entry_price (price went down, not up)
    Squeeze fired bearishly — exit immediately.
    Engine C only goes long. Wrong-direction squeeze = instant exit.

  EXIT C-3: MOMENTUM STALL
    TTM momentum histogram turns negative within hold window.
    Compression resolved bearishly — exit regardless of P&L.

  EXIT C-4: TIME STOP
    days_held >= max_hold_days (8 bars maximum, hard cap)
    The squeeze edge expires in 3-8 bars. After that, we're holding a normal trade.

Usage:
    from exit_monitor_c import check_engine_c_exits
"""

import logging
import math
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ares_config import ENGINE_STOP_PARAMS
from ledger import Ledger

logger = logging.getLogger("ares.exit_c")
ENGINE_ID = "C"

BB_PERIOD = 20
BB_MULT   = 2.0
KC_PERIOD = 20
KC_MULT   = 1.5


def _atr(df, period=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h-l, (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if not math.isnan(val) else 0.0


def _ttm_momentum(df):
    """TTM momentum oscillator value (last bar)."""
    if len(df) < BB_PERIOD + 2:
        return 0.0
    bb_mid  = df["close"].rolling(BB_PERIOD).mean()
    highest = df["high"].rolling(BB_PERIOD).max()
    lowest  = df["low"].rolling(BB_PERIOD).min()
    mid_hl  = (highest + lowest) / 2
    mom     = df["close"] - (mid_hl + bb_mid) / 2
    return float(mom.iloc[-1])


def _days_held(entry_date_str):
    try:
        entry = datetime.strptime(str(entry_date_str)[:10], "%Y-%m-%d").date()
        return (datetime.now().date() - entry).days
    except Exception:
        return 0


def submit_sell(symbol, qty, reason, alpaca_key, alpaca_secret, base_url, dry_run=False):
    if dry_run:
        logger.info("[DRY] Would sell %d %s — %s", qty, symbol, reason)
        return {"id": "dry-run"}
    import requests as req
    headers = {
        "APCA-API-KEY-ID":     alpaca_key,
        "APCA-API-SECRET-KEY": alpaca_secret,
        "Content-Type":        "application/json",
    }
    body = {
        "symbol":          symbol,
        "qty":             str(qty),
        "side":            "sell",
        "type":            "market",
        "time_in_force":   "day",
        "client_order_id": f"C_{reason}_{symbol}_{datetime.now():%Y%m%d%H%M%S}",
    }
    try:
        resp = req.post(f"{base_url}/v2/orders", headers=headers, json=body, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Sell failed for %s: %s", symbol, e)
        return None


def check_engine_c_exits(positions, bars_map, ledger, alpaca_key, alpaca_secret,
                          base_url, dry_run=False):
    exits_executed = []

    for pos in positions:
        symbol      = pos.get("symbol")
        entry_price = pos.get("entry_price", 0)
        shares      = pos.get("shares", 0)
        meta        = pos.get("metadata", {})
        entry_date  = pos.get("entry_date", "")

        if not symbol or entry_price <= 0 or shares <= 0:
            continue

        df = bars_map.get(symbol)
        if df is None or len(df) < 5:
            continue

        price       = float(df["close"].iloc[-1])
        atr         = _atr(df)
        days        = _days_held(entry_date)
        stop_price  = float(meta.get("stop_price", entry_price - 1.5 * atr))
        max_hold    = int(meta.get("max_hold_days", 8))
        mom_now     = _ttm_momentum(df)
        pnl_pct     = round(((price / entry_price) - 1) * 100, 4)
        exit_reason = None

        # EXIT C-1: Hard stop
        if price <= stop_price:
            exit_reason = "hard_stop"
            logger.info("EXIT C-1 HARD_STOP | %s | %.2f <= %.2f | pnl=%.2f%%",
                        symbol, price, stop_price, pnl_pct)

        # EXIT C-2: Wrong direction (1+ bars held, price below entry)
        elif days >= 1 and price < entry_price * 0.995:
            exit_reason = "wrong_direction"
            logger.info("EXIT C-2 WRONG_DIRECTION | %s | price=%.2f < entry=%.2f | days=%d",
                        symbol, price, entry_price, days)

        # EXIT C-3: Momentum stall
        elif mom_now < 0 and days >= 1:
            exit_reason = "momentum_stall"
            logger.info("EXIT C-3 MOMENTUM_STALL | %s | mom=%.4f < 0 | pnl=%.2f%%",
                        symbol, mom_now, pnl_pct)

        # EXIT C-4: Time stop
        elif days >= max_hold:
            exit_reason = "time_stop"
            logger.info("EXIT C-4 TIME_STOP | %s | days=%d >= %d | pnl=%.2f%%",
                        symbol, days, max_hold, pnl_pct)

        if exit_reason:
            order = submit_sell(symbol, shares, exit_reason,
                                alpaca_key, alpaca_secret, base_url, dry_run=dry_run)
            if order or dry_run:
                if not dry_run:
                    ledger.record_exit(ENGINE_ID, symbol, price,
                                       datetime.now().strftime("%Y-%m-%d"), exit_reason)
                exits_executed.append({
                    "symbol":      symbol,
                    "engine_id":   ENGINE_ID,
                    "exit_reason": exit_reason,
                    "price":       price,
                    "pnl_pct":     pnl_pct,
                    "days_held":   days,
                })

    return exits_executed
