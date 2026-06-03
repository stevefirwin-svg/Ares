"""
exit_monitor_f.py — Engine F Exit Logic
=========================================
Engine F exits are structure-based. The thesis is: breakout from consolidation
leads to a directional move. The exits define when that thesis is wrong or complete.

Exit conditions (checked in priority order):

  EXIT F-1: BASE VIOLATED (Hard stop — structural)
    price <= stop_price (stored in ledger metadata)
    stop_price = consol_low - 0.10×ATR (structural, not ATR-based stop)
    Price falling back into the base = false breakout. Exit immediately.
    This is the ONLY Engine F stop. It is structural — never ATR-based.

  EXIT F-2: BREAKOUT SUPPORT FAILED
    price < breakout_level AND days_held > 3 AND pnl < 0
    Price re-tested the breakout level and failed to hold it.
    The breakout level should become support — if it doesn't, thesis is weakening.
    3-day grace allows for normal post-breakout consolidation.

  EXIT F-3: MEASURED MOVE TRIM (partial exit — not a full exit)
    price >= measured_target × 0.98
    Trim 40% of shares at the measured move target (Edwards & Magee).
    The remaining 60% continues under trail logic (EXIT F-4).
    Only fires once per trade (measured_move_trimmed flag in metadata).
    NOTE: This is a TRIM, not a full exit. Records partial_exit in ledger.

  EXIT F-4: TRAIL STOP
    Activates once position is in profit (price > entry_price).
    trail_stop = high_water - (2.2 × trail_scale × ATR)
    trail_scale from entry metadata = 1.1 → effective trail = 2.42 ATR
    Slightly wider than composite — breakouts need room to pull back and run again.

  EXIT F-5: TIME STOP
    days_held >= hold_target_days (range 10–20, score-derived at entry)
    If measured move not achieved in time → reassess. Prevents dead-money.

Usage:
    from exit_monitor_f import check_engine_f_exits
    exits = check_engine_f_exits(positions, bars_map, ledger, alpaca_key,
                                  alpaca_secret, base_url, dry_run=False)

Not run standalone — called from ares_exit_monitor.py engine dispatch.
"""

import math
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ledger import Ledger

logger = logging.getLogger("ares.exit_f")

ENGINE_ID = "F"

# Trail parameters for Engine F
F_TRAIL_BASE_ATR = 2.2   # base ATR multiplier for trail
                          # × trail_scale (1.1) = 2.42 ATR effective
                          # Wider than Engine B, slightly wider than A — breakouts pull back

# Measured move trim fraction (40% of shares)
F_TRIM_FRACTION = 0.40


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


def _high_water(df: pd.DataFrame, entry_date_str: str, entry_price: float) -> float:
    """Max close since entry date. Falls back to max(current, entry)."""
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
    client_order_id = f"F_{reason}_{symbol}_{datetime.now():%Y%m%d%H%M%S}"
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
        logger.info("SELL ORDER F | %s | qty=%d | reason=%s | order_id=%s",
                    symbol, qty, reason, order.get("id", "?")[:8])
        return order
    except Exception as e:
        logger.error("Sell order failed for %s: %s", symbol, e)
        return None


# ── Exit logic ────────────────────────────────────────────────────────────────

