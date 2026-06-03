"""
exit_monitor_b.py — Engine B Exit Logic
=========================================
Engine B exits are OU-thesis driven, not generic trailing stops.

Exit conditions (checked in priority order):

  EXIT B-1: HARD STOP
    price <= stop_price stored in ledger metadata
    Thesis violated — price moved further against us, not reverting.

  EXIT B-2: AVWAP REACHED (Primary target)
    price >= AVWAP (rolling 20d)
    Reversion thesis complete — price returned to equilibrium.
    This is the main profit-taking exit for Engine B.

  EXIT B-3: TRAIL STOP (Secondary)
    Once price has risen > 0.5 ATR from entry, trail at entry + 0.25 ATR.
    Locks in partial gain if AVWAP target is not cleanly reached.
    Prevents giving back a reversion move.

  EXIT B-4: TIME STOP
    Days held >= hold_target_days (from OU half-life calculation)
    Thesis has had its time; if not reverted, exit regardless.
    Prevents dead-money positions.

  EXIT B-5: THESIS INVALID
    b_volume_climax on current bar >= 2.5 AND price direction is further against us.
    Another capitulation event below our entry — the selling is escalating,
    not exhausting. OU equilibrium may have shifted. Exit and reassess.

Usage:
    from exit_monitor_b import check_engine_b_exits
    exits = check_engine_b_exits(positions, bars_map, dry_run=False)

Not run standalone — called from exit_monitor.py engine dispatch.
"""

import json
import logging
import math
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ares_config import ENGINE_STOP_PARAMS
from ledger import Ledger

logger = logging.getLogger("ares.exit_b")

ENGINE_ID = "B"

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


def _volume_climax(df: pd.DataFrame, lookback: int = 20) -> float:
    if len(df) < lookback + 1:
        return 0.0
    avg_vol = float(df["volume"].iloc[-lookback-1:-1].mean())
    cur_vol = float(df["volume"].iloc[-1])
    return (cur_vol / avg_vol) if avg_vol > 0 else 0.0


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
    client_order_id = f"B_{reason}_{symbol}_{datetime.now():%Y%m%d%H%M%S}"
    body = {
        "symbol":           symbol,
        "qty":              str(qty),
        "side":             "sell",
        "type":             "market",
        "time_in_force":    "day",
        "client_order_id":  client_order_id,
    }
    try:
        resp = req.post(f"{base_url}/v2/orders", headers=headers, json=body, timeout=10)
        resp.raise_for_status()
        order = resp.json()
        logger.info("SELL ORDER B | %s | qty=%d | reason=%s | order_id=%s",
                    symbol, qty, reason, order.get("id", "?")[:8])
        return order
    except Exception as e:
        logger.error("Sell order failed for %s: %s", symbol, e)
        return None


# ── Exit logic ────────────────────────────────────────────────────────────────

def check_engine_b_exits(
    positions: List[dict],
    bars_map:  Dict[str, pd.DataFrame],
    ledger:    Ledger,
    alpaca_key: str,
    alpaca_secret: str,
    base_url: str,
    dry_run: bool = False,
) -> List[dict]:
    """
    Check all open Engine B positions against exit conditions.

    Args:
        positions    — list of open position dicts from ledger.get_positions("B")
        bars_map     — {symbol: DataFrame} from bar fetch
        ledger       — Ares Ledger instance
        dry_run      — if True, log exits but don't submit orders

    Returns list of exit records [{symbol, exit_reason, price, pnl_pct}, ...]
    """
    exits_executed = []
    stop_params    = ENGINE_STOP_PARAMS[ENGINE_ID]

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
            logger.warning("EXIT B: no bars for %s — skipping", symbol)
            continue

        price        = float(df["close"].iloc[-1])
        atr          = _atr(df)
        avwap        = _compute_avwap(df)
        days         = _days_held(entry_date)
        vol_climax   = _volume_climax(df)

        # Pull stop_price and hold_target from entry metadata
        stop_price_stored = meta.get("stop_price")
        hold_target       = meta.get("hold_target_days", 10)

        # Fallback stop if metadata missing
        if stop_price_stored is None:
            stop_mult  = stop_params.get("stop_mult_low_vol", 2.0)
            stop_price = entry_price - stop_mult * atr
        else:
            stop_price = float(stop_price_stored)

        pnl_pct = round(((price / entry_price) - 1) * 100, 4) if entry_price else 0

        exit_reason = None

        # ── EXIT B-1: Hard stop ───────────────────────────────────────────────
        if price <= stop_price:
            exit_reason = "hard_stop"
            logger.info("EXIT B-1 HARD_STOP | %s | price=%.2f <= stop=%.2f | pnl=%.2f%%",
                        symbol, price, stop_price, pnl_pct)

        # ── EXIT B-2: AVWAP reached ───────────────────────────────────────────
        elif price >= avwap:
            exit_reason = "avwap_reached"
            logger.info("EXIT B-2 AVWAP_REACHED | %s | price=%.2f >= avwap=%.2f | pnl=%.2f%%",
                        symbol, price, avwap, pnl_pct)

        # ── EXIT B-3: Trail stop (once in profit) ─────────────────────────────
        elif price > entry_price + 0.5 * atr:
            trail_price = entry_price + 0.25 * atr
            if price <= trail_price:
                exit_reason = "trail_stop"
                logger.info("EXIT B-3 TRAIL_STOP | %s | price=%.2f <= trail=%.2f | pnl=%.2f%%",
                            symbol, price, trail_price, pnl_pct)

        # ── EXIT B-4: Time stop ───────────────────────────────────────────────
        elif days >= hold_target:
            exit_reason = "time_stop"
            logger.info("EXIT B-4 TIME_STOP | %s | days=%d >= target=%d | pnl=%.2f%%",
                        symbol, days, hold_target, pnl_pct)

        # ── EXIT B-5: Thesis invalid (escalating selling) ─────────────────────
        elif vol_climax >= 2.5 and price < entry_price:
            # New capitulation event below entry — OU equilibrium may have shifted
            exit_reason = "thesis_invalid"
            logger.info("EXIT B-5 THESIS_INVALID | %s | vol_climax=%.2f >= 2.5 "
                        "AND price=%.2f < entry=%.2f | pnl=%.2f%%",
                        symbol, vol_climax, price, entry_price, pnl_pct)

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
                    "price":       price,
                    "pnl_pct":     pnl_pct,
                    "days_held":   days,
                })

    return exits_executed
