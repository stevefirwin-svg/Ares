"""
ares_exit_monitor.py — Ares Exit Monitor (Engine-Dispatched)
=============================================================
Engine-aware exit monitor. Does NOT use signals.py or Raptor infrastructure.
Source of truth is the Ares ledger (ares_position_ledger.json), not broker positions.

Dispatch:
    Engine B → exit_monitor_b.check_engine_b_exits()
    Engine A → exit_monitor_a.check_engine_a_exits()
    Engine F → exit_monitor_f.check_engine_f_exits()
    Engine C → exit_monitor_c.check_engine_c_exits()
    Engine E → exit_monitor_e.check_engine_e_exits()

Run:
    python ares_exit_monitor.py            # execute exits
    python ares_exit_monitor.py --dry-run  # show what would exit, no orders
    python ares_exit_monitor.py --status   # print open positions by engine

Scheduled via Task Scheduler:
    9:55 AM ET  — morning exit check
    4:00 PM ET  — afternoon exit check (catch end-of-day stops)
"""

import json
import logging
import os
import sys
from datetime import datetime
from typing import Dict, List

import pandas as pd

from ares_config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    ENGINE_STATUS, BARS_LOOKBACK,
)
from ledger import Ledger
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AresExit] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"logs/ares_exits_{datetime.now():%Y%m%d}.log"),
    ],
)
logger = logging.getLogger("ares.exit_monitor")


# ── Bar fetch ─────────────────────────────────────────────────────────────────

def fetch_bars(symbols: List[str], lookback: int = BARS_LOOKBACK) -> Dict[str, pd.DataFrame]:
    """
    Fetch daily OHLCV bars via yfinance.

    Switched from Alpaca/IEX (which only covers ~2-3% of consolidated tape on
    paper accounts) to yfinance, which has full coverage for all symbols.
    This matches the approach used in all scan engines.
    Order submission remains on Alpaca — only bar data uses yfinance.
    """
    import yfinance as yf
    from datetime import timedelta

    if not symbols:
        return {}

    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(days=int(lookback * 1.6) + 10)

    bars_out = {}
    # yfinance download accepts a space-separated string for multiple tickers
    try:
        raw = yf.download(
            tickers=" ".join(symbols),
            start=start_dt.strftime("%Y-%m-%d"),
            end=end_dt.strftime("%Y-%m-%d"),
            interval="1d",
            auto_adjust=True,
            progress=False,
            group_by="ticker" if len(symbols) > 1 else None,
        )
        for sym in symbols:
            try:
                if len(symbols) == 1:
                    df = raw.copy()
                else:
                    df = raw[sym].copy() if sym in raw.columns.get_level_values(0) else pd.DataFrame()
                if df.empty or len(df) < 5:
                    continue
                df.columns = [c.lower() for c in df.columns]
                df = df.rename(columns={"adj close": "close"}) if "adj close" in df.columns else df
                df = df[["open", "high", "low", "close", "volume"]].dropna()
                df.index.name = "timestamp"
                df = df.tail(lookback)
                if len(df) >= 5:
                    bars_out[sym] = df
            except Exception as e:
                logger.debug("Bar parse failed for %s: %s", sym, e)
    except Exception as e:
        logger.warning("yfinance batch fetch failed: %s — falling back to per-symbol", e)
        for sym in symbols:
            try:
                df = yf.download(sym, start=start_dt.strftime("%Y-%m-%d"),
                                 end=end_dt.strftime("%Y-%m-%d"),
                                 interval="1d", auto_adjust=True, progress=False)
                if df.empty or len(df) < 5:
                    continue
                df.columns = [c.lower() for c in df.columns]
                df = df.tail(lookback)
                bars_out[sym] = df
            except Exception as e2:
                logger.debug("Per-symbol fetch failed for %s: %s", sym, e2)
    logger.info("yfinance bars fetched for %d/%d symbols", len(bars_out), len(symbols))
    return bars_out


# ── Tag outcome on exit ───────────────────────────────────────────────────────

def _tag_outcome(n_exits: int):
    """Run outcome_tracker after any exit to tag new closed trades."""
    if n_exits == 0:
        return
    try:
        import outcome_tracker
        n = outcome_tracker.run_tracker(verbose=False)
        if n > 0:
            logger.info("[OutcomeTracker] Tagged %d new trade(s)", n)
    except Exception as e:
        logger.warning("[OutcomeTracker] Non-fatal error: %s", e)


# ── Engine dispatch ───────────────────────────────────────────────────────────

def run_engine_b_exits(ledger: Ledger, bars_map: Dict, dry_run: bool) -> List[dict]:
    from exit_monitor_b import check_engine_b_exits
    positions = ledger.get_positions("B")
    if not positions:
        return []
    return check_engine_b_exits(
        positions, bars_map, ledger,
        ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
        dry_run=dry_run
    )


def run_engine_a_exits(ledger: Ledger, bars_map: Dict, dry_run: bool) -> List[dict]:
    from exit_monitor_a import check_engine_a_exits
    positions = ledger.get_positions("A")
    if not positions:
        return []
    exits = check_engine_a_exits(
        positions, bars_map, ledger,
        ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
        dry_run=dry_run
    )
    for ex in exits:
        ex.setdefault("engine_id", "A")
    return exits


