"""
ares_position_sync.py — Alpaca vs Ledger Position Synchronizer
===============================================================
Compares live Alpaca positions against ares_position_ledger.json and
generates a corrective action plan. Does NOT auto-execute — shows exactly
what needs to be done and lets you confirm.

Run:
    python ares_position_sync.py           # Show divergences + action plan
    python ares_position_sync.py --fix     # Execute corrections after review
    python ares_position_sync.py --close-shorts   # Close any accidental short positions

CRITICAL: Run this whenever ares_reconcile.py --alpaca shows divergences.
"""

import os
import json
import logging
import argparse
import requests
from datetime import datetime
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from ledger import Ledger

ALPACA_KEY    = os.getenv("ARES_ALPACA_API_KEY")
ALPACA_SECRET = os.getenv("ARES_ALPACA_SECRET_KEY")
BASE_URL      = os.getenv("ARES_ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [PositionSync] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"logs/position_sync_{datetime.now():%Y%m%d}.log"),
    ],
)
logger = logging.getLogger("ares.sync")

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}


def get_alpaca_positions():
    try:
        resp = requests.get(f"{BASE_URL}/v2/positions", headers=HEADERS, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Alpaca position fetch failed: %s", e)
        return []


def get_alpaca_account():
    try:
        resp = requests.get(f"{BASE_URL}/v2/account", headers=HEADERS, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Alpaca account fetch failed: %s", e)
        return {}


def close_position(symbol: str, qty: int, side: str = "sell", dry_run: bool = False) -> dict:
    """Submit a market order to close/flatten a position."""
    if dry_run:
        logger.info("[DRY] Would %s %d %s", side, abs(qty), symbol)
        return {"dry_run": True, "symbol": symbol, "qty": qty}

    body = {
        "symbol":        symbol,
        "qty":           str(abs(qty)),
        "side":          side,
        "type":          "market",
        "time_in_force": "day",
        "client_order_id": f"SYNC_{side}_{symbol}_{datetime.now():%Y%m%d%H%M%S}",
    }
    try:
        resp = requests.post(f"{BASE_URL}/v2/orders", headers=HEADERS, json=body, timeout=10)
        resp.raise_for_status()
        order = resp.json()
        logger.info("ORDER PLACED | %s %s qty=%d | id=%s", side.upper(), symbol, abs(qty), order.get("id","?"))
        return order
    except Exception as e:
        logger.error("Order failed for %s: %s", symbol, e)
        return {"error": str(e)}


def run_sync(fix: bool = False, close_shorts: bool = False, dry_run: bool = True, void_ghosts_only: bool = False):
    sep = "=" * 72
    print(f"\n{sep}")
    print(f"  ARES POSITION SYNC  —  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if fix:
        print(f"  MODE: FIX (orders will be submitted)")
    elif close_shorts:
        print(f"  MODE: CLOSE SHORTS ONLY")
    else:
        print(f"  MODE: AUDIT ONLY (dry run)")
    print(sep)

    ledger          = Ledger()
    alpaca_raw      = get_alpaca_positions()
    account         = get_alpaca_account()

    # Build maps
    alpaca_map      = {}
    for p in alpaca_raw:
        sym = p["symbol"]
        qty = int(float(p.get("qty", 0)))
        cur = float(p.get("current_price", 0))
        mv  = float(p.get("market_value", 0))
        side = "long" if qty > 0 else "short"
        alpaca_map[sym] = {"qty": qty, "current_price": cur, "market_value": mv, "side": side, "raw": p}

    ledger_map = {}
    for p in ledger.data["positions"].values():
        sym = p["symbol"]
        ledger_map[sym] = {
            "engine_id":   p["engine_id"],
            "shares":      p["shares"],
            "entry_price": p["entry_price"],
            "entry_date":  p["entry_date"],
        }

    print(f"\n  Alpaca positions: {len(alpaca_map)}")
    for sym, d in alpaca_map.items():
        tag = "⚠️ SHORT" if d["side"] == "short" else "LONG"
        print(f"    {sym:8s}  qty={d['qty']:+d}  price={d['current_price']:.2f}  [{tag}]")

    print(f"\n  Ledger positions: {len(ledger_map)}")
    for sym, d in ledger_map.items():
        print(f"    {sym:8s}  shares={d['shares']}  entry={d['entry_price']:.2f}  Engine {d['engine_id']}")

    # ── Find divergences ──────────────────────────────────────────────────────
    all_syms = set(alpaca_map) | set(ledger_map)
    actions  = []
    warnings = []

    print(f"\n{sep}")
    print(f"  DIVERGENCE ANALYSIS")
    print(sep)

    for sym in sorted(all_syms):
        in_alpaca = sym in alpaca_map
        in_ledger = sym in ledger_map
        al = alpaca_map.get(sym, {})
        ld = ledger_map.get(sym, {})

        if in_alpaca and not in_ledger:
            qty   = al["qty"]
            price = al["current_price"]
            side  = al["side"]
            if side == "short":
                # Accidental short — buy to cover
                msg = f"⛔ SHORT {sym} qty={qty} @ ${price:.2f} — NO ledger entry — CLOSE SHORT (buy to cover)"
                print(f"\n  {msg}")
                actions.append({"action": "buy_to_cover", "symbol": sym, "qty": abs(qty),
                                "reason": "accidental_short_no_ledger"})
            else:
                # Alpaca long with no ledger — ghost position
                msg = f"⚠️  {sym} LONG qty={qty} @ ${price:.2f} in Alpaca but NOT in ledger"
                print(f"\n  {msg}")
                print(f"     Options:")
                print(f"     A) If this was a valid re-entry: ADD to ledger manually")
                print(f"     B) If this is a ghost: CLOSE in Alpaca (sell {qty} shares)")
                actions.append({"action": "review", "symbol": sym, "qty": qty,
                                "reason": "in_alpaca_not_ledger", "side": "long"})

        elif in_ledger and not in_alpaca:
            # Ledger says open but Alpaca doesn't have it
            print(f"\n  ❌ {sym} OPEN in ledger (Engine {ld['engine_id']}, {ld['shares']} sh) but NOT in Alpaca")
            print(f"     Entry date: {ld['entry_date']}  Entry price: ${ld['entry_price']:.2f}")
            print(f"     Most likely: entry ORDER FAILED but ledger was written — must close ledger entry")
            actions.append({"action": "close_ledger_entry", "symbol": sym,
                            "engine_id": ld["engine_id"],
                            "reason": "in_ledger_not_alpaca"})

        elif in_alpaca and in_ledger:
            al_qty = al["qty"]
            ld_qty = ld["shares"]
            diff   = abs(al_qty) - ld_qty
            side   = al["side"]

            if side == "short":
                print(f"\n  ⛔ {sym} is SHORT in Alpaca (qty={al_qty}) but LONG in ledger (shares={ld_qty})")
                print(f"     Buy to cover: {abs(al_qty)} shares, then ledger needs correction")
                actions.append({"action": "buy_to_cover", "symbol": sym, "qty": abs(al_qty),
                                "reason": "short_but_ledger_long"})
            elif diff != 0:
                print(f"\n  ⚠️  {sym} share count mismatch: Alpaca={al_qty}  Ledger={ld_qty}  diff={diff:+d}")
                actions.append({"action": "share_count_mismatch", "symbol": sym,
                                "alpaca_qty": al_qty, "ledger_qty": ld_qty})
            else:
                print(f"  ✅ {sym}: {ld_qty} shares match in both systems")

    # ── Execute or report ─────────────────────────────────────────────────────
    print(f"\n{sep}")
    print(f"  ACTION PLAN ({len(actions)} actions)")
    print(sep)

    executed = []
    for i, a in enumerate(actions, 1):
        sym    = a["symbol"]
        action = a["action"]
        reason = a.get("reason", "")
        print(f"\n  [{i}] {action.upper()} — {sym}  ({reason})")

        if action == "buy_to_cover":
            qty = a["qty"]
            print(f"       Submit BUY {qty} {sym} (market, to cover short)")
            if void_ghosts_only:
                print(f"       → Skipped (--void-ghosts only closes ledger entries)")
                continue
            if fix or close_shorts:
                order = close_position(sym, qty, side="buy", dry_run=False)
                executed.append({"action": action, "symbol": sym, "result": order})
                if "error" not in order:
                    # Remove from ledger if present (position is now flat)
                    if sym in ledger_map:
                        eid = ledger_map[sym]["engine_id"]
                        price = alpaca_map.get(sym, {}).get("current_price", 0)
                        ledger.record_exit(eid, sym, price,
                                           datetime.now().strftime("%Y-%m-%d"),
                                           "sync_close_short")
                        print(f"       Ledger: closed {eid}:{sym} (sync_close_short)")
            else:
                print(f"       → Run with --fix or --close-shorts to execute")

        elif action == "close_ledger_entry":
            eid = a["engine_id"]
            print(f"       Remove {eid}:{sym} from ledger (order never filled in Alpaca)")
            if fix or void_ghosts_only:
                # Record exit at entry price (net zero P&L since it never filled)
                ep = ledger_map[sym]["entry_price"]
                ledger.record_exit(eid, sym, ep,
                                   datetime.now().strftime("%Y-%m-%d"),
                                   "sync_void_unfilled")
                # Remove from outcomes if tagged (zero P&L trade)
                print(f"       Ledger: voided {eid}:{sym} at entry price (unfilled order)")
                executed.append({"action": action, "symbol": sym})
            else:
                print(f"       → Run with --fix to void this ledger entry")

        elif action == "review":
            qty  = a.get("qty", 0)
            side = a.get("side", "long")
            print(f"       Manual review needed — {sym} {side.upper()} qty={qty} in Alpaca, not in ledger")
            print(f"       If valid re-entry: add to ledger. If ghost: sell {qty} shares in Alpaca.")
            print(f"       Run --fix after manual review OR close in Alpaca dashboard")

        elif action == "share_count_mismatch":
            al_qty = a["alpaca_qty"]
            ld_qty = a["ledger_qty"]
            print(f"       Share mismatch: Alpaca={al_qty} Ledger={ld_qty}")
            print(f"       Manual correction needed — check order history")

    # ── Account summary ───────────────────────────────────────────────────────
    print(f"\n{sep}")
    print(f"  ACCOUNT SUMMARY")
    print(sep)
    equity = float(account.get("equity", 0))
    cash   = float(account.get("cash", 0))
    bp     = float(account.get("buying_power", 0))
    print(f"  Equity:        ${equity:>12,.2f}")
    print(f"  Cash:          ${cash:>12,.2f}")
    print(f"  Buying power:  ${bp:>12,.2f}")

    total_mv = sum(abs(d["market_value"]) for d in alpaca_map.values())
    short_mv = sum(abs(d["market_value"]) for d in alpaca_map.values() if d["side"] == "short")
    if short_mv > 0:
        print(f"  ⛔ Short MV:   ${short_mv:>12,.2f}  — CLOSE THESE IMMEDIATELY")

    if executed:
        print(f"\n  ✅ {len(executed)} actions executed in this run")

    print(sep + "\n")
    return actions


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ares Position Synchronizer")
    parser.add_argument("--fix",          action="store_true", help="Execute all corrective actions")
    parser.add_argument("--close-shorts", action="store_true", help="Only close accidental short positions")
    parser.add_argument("--void-ghosts",  action="store_true",
                        help="EOD: void ledger entries with no Alpaca position (unfilled orders)")
    args = parser.parse_args()
    if args.void_ghosts:
        # Shortcut: only close ghost ledger entries, no order submission
        run_sync(fix=True, close_shorts=False, void_ghosts_only=True)
    else:
        run_sync(fix=args.fix, close_shorts=args.close_shorts)
