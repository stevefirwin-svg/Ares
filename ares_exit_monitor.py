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

from bars_fetch import fetch_bars as _bars_fetch_shared

def fetch_bars(symbols: List[str], lookback: int = BARS_LOOKBACK) -> Dict[str, pd.DataFrame]:
    """
    Fetch daily OHLCV bars via yfinance.

    Thin wrapper around the shared implementation in bars_fetch.py
    (consolidated 2026-06-28).

    BUG FIX (2026-06-28): the previous implementation here put `timestamp`
    on the DataFrame index, never as a column. exit_monitor_a.py's and
    exit_monitor_f.py's _high_water() (trailing-stop high-water-mark calc)
    only does its real "max close since entry date" computation
    `if "timestamp" in df.columns` — which was never true, so both engines'
    trailing stops have been silently falling back to max(current_price,
    entry_price) instead of the true peak since entry. bars_fetch.py
    returns `timestamp` as a column, which restores the intended behavior.
    This is a real change to live exit behavior, not just a refactor:
    trailing stops on Engine A/F will now trail the true high-since-entry
    (tighter / more protective) instead of just the current price.
    """
    return _bars_fetch_shared(symbols, lookback=lookback, min_bars=5)


# ── Cross-engine hold-health override ────────────────────────────────────────

HOLD_HEALTH_FILE = "ares_hold_health.json"


def _load_health_map() -> dict:
    if os.path.exists(HOLD_HEALTH_FILE):
        try:
            with open(HOLD_HEALTH_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _days_held(entry_date_str: str) -> int:
    try:
        entry = datetime.strptime(str(entry_date_str)[:10], "%Y-%m-%d").date()
        return (datetime.now().date() - entry).days
    except Exception:
        return 0


def check_exhausted_at_loss(
    ledger: Ledger,
    bars_map: Dict[str, pd.DataFrame],
    health_map: dict,
    already_exited: set,
    dry_run: bool = False,
) -> List[dict]:
    """
    Cross-engine override, added 2026-06-28.

    ares_hold_monitor.py computes an EXHAUSTED/DECAYING/STRENGTHENING health
    tier per position, but until now nothing in exit logic ever read it — it
    was dashboard-only. Per-engine exit logic generally won't close a losing
    position on exhaustion signals alone by design (e.g. Engine A's own
    trend_exhaustion exit only fires when profitable — "no reason to exit at
    a loss on ADX noise alone"). That leaves positions like MRVL (EXHAUSTED
    tier, at a loss) with no exit path until the (often wide) hard stop is
    hit.

    This adds one explicit, engine-agnostic rule on top of each engine's own
    logic: if a position's hold-health tier is EXHAUSTED *and* it is
    currently at a loss, close it — regardless of what the engine-specific
    exit logic decided. Runs after all per-engine exit checks, and only
    considers positions that weren't already closed earlier in this run.
    """
    from exit_monitor_a import submit_sell as _sell_a
    from exit_monitor_b import submit_sell as _sell_b
    from exit_monitor_c import submit_sell as _sell_c
    from exit_monitor_e import submit_sell as _sell_e
    from exit_monitor_f import submit_sell as _sell_f
    sellers = {"A": _sell_a, "B": _sell_b, "C": _sell_c, "E": _sell_e, "F": _sell_f}

    exits_executed = []
    for eid in ("A", "B", "C", "E", "F"):
        for pos in ledger.get_positions(eid):
            symbol = pos.get("symbol")
            if not symbol or symbol in already_exited:
                continue
            entry_price = float(pos.get("entry_price", 0))
            shares      = pos.get("shares", 0)
            if entry_price <= 0 or shares <= 0:
                continue

            tier = health_map.get(symbol, {}).get("tier")
            if tier != "EXHAUSTED":
                continue

            df = bars_map.get(symbol)
            if df is None or len(df) == 0:
                logger.warning("EXHAUSTED_AT_LOSS: no bars for %s — skipping override check", symbol)
                continue
            price   = float(df["close"].iloc[-1])
            pnl_pct = round(((price / entry_price) - 1) * 100, 4)

            if pnl_pct >= 0:
                continue  # override only applies to losing positions

            submit_sell = sellers[eid]
            order = submit_sell(
                symbol, shares, "exhausted_at_loss",
                ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
                dry_run=dry_run,
            )
            if order or dry_run:
                exit_date = datetime.now().strftime("%Y-%m-%d")
                if not dry_run:
                    ledger.record_exit(eid, symbol, price, exit_date, "exhausted_at_loss")

                exits_executed.append({
                    "symbol":      symbol,
                    "engine_id":   eid,
                    "exit_reason": "exhausted_at_loss",
                    "exit_detail": f"hold-health tier=EXHAUSTED | pnl={pnl_pct:.2f}%",
                    "price":       price,
                    "pnl_pct":     pnl_pct,
                    "days_held":   _days_held(pos.get("entry_date", "")),
                })
                logger.info("EXIT EXHAUSTED_AT_LOSS | [%s] %s | pnl=%.2f%%",
                            eid, symbol, pnl_pct)

    return exits_executed


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

    # ── Cross-engine hold-health override ────────────────────────────────────
    health_map = _load_health_map()
    if health_map:
        already_exited = {ex["symbol"] for ex in total_exits}
        exits_exhausted = check_exhausted_at_loss(
            ledger, bars_map, health_map, already_exited, dry_run=dry_run
        )
        if exits_exhausted:
            logger.info("Exhausted-at-loss override exits: %d", len(exits_exhausted))
        total_exits.extend(exits_exhausted)

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