def run_engine_f_exits(ledger: Ledger, bars_map: Dict, dry_run: bool) -> List[dict]:
    from exit_monitor_f import check_engine_f_exits
    positions = ledger.get_positions("F")
    if not positions:
        return []
    exits = check_engine_f_exits(
        positions, bars_map, ledger,
        ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
        dry_run=dry_run
    )
    for ex in exits:
        ex.setdefault("engine_id", "F")
    return exits


# ── Main ─────────────────────────────────────────────────────────────────────

def run_exit_monitor(dry_run: bool = False):
    logger.info("=" * 60)
    logger.info("ARES EXIT MONITOR — %s", datetime.now().isoformat())
    if dry_run:
        logger.info("DRY RUN — no orders will be submitted")
    logger.info("=" * 60)

    ledger      = Ledger()
    all_symbols = list(ledger.get_all_held_symbols())

    if not all_symbols:
        logger.info("No open Ares positions. Nothing to monitor.")
        return

    logger.info("Open positions: %d symbols across all engines", len(all_symbols))
    logger.info("Fetching bars for held symbols...")
    bars_map = fetch_bars(all_symbols)
    logger.info("Bars fetched for %d/%d symbols", len(bars_map), len(all_symbols))

    total_exits = []

    # ── Engine B ──────────────────────────────────────────────────────────────
    if ENGINE_STATUS.get("B") in ("shadow", "live"):
        exits_b = run_engine_b_exits(ledger, bars_map, dry_run)
        if exits_b:
            logger.info("Engine B exits: %d", len(exits_b))
        total_exits.extend(exits_b)

    # ── Engine A ──────────────────────────────────────────────────────────────
    if ENGINE_STATUS.get("A") in ("shadow", "live"):
        exits_a = run_engine_a_exits(ledger, bars_map, dry_run)
        if exits_a:
            logger.info("Engine A exits: %d", len(exits_a))
        total_exits.extend(exits_a)

    # ── Engine F ──────────────────────────────────────────────────────────────
    if ENGINE_STATUS.get("F") in ("shadow", "live"):
        exits_f = run_engine_f_exits(ledger, bars_map, dry_run)
        if exits_f:
            logger.info("Engine F exits: %d", len(exits_f))
        total_exits.extend(exits_f)

    # ── Engine C ──────────────────────────────────────────────────────────────
    if ENGINE_STATUS.get("C") in ("shadow", "live"):
        from exit_monitor_c import check_engine_c_exits
        positions_c = ledger.get_positions("C")
        if positions_c:
            exits_c = check_engine_c_exits(
                positions_c, bars_map, ledger,
                ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
                dry_run=dry_run
            )
            for ex in exits_c:
                ex.setdefault("engine_id", "C")
            if exits_c:
                logger.info("Engine C exits: %d", len(exits_c))
            total_exits.extend(exits_c)

    # ── Engine E ──────────────────────────────────────────────────────────────
    if ENGINE_STATUS.get("E") in ("shadow", "live"):
        from exit_monitor_e import check_engine_e_exits
        positions_e = ledger.get_positions("E")
        if positions_e:
            exits_e = check_engine_e_exits(
                positions_e, bars_map, ledger,
                ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
                dry_run=dry_run
            )
            for ex in exits_e:
                ex.setdefault("engine_id", "E")
            if exits_e:
                logger.info("Engine E exits: %d", len(exits_e))
            total_exits.extend(exits_e)

    # ── Summary ───────────────────────────────────────────────────────────────
    logger.info("")
    logger.info("=" * 60)
    logger.info("  ARES EXIT SUMMARY")
    logger.info("=" * 60)
    if total_exits:
        for ex in total_exits:
            eid = ex.get("engine_id", "B")
            logger.info("  [%s] %s | %s | pnl=%+.2f%% | days=%d",
                        eid, ex["symbol"], ex["exit_reason"],
                        ex["pnl_pct"], ex.get("days_held", 0))
    else:
        logger.info("  No exits triggered.")
    logger.info("=" * 60)

    _tag_outcome(len(total_exits))
    return total_exits


def print_status():
    ledger = Ledger()
    print(f"\n{'='*60}")
    print(f"  ARES OPEN POSITIONS — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")
    for eid in sorted(ENGINE_STATUS.keys()):
        positions = ledger.get_positions(eid)
        status    = ENGINE_STATUS.get(eid, "off")
        if not positions:
            print(f"  Engine {eid} [{status}]: no open positions")
            continue
        print(f"  Engine {eid} [{status}]: {len(positions)} position(s)")
        for p in positions:
            print(f"    {p['symbol']:8s}  {p['shares']} shares @ ${p['entry_price']:.2f}"
                  f"  entry={p['entry_date']}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Ares Exit Monitor")
    parser.add_argument("--dry-run", action="store_true", help="No orders submitted")
    parser.add_argument("--status",  action="store_true", help="Print open positions")
    args = parser.parse_args()

    if args.status:
        print_status()
    else:
        run_exit_monitor(dry_run=args.dry_run)