def check_engine_f_exits(
    positions:     List[dict],
    bars_map:      Dict[str, pd.DataFrame],
    ledger:        Ledger,
    alpaca_key:    str,
    alpaca_secret: str,
    base_url:      str,
    dry_run:       bool = False,
) -> List[dict]:
    """
    Check all open Engine F positions against exit conditions.

    Returns list of exit/trim records [{symbol, exit_reason, price, pnl_pct}, ...]
    """
    exits_executed = []

    for pos in positions:
        symbol      = pos.get("symbol")
        entry_price = float(pos.get("entry_price", 0))
        shares      = pos.get("shares", 0)
        meta        = pos.get("metadata", {})
        entry_date  = pos.get("entry_date", "")

        if not symbol or entry_price <= 0 or shares <= 0:
            continue

        df = bars_map.get(symbol)
        if df is None or len(df) < 5:
            logger.warning("EXIT F: no bars for %s — skipping", symbol)
            continue

        price       = float(df["close"].iloc[-1])
        atr         = _atr(df)
        days        = _days_held(entry_date)
        hold_target = int(meta.get("hold_target_days", 15))
        trail_scale = float(meta.get("trail_scale", 1.1))

        # Structural stop — pulled from entry metadata
        stop_price = meta.get("stop_price")
        if stop_price is None:
            # Fallback: should never happen if entry recorded correctly
            logger.warning("EXIT F: %s missing stop_price in metadata — using 3ATR fallback", symbol)
            stop_price = entry_price - 3.0 * atr
        else:
            stop_price = float(stop_price)

        # Breakout geometry from entry metadata
        breakout_level        = meta.get("breakout_level")
        measured_target       = meta.get("measured_target")
        measured_move_trimmed = bool(meta.get("measured_move_trimmed", False))

        # High water mark and trail
        hw         = _high_water(df, entry_date, entry_price)
        trail_atr  = F_TRAIL_BASE_ATR * trail_scale
        trail_stop = hw - (trail_atr * atr)

        pnl_pct = round(((price / entry_price) - 1) * 100, 4) if entry_price else 0.0

        exit_reason = None
        exit_detail = ""
        trim_qty    = 0

        # ── EXIT F-1: Base violated ───────────────────────────────────────────
        if price <= stop_price:
            exit_reason = "breakout_failed"
            exit_detail = (f"price={price:.2f} <= structural_stop={stop_price:.2f} "
                           f"(consol_low={meta.get('consol_low', '?')})")
            logger.info("EXIT F-1 BASE_VIOLATED | %s | %s | pnl=%.2f%%",
                        symbol, exit_detail, pnl_pct)

        # ── EXIT F-2: Breakout support failed ────────────────────────────────
        elif (breakout_level is not None and
              days > 3 and
              price < float(breakout_level) and
              pnl_pct < 0):
            exit_reason = "breakout_support_failed"
            exit_detail = (f"price={price:.2f} < breakout_level={float(breakout_level):.2f} "
                           f"after {days}d | pnl={pnl_pct:.2f}%")
            logger.info("EXIT F-2 SUPPORT_FAILED | %s | %s", symbol, exit_detail)

        # ── EXIT F-3: Measured move trim (partial — 40% of shares) ───────────
        elif (measured_target is not None and
              not measured_move_trimmed and
              price >= float(measured_target) * 0.98):
            trim_qty = max(1, int(shares * F_TRIM_FRACTION))
            exit_reason = "measured_move_trim"
            exit_detail = (f"price={price:.2f} reached target={float(measured_target):.2f} "
                           f"trim={trim_qty}/{shares} shares ({F_TRIM_FRACTION:.0%})")
            logger.info("EXIT F-3 MEASURED_MOVE_TRIM | %s | %s | pnl=%.2f%%",
                        symbol, exit_detail, pnl_pct)

        # ── EXIT F-4: Trail stop ──────────────────────────────────────────────
        elif price > entry_price and price <= trail_stop:
            exit_reason = "trail_stop_F"
            exit_detail = (f"price={price:.2f} <= trail={trail_stop:.2f} "
                           f"(hw={hw:.2f} - {trail_atr:.2f}×ATR)")
            logger.info("EXIT F-4 TRAIL_STOP | %s | %s | pnl=%.2f%%",
                        symbol, exit_detail, pnl_pct)

        # ── EXIT F-5: Time stop ───────────────────────────────────────────────
        elif days >= hold_target:
            exit_reason = "time_stop"
            exit_detail = f"days={days} >= hold_target={hold_target}"
            logger.info("EXIT F-5 TIME_STOP | %s | %s | pnl=%.2f%%",
                        symbol, exit_detail, pnl_pct)

        if exit_reason:
            # For measured move trim: sell trim_qty, not all shares
            sell_qty = trim_qty if exit_reason == "measured_move_trim" else shares

            order = submit_sell(
                symbol, sell_qty, exit_reason,
                alpaca_key, alpaca_secret, base_url,
                dry_run=dry_run
            )
            if order or dry_run:
                exit_date = datetime.now().strftime("%Y-%m-%d")
                if not dry_run:
                    if exit_reason == "measured_move_trim":
                        # Partial exit: reduce shares, keep position open, flag trimmed
                        ledger.record_trim(ENGINE_ID, symbol, sell_qty, price,
                                           exit_date, exit_reason)
                        ledger.update_metadata(ENGINE_ID, symbol,
                                               {"measured_move_trimmed": True})
                    else:
                        ledger.record_exit(ENGINE_ID, symbol, price, exit_date, exit_reason)

                exits_executed.append({
                    "symbol":      symbol,
                    "exit_reason": exit_reason,
                    "exit_detail": exit_detail,
                    "price":       price,
                    "pnl_pct":     pnl_pct,
                    "days_held":   days,
                    "qty_sold":    sell_qty,
                    "shares_total": shares,
                })

    return exits_executed
