"""
ares_watchdog.py — Ares Intraday Position Watchdog
====================================================
Runs during market hours to catch intraday moves the morning scan misses.
Checks LIVE prices from Alpaca positions endpoint every 15 minutes.

What this catches (intraday only):
  - Hard stop breach (price <= stop_price in metadata)
  - Portfolio-level circuit breaker (SPY down > 3% intraday)
  - Extreme single-position loss (> 8% intraday from entry)

What this does NOT do (leave to ares_exit_monitor.py):
  - Thesis invalidation (requires composite re-score — daily bars only)
  - Momentum break / OU health decay (multi-day signals)
  - Measured move trims (Engine F — daily only)
  - Time decay exits

Run:
  python ares_watchdog.py              # one check cycle
  python ares_watchdog.py --loop       # continuous loop every 15 min during market hours
  python ares_watchdog.py --dry-run    # check but do not submit orders
  python ares_watchdog.py --status     # print current live prices vs stops

Schedule via Task Scheduler:
  Every 15 minutes, 9:45 AM – 3:55 PM ET, weekdays only
  (Morning scan + exit monitor run first at 9:35/9:55 — watchdog starts after)

Why separate from ares_exit_monitor.py:
  Exit monitor uses daily bar data. Watchdog uses live broker prices.
  A hard stop breach can happen at 11 AM on a gap-down — the exit monitor
  won't catch it until 4 PM. Watchdog fills that gap.
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests

from ares_config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    ENGINE_STATUS,
    MAX_PORTFOLIO_DRAWDOWN,
)
from ledger import Ledger
from dotenv import load_dotenv
load_dotenv()

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Watchdog] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"logs/watchdog_{datetime.now():%Y%m%d}.log"),
    ],
)
logger = logging.getLogger("ares.watchdog")

# ── Constants ─────────────────────────────────────────────────────────────────

WATCHDOG_INTERVAL_SECONDS = 900     # 15 minutes between checks
SPY_CIRCUIT_BREAKER       = -0.03   # Exit all if SPY is down > 3% intraday
                                     # TODO:DERIVE from regime-conditional tail analysis
INTRADAY_LOSS_HARD_EXIT   = -0.08   # Exit if any position is down > 8% intraday
                                     # TODO:DERIVE from EVT on per-position intraday drawdowns
MARKET_OPEN_ET            = (9, 30)
MARKET_CLOSE_ET           = (16, 0)
WATCHDOG_START_ET         = (9, 45)  # Start after morning scan + exit monitor
WATCHDOG_END_ET           = (15, 55) # Stop before close


# ── Alpaca helpers ────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }


def get_live_positions() -> List[dict]:
    """Fetch live positions from Alpaca broker (NOT from ledger)."""
    try:
        resp = requests.get(
            f"{ALPACA_BASE_URL}/v2/positions",
            headers=_headers(), timeout=10
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("Failed to fetch live positions: %s", e)
        return []


def get_live_price(symbol: str) -> Optional[float]:
    """Get latest trade price for a symbol."""
    try:
        resp = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest",
            headers=_headers(), timeout=8
        )
        resp.raise_for_status()
        data = resp.json()
        return float(data["trade"]["p"])
    except Exception:
        return None


def get_spy_intraday_change() -> Optional[float]:
    """
    SPY intraday change vs prior close.
    Returns decimal (e.g. -0.032 = down 3.2%).
    """
    try:
        # Get today's bars
        today = datetime.now().strftime("%Y-%m-%d")
        resp = requests.get(
            "https://data.alpaca.markets/v2/stocks/SPY/bars",
            headers=_headers(),
            params={
                "timeframe": "1Day",
                "start":     (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d"),
                "end":       today,
                "limit":     5,
                "feed":      "iex",
            },
            timeout=10
        )
        resp.raise_for_status()
        bars = resp.json().get("bars", [])
        if len(bars) < 2:
            return None

        # Prior close
        prior_close = float(bars[-2]["c"])

        # Live SPY price
        live = get_live_price("SPY")
        if live is None or prior_close <= 0:
            return None

        return (live / prior_close) - 1.0

    except Exception as e:
        logger.warning("SPY intraday check failed: %s", e)
        return None


def submit_market_sell(symbol: str, qty: int, reason: str, dry_run: bool = False) -> bool:
    """Submit a market sell order. Returns True if submitted (or dry_run)."""
    if dry_run:
        logger.info("[DRY RUN] Would sell %d %s — reason: %s", qty, symbol, reason)
        return True

    client_id = f"WATCHDOG-{symbol}-{reason[:10]}-{datetime.now():%H%M%S}"
    try:
        payload = {
            "symbol":        symbol,
            "qty":           str(qty),
            "side":          "sell",
            "type":          "market",
            "time_in_force": "day",
            "client_order_id": client_id,
        }
        resp = requests.post(
            f"{ALPACA_BASE_URL}/v2/orders",
            headers=_headers(), json=payload, timeout=10
        )
        resp.raise_for_status()
        order_id = resp.json().get("id", "?")
        logger.info("SELL SUBMITTED | %s | qty=%d | reason=%s | order_id=%s",
                    symbol, qty, reason, order_id)
        return True
    except Exception as e:
        logger.error("Order submission failed for %s: %s", symbol, e)
        return False


# ── Market hours check ────────────────────────────────────────────────────────

def is_watchdog_active() -> bool:
    """True if current ET time is within watchdog operating window."""
    now_utc = datetime.utcnow()
    # ET = UTC - 4 (EDT) or UTC - 5 (EST) — approximate with UTC-4 during summer
    # TODO: use pytz or zoneinfo for DST-correct ET
    et_offset = 4  # EDT
    now_et = now_utc - timedelta(hours=et_offset)

    h, m = now_et.hour, now_et.minute
    current_minutes = h * 60 + m
    start_minutes   = WATCHDOG_START_ET[0] * 60 + WATCHDOG_START_ET[1]
    end_minutes     = WATCHDOG_END_ET[0]   * 60 + WATCHDOG_END_ET[1]

    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False

    return start_minutes <= current_minutes <= end_minutes


# ── Core watchdog logic ───────────────────────────────────────────────────────

def run_check(dry_run: bool = False) -> dict:
    """
    Single watchdog cycle. Returns summary dict.

    Priority order:
      1. SPY circuit breaker — if triggered, exit ALL positions
      2. Per-position hard stop breach
      3. Per-position extreme intraday loss
    """
    logger.info("-" * 50)
    logger.info("Watchdog check — %s", datetime.now().strftime("%H:%M:%S"))

    summary = {
        "timestamp":         datetime.now().isoformat(),
        "exits_triggered":   [],
        "circuit_breaker":   False,
        "spy_change":        None,
        "positions_checked": 0,
        "errors":            [],
    }

    ledger = Ledger()

    # ── 1. SPY circuit breaker ────────────────────────────────────────────────
    spy_change = get_spy_intraday_change()
    summary["spy_change"] = round(spy_change, 4) if spy_change is not None else None

    if spy_change is not None and spy_change <= SPY_CIRCUIT_BREAKER:
        logger.warning("SPY CIRCUIT BREAKER: SPY %+.2f%% intraday — exiting ALL positions",
                       spy_change * 100)
        summary["circuit_breaker"] = True

        broker_positions = get_live_positions()
        for bp in broker_positions:
            sym = bp.get("symbol")
            qty = int(float(bp.get("qty", 0)))
            if sym and qty > 0:
                ok = submit_market_sell(sym, qty, "spy_circuit_breaker", dry_run)
                if ok:
                    summary["exits_triggered"].append({
                        "symbol":      sym,
                        "reason":      "spy_circuit_breaker",
                        "spy_change":  round(spy_change, 4),
                    })
                    # Update ledger if we submitted
                    if not dry_run:
                        try:
                            live_p = get_live_price(sym)
                            ledger.record_exit(sym, exit_price=live_p or 0,
                                               exit_date=datetime.now().strftime("%Y-%m-%d"),
                                               reason="spy_circuit_breaker")
                        except Exception:
                            pass

        logger.info("Circuit breaker exits: %d", len(summary["exits_triggered"]))
        return summary

    # ── 2. Per-position checks ────────────────────────────────────────────────
    # Get all Ares-managed positions from ledger
    all_positions = []
    for engine_id in ENGINE_STATUS:
        if ENGINE_STATUS[engine_id] == "off":
            continue
        engine_positions = ledger.get_positions(engine_id)
        for pos in engine_positions:
            pos["engine_id"] = engine_id
            all_positions.append(pos)

    if not all_positions:
        logger.info("No open Ares positions — nothing to monitor.")
        return summary

    summary["positions_checked"] = len(all_positions)
    logger.info("Checking %d position(s)...", len(all_positions))

    for pos in all_positions:
        symbol      = pos.get("symbol")
        engine_id   = pos.get("engine_id", "?")
        entry_price = float(pos.get("entry_price", 0))
        shares      = int(pos.get("shares", 0))
        meta        = pos.get("metadata", {})
        stop_price  = meta.get("stop_price")

        if not symbol or shares <= 0:
            continue

        # Get live price
        live_price = get_live_price(symbol)
        if live_price is None:
            logger.warning("[%s] %s — could not fetch live price", engine_id, symbol)
            summary["errors"].append(f"{symbol}: live price fetch failed")
            continue

        intraday_pnl = (live_price / entry_price) - 1.0 if entry_price > 0 else 0.0

        logger.info("[%s] %s | live=%.2f | entry=%.2f | pnl=%+.2f%% | stop=%s",
                    engine_id, symbol, live_price, entry_price,
                    intraday_pnl * 100,
                    f"{stop_price:.2f}" if stop_price else "N/A")

        exit_reason = None

        # Hard stop breach
        if stop_price is not None and live_price <= float(stop_price):
            exit_reason = "hard_stop_watchdog"
            logger.warning("[%s] HARD STOP BREACH: %s | live=%.2f <= stop=%.2f",
                           engine_id, symbol, live_price, float(stop_price))

        # Extreme intraday loss
        elif intraday_pnl <= INTRADAY_LOSS_HARD_EXIT:
            exit_reason = "extreme_intraday_loss_watchdog"
            logger.warning("[%s] EXTREME LOSS: %s | %+.2f%% intraday",
                           engine_id, symbol, intraday_pnl * 100)

        if exit_reason:
            ok = submit_market_sell(symbol, shares, exit_reason, dry_run)
            if ok:
                summary["exits_triggered"].append({
                    "symbol":      symbol,
                    "engine_id":   engine_id,
                    "reason":      exit_reason,
                    "live_price":  round(live_price, 4),
                    "entry_price": round(entry_price, 4),
                    "pnl_pct":     round(intraday_pnl * 100, 2),
                })
                if not dry_run:
                    try:
                        ledger.record_exit(
                            symbol,
                            exit_price=live_price,
                            exit_date=datetime.now().strftime("%Y-%m-%d"),
                            reason=exit_reason
                        )
                    except Exception as e:
                        logger.warning("Ledger exit record failed for %s: %s", symbol, e)

    n_exits = len(summary["exits_triggered"])
    if n_exits:
        logger.warning("Watchdog exits this cycle: %d", n_exits)
    else:
        logger.info("No exits triggered.")

    return summary


def print_status():
    """Print current positions with live prices vs stops."""
    ledger = Ledger()
    print(f"\n{'='*65}")
    print(f"  ARES WATCHDOG STATUS — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*65}")

    spy_change = get_spy_intraday_change()
    spy_str = f"{spy_change*100:+.2f}%" if spy_change is not None else "N/A"
    circuit = "⚠ CIRCUIT BREAKER ZONE" if spy_change and spy_change <= SPY_CIRCUIT_BREAKER else "OK"
    print(f"  SPY intraday: {spy_str}  [{circuit}]")
    print(f"  Circuit breaker threshold: {SPY_CIRCUIT_BREAKER*100:.1f}%")
    print()

    any_positions = False
    for engine_id in sorted(ENGINE_STATUS):
        if ENGINE_STATUS[engine_id] == "off":
            continue
        positions = ledger.get_positions(engine_id)
        for pos in positions:
            any_positions = True
            sym         = pos.get("symbol")
            entry_price = float(pos.get("entry_price", 0))
            shares      = int(pos.get("shares", 0))
            stop_price  = pos.get("metadata", {}).get("stop_price")
            live_price  = get_live_price(sym) if sym else None

            if live_price and entry_price > 0:
                pnl = (live_price / entry_price - 1) * 100
                at_risk = live_price <= float(stop_price) if stop_price else False
                flag = "⚠ AT STOP" if at_risk else ""
            else:
                pnl = 0.0
                flag = "(no live price)"

            print(f"  [{engine_id}] {sym:8s}  {shares} sh @ ${entry_price:.2f}"
                  f"  live=${live_price:.2f}" if live_price else f"  live=N/A"
                  f"  pnl={pnl:+.2f}%"
                  f"  stop={stop_price:.2f}" if stop_price else "  stop=N/A"
                  f"  {flag}")

    if not any_positions:
        print("  No open positions.")

    print(f"{'='*65}\n")


def run_loop(dry_run: bool = False):
    """Continuous loop — run watchdog every 15 min during market hours."""
    logger.info("Watchdog loop started. Interval: %ds. Press Ctrl+C to stop.",
                WATCHDOG_INTERVAL_SECONDS)

    while True:
        if is_watchdog_active():
            run_check(dry_run=dry_run)
        else:
            logger.info("Outside watchdog window — sleeping.")

        time.sleep(WATCHDOG_INTERVAL_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ares Intraday Watchdog")
    parser.add_argument("--loop",    action="store_true", help="Continuous 15-min loop")
    parser.add_argument("--dry-run", action="store_true", help="Check only, no orders")
    parser.add_argument("--status",  action="store_true", help="Print live prices vs stops")
    args = parser.parse_args()

    if args.status:
        print_status()
    elif args.loop:
        run_loop(dry_run=args.dry_run)
    else:
        run_check(dry_run=args.dry_run)
