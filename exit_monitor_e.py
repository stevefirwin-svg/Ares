"""
exit_monitor_e.py — Engine E Exit Logic
=========================================
RS rotation exits are RS-driven, not price-driven primarily.
The thesis is capital rotation — exit when the rotation reverses.

Exit conditions:

  EXIT E-1: HARD STOP
    price <= stop_price (3.0× ATR below entry)
    Price-based hard protection.

  EXIT E-2: RS LOST VS SECTOR
    Stock 10d RS vs sector ETF turns negative.
    The rotation that drove entry has reversed. Primary exit for Engine E.
    RS_lost = stock_return_10d < sector_return_10d - 0.01 (1% underperformance tolerance)

  EXIT E-3: SECTOR ROTATION REVERSAL
    Sector 10d RS vs SPY turns negative.
    Capital leaving the sector. Stock will follow sector lower.

  EXIT E-4: TRAIL STOP
    Once price > entry + 1.5×ATR, trail at entry + 0.5×ATR.
    Wider trail than Engine A — RS trades need room to breathe.

  EXIT E-5: TIME STOP
    days_held >= hold_target_days (20 days).
    IC horizon = 20 days for Engine E.
"""

import logging
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

from ares_config import ENGINE_STOP_PARAMS, ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL, BAR_FEED
from ledger import Ledger

logger = logging.getLogger("ares.exit_e")
ENGINE_ID = "E"


def _atr(df, period=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h-l, (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if not math.isnan(val) else 0.0


def _return_n(df, n):
    if len(df) < n + 1:
        return 0.0
    p_now  = float(df["close"].iloc[-1])
    p_then = float(df["close"].iloc[-n-1])
    return (p_now / p_then) - 1.0 if p_then > 0 else 0.0


def _days_held(entry_date_str):
    try:
        entry = datetime.strptime(str(entry_date_str)[:10], "%Y-%m-%d").date()
        return (datetime.now().date() - entry).days
    except Exception:
        return 0


def _fetch_sector_and_spy(sector_etf: str) -> tuple:
    """Fetch bars for sector ETF and SPY for RS computation."""
    import requests as req
    from datetime import timedelta
    symbols = [sector_etf, "SPY"]
    end   = datetime.now()
    start = end - timedelta(days=30)
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    params = {
        "symbols":   ",".join(symbols),
        "timeframe": "1Day",
        "start":     start.strftime("%Y-%m-%d"),
        "end":       end.strftime("%Y-%m-%d"),
        "limit":     1000,
        "feed":      BAR_FEED,
    }
    try:
        resp = req.get("https://data.alpaca.markets/v2/stocks/bars",
                       headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("bars", {})
        dfs  = {}
        for sym, bars in data.items():
            if bars:
                df = pd.DataFrame(bars)
                df.rename(columns={"o":"open","h":"high","l":"low","c":"close","v":"volume"}, inplace=True)
                dfs[sym] = df
        return dfs.get(sector_etf), dfs.get("SPY")
    except Exception as e:
        logger.warning("Sector/SPY fetch failed: %s", e)
        return None, None


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
        "client_order_id": f"E_{reason}_{symbol}_{datetime.now():%Y%m%d%H%M%S}",
    }
    try:
        resp = req.post(f"{base_url}/v2/orders", headers=headers, json=body, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Sell failed for %s: %s", symbol, e)
        return None


def check_engine_e_exits(positions, bars_map, ledger, alpaca_key, alpaca_secret,
                          base_url, dry_run=False):
    exits_executed = []
    stop_params    = ENGINE_STOP_PARAMS.get(ENGINE_ID, {})

    # Cache sector/SPY bars to avoid fetching multiple times per sector
    sector_cache = {}

    for pos in positions:
        symbol      = pos.get("symbol")
        entry_price = pos.get("entry_price", 0)
        shares      = pos.get("shares", 0)
        meta        = pos.get("metadata", {})
        entry_date  = pos.get("entry_date", "")

        if not symbol or entry_price <= 0 or shares <= 0:
            continue

        stock_df = bars_map.get(symbol)
        if stock_df is None or len(stock_df) < 12:
            continue

        price       = float(stock_df["close"].iloc[-1])
        atr         = _atr(stock_df)
        days        = _days_held(entry_date)
        stop_price  = float(meta.get("stop_price", entry_price - 3.0 * atr))
        hold_target = int(meta.get("hold_target_days", 20))
        sector_etf  = meta.get("sector_etf", "XLK")
        trail_high  = float(meta.get("trail_high", entry_price))

        # Fetch sector/SPY bars (cached)
        if sector_etf not in sector_cache:
            sec_df, spy_df = _fetch_sector_and_spy(sector_etf)
            sector_cache[sector_etf] = (sec_df, spy_df)
        else:
            sec_df, spy_df = sector_cache[sector_etf]

        # Update trail high
        if price > trail_high:
            meta["trail_high"] = price
            trail_high = price

        trail_price   = entry_price + 0.5 * atr
        in_profit     = price > entry_price + 1.5 * atr
        pnl_pct       = round(((price / entry_price) - 1) * 100, 4)
        exit_reason   = None

        # EXIT E-1: Hard stop
        if price <= stop_price:
            exit_reason = "hard_stop"
            logger.info("EXIT E-1 | %s | %.2f <= %.2f | pnl=%.2f%%",
                        symbol, price, stop_price, pnl_pct)

        # EXIT E-2: RS lost vs sector
        elif sec_df is not None and len(sec_df) >= 12:
            stock_ret10  = _return_n(stock_df, 10)
            sector_ret10 = _return_n(sec_df, 10)
            rs_lost      = stock_ret10 < sector_ret10 - 0.01
            if rs_lost:
                exit_reason = "rs_lost"
                logger.info("EXIT E-2 RS_LOST | %s | stock10=%.3f < sector10=%.3f | pnl=%.2f%%",
                            symbol, stock_ret10, sector_ret10, pnl_pct)

        # EXIT E-3: Sector rotation reversal
        if not exit_reason and sec_df is not None and spy_df is not None:
            if len(sec_df) >= 12 and len(spy_df) >= 12:
                sec_ret10  = _return_n(sec_df, 10)
                spy_ret10  = _return_n(spy_df, 10)
                sec_losing = sec_ret10 < spy_ret10 - 0.01
                if sec_losing:
                    exit_reason = "sector_rotation"
                    logger.info("EXIT E-3 SECTOR_ROTATION | %s [%s] | sec=%.3f < spy=%.3f | pnl=%.2f%%",
                                symbol, sector_etf, sec_ret10, spy_ret10, pnl_pct)

        # EXIT E-4: Trail stop
        if not exit_reason and in_profit and price <= trail_price:
            exit_reason = "trail_stop"
            logger.info("EXIT E-4 TRAIL | %s | %.2f <= trail=%.2f | pnl=%.2f%%",
                        symbol, price, trail_price, pnl_pct)

        # EXIT E-5: Time stop
        if not exit_reason and days >= hold_target:
            exit_reason = "time_stop"
            logger.info("EXIT E-5 TIME | %s | days=%d >= %d | pnl=%.2f%%",
                        symbol, days, hold_target, pnl_pct)

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
