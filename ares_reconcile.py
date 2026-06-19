"""
ares_reconcile.py — Ares Data Reconciliation Tool
==================================================
Compares ares_position_ledger.json, sub_engine_outcomes.json,
symbol_ownership.json, and Alpaca live positions to surface any
discrepancies before the daily recap runs.

Run:
    python ares_reconcile.py              # Console report
    python ares_reconcile.py --fix        # Auto-fix stale ownership + ic_valid
    python ares_reconcile.py --alpaca     # Also compare against live Alpaca positions

Schedule at 4:05 PM ET (before daily_recap at 4:15 PM).
"""

import os
import json
import logging
import argparse
from datetime import datetime, timedelta, date
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

LEDGER_FILE    = "ares_position_ledger.json"
OUTCOMES_FILE  = "sub_engine_outcomes.json"
OWNERSHIP_FILE = "symbol_ownership.json"

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Reconcile] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"logs/reconcile_{datetime.now():%Y%m%d}.log"),
    ],
)
logger = logging.getLogger("ares.reconcile")

TERMINAL_EXIT_PATHS = {
    "hard_stop", "trail_stop", "time_stop", "target", "manual",
    "avwap_reached", "thesis_invalid", "measured_move",
    "sector_rotation", "trend_exhaustion", "rs_lost",
}

def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load {path}: {e}")
        return default

def save_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)

def make_trade_key(t):
    return (t.get("engine_id"), t.get("symbol"), str(t.get("entry_date",""))[:10])

def is_terminal(exit_reason: str) -> bool:
    if not exit_reason:
        return False
    return any(label in exit_reason for label in TERMINAL_EXIT_PATHS)


def run_reconcile(fix: bool = False, check_alpaca: bool = False) -> dict:
    issues = []
    fixes  = []
    ok     = []

    ledger    = load_json(LEDGER_FILE, {"positions": {}, "closed": []})
    outcomes  = load_json(OUTCOMES_FILE, {"trades": []})
    ownership = load_json(OWNERSHIP_FILE, {})
    trades    = outcomes.get("trades", [])

    sep = "=" * 72
    print(f"\n{sep}")
    print(f"  ARES RECONCILIATION  —  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(sep)

    # ── 1. Open positions ─────────────────────────────────────────────────────
    open_pos   = ledger.get("positions", {})
    closed_pos = ledger.get("closed", [])
    open_syms  = {p["symbol"] for p in open_pos.values()}

    print(f"\n[1] LEDGER STATE")
    print(f"    Open positions: {len(open_pos)}")
    for key, p in sorted(open_pos.items()):
        mv = p.get("entry_price",0) * p.get("shares",0)
        print(f"      {key:12s}  {p['shares']} sh @ ${p['entry_price']:.2f} = ${mv:,.0f}  ({p['entry_date']})")
    print(f"    Closed trades:  {len(closed_pos)}")

    # ── 2. Ownership check ────────────────────────────────────────────────────
    print(f"\n[2] OWNERSHIP INTEGRITY")
    stale_owned   = {s: e for s, e in ownership.items() if s not in open_syms}
    missing_owned = {p["symbol"]: p["engine_id"] for p in open_pos.values() if p["symbol"] not in ownership}
    wrong_owner   = {s for s in ownership if s in open_syms and
                     ownership[s] != {p["symbol"]: p["engine_id"] for p in open_pos.values()}.get(s)}

    if stale_owned:
        msg = f"Stale ownership entries (no open position): {stale_owned}"
        issues.append(msg)
        print(f"    ❌ {msg}")
        if fix:
            for s in stale_owned:
                del ownership[s]
            save_json_atomic(OWNERSHIP_FILE, ownership)
            fix_msg = f"FIXED: Removed stale ownership entries {list(stale_owned.keys())}"
            fixes.append(fix_msg)
            print(f"    ✅ {fix_msg}")
    else:
        print(f"    ✅ No stale ownership entries")

    if missing_owned:
        msg = f"Open positions missing from ownership file: {missing_owned}"
        issues.append(msg)
        print(f"    ❌ {msg}")
    else:
        print(f"    ✅ All open positions registered in ownership file")

    if wrong_owner:
        for s in wrong_owner:
            issues.append(f"Engine mismatch: {s} ownership={ownership[s]} ledger={open_syms}")
            print(f"    ❌ Engine mismatch: {s}")
    else:
        ok.append("Ownership engine IDs consistent")
        print(f"    ✅ No engine/symbol ownership conflicts")

    # ── 3. Ledger vs outcomes ─────────────────────────────────────────────────
    print(f"\n[3] LEDGER ↔ OUTCOMES RECONCILIATION")
    ledger_keys  = {make_trade_key(t) for t in closed_pos}
    outcome_keys = {make_trade_key(t) for t in trades}

    in_ledger_not_outcomes = ledger_keys - outcome_keys
    in_outcomes_not_ledger = outcome_keys - ledger_keys

    if in_ledger_not_outcomes:
        msg = f"{len(in_ledger_not_outcomes)} closed trades in ledger but NOT in outcomes"
        issues.append(msg)
        print(f"    ❌ {msg}:")
        for k in sorted(in_ledger_not_outcomes):
            print(f"         {k}")
        if fix:
            # Auto-tag them by running outcome_tracker
            print(f"    → Run: python outcome_tracker.py to tag missing records")
    else:
        print(f"    ✅ All {len(ledger_keys)} closed trades have outcome records")

    if in_outcomes_not_ledger:
        msg = f"{len(in_outcomes_not_ledger)} outcomes with no matching ledger entry (may be older purged trades)"
        print(f"    ⚠️  {msg}:")
        for k in sorted(in_outcomes_not_ledger):
            print(f"         {k}")
    else:
        print(f"    ✅ All outcome records match ledger entries")

    # ── 4. P&L crosscheck ────────────────────────────────────────────────────
    print(f"\n[4] P&L CROSS-CHECK")
    pnl_mismatches = []
    outcome_map = {make_trade_key(t): t for t in trades}
    for lt in closed_pos:
        lk = make_trade_key(lt)
        ot = outcome_map.get(lk)
        if ot and abs((lt.get("pnl") or 0) - (ot.get("pnl") or 0)) > 0.02:
            pnl_mismatches.append((lk, lt.get("pnl"), ot.get("pnl")))
    if pnl_mismatches:
        for k, l, o in pnl_mismatches:
            msg = f"P&L mismatch {k}: ledger=${l:.2f} outcomes=${o:.2f}"
            issues.append(msg)
            print(f"    ❌ {msg}")
    else:
        ledger_net  = sum(t.get("pnl",0) or 0 for t in closed_pos)
        outcome_net = sum(t.get("pnl",0) or 0 for t in trades)
        print(f"    ✅ All P&Ls agree  (ledger=${ledger_net:+.2f} outcomes=${outcome_net:+.2f})")

    # ── 5. ic_valid check ────────────────────────────────────────────────────
    print(f"\n[5] IC_VALID INTEGRITY")
    ic_wrong = []
    for t in trades:
        exit_reason = t.get("exit_reason", "")
        should_be   = is_terminal(exit_reason)
        actual      = t.get("ic_valid", False)
        if should_be != actual:
            ic_wrong.append((t["trade_id"], exit_reason, should_be, actual))

    if ic_wrong:
        for tid, reason, should, actual in ic_wrong:
            msg = f"ic_valid wrong: {tid} (exit={reason}) should={should} got={actual}"
            issues.append(msg)
            print(f"    ❌ {msg}")
        if fix:
            for t in trades:
                exit_reason = t.get("exit_reason", "")
                t["ic_valid"] = is_terminal(exit_reason)
            save_json_atomic(OUTCOMES_FILE, outcomes)
            fix_msg = f"FIXED: Corrected ic_valid on {len(ic_wrong)} records"
            fixes.append(fix_msg)
            print(f"    ✅ {fix_msg}")
    else:
        ic_valid_count = sum(1 for t in trades if t.get("ic_valid"))
        print(f"    ✅ All ic_valid flags correct  ({ic_valid_count}/{len(trades)} terminal exits)")

    # ── 6. Same-day flips ────────────────────────────────────────────────────
    print(f"\n[6] SAME-DAY ROUND TRIPS")
    sameday = [(t.get("symbol"), t.get("engine_id"), t.get("entry_date"), t.get("pnl"))
               for t in closed_pos if t.get("entry_date") == t.get("exit_date")]
    if sameday:
        for sym, eid, d, pnl in sameday:
            print(f"    ⚠️  Engine {eid}: {sym} entered+exited on {d}  pnl=${pnl:+.2f}")
        print(f"    Note: Same-day round trips are normal for momentum exhaustion exits.")
        print(f"          Confirm these are not PDT violations on paper account.")
    else:
        print(f"    ✅ No same-day round trips")

    # ── 7. Forward return status ──────────────────────────────────────────────
    print(f"\n[7] FORWARD RETURN STATUS")
    today = date.today()
    null_fwd = [(t["trade_id"], t.get("ic_horizon_days",10),
                 datetime.strptime(str(t.get("entry_date","2000-01-01"))[:10], "%Y-%m-%d").date())
                for t in trades if t.get("forward_return_Nd") is None and t.get("ic_valid")]
    past_due = [(tid, h, ed) for tid, h, ed in null_fwd
                if ed + timedelta(days=int(h * 1.45)) <= today]
    not_yet  = [(tid, h, ed) for tid, h, ed in null_fwd
                if ed + timedelta(days=int(h * 1.45)) > today]

    if past_due:
        for tid, h, ed in past_due:
            due = ed + timedelta(days=int(h * 1.45))
            msg = f"Forward return overdue: {tid} (due {due})"
            issues.append(msg)
            print(f"    ❌ {msg}")
        print(f"    → Run: python outcome_tracker.py --forward")
    else:
        print(f"    ✅ No overdue forward returns")
    if not_yet:
        print(f"    ⏳ {len(not_yet)} forward returns pending (horizon not elapsed)")

    # ── 8. Alpaca live check ──────────────────────────────────────────────────
    if check_alpaca:
        print(f"\n[8] ALPACA LIVE POSITIONS vs LEDGER")
        try:
            import requests as req
            api_key    = os.getenv("ARES_ALPACA_API_KEY")
            api_secret = os.getenv("ARES_ALPACA_SECRET_KEY")
            base_url   = os.getenv("ARES_ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
            headers    = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret}
            resp = req.get(f"{base_url}/v2/positions", headers=headers, timeout=10)
            resp.raise_for_status()
            alpaca_positions = resp.json()
            alpaca_syms = {p["symbol"] for p in alpaca_positions}

            ledger_syms = open_syms
            in_alpaca_not_ledger = alpaca_syms - ledger_syms
            in_ledger_not_alpaca = ledger_syms - alpaca_syms

            if in_alpaca_not_ledger:
                msg = f"Alpaca has positions NOT in ledger: {in_alpaca_not_ledger}"
                issues.append(msg)
                print(f"    ❌ {msg}")
            else:
                print(f"    ✅ All Alpaca positions present in ledger")

            if in_ledger_not_alpaca:
                msg = f"Ledger has positions NOT in Alpaca: {in_ledger_not_alpaca}"
                issues.append(msg)
                print(f"    ❌ {msg}")
            else:
                print(f"    ✅ All ledger positions present in Alpaca")

            # Share count check
            alpaca_map = {p["symbol"]: int(float(p.get("qty",0))) for p in alpaca_positions}
            for p in open_pos.values():
                sym = p["symbol"]
                if sym in alpaca_map:
                    diff = abs(p["shares"] - alpaca_map[sym])
                    if diff > 0:
                        msg = f"Share count mismatch: {sym} ledger={p['shares']} alpaca={alpaca_map[sym]}"
                        issues.append(msg)
                        print(f"    ❌ {msg}")
                    else:
                        print(f"    ✅ {sym}: {p['shares']} shares match")

        except Exception as e:
            print(f"    ⚠️  Alpaca API check failed: {e}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{sep}")
    print(f"  RECONCILIATION SUMMARY")
    print(sep)
    if issues:
        print(f"  ❌ {len(issues)} issues found:")
        for i in issues:
            print(f"     - {i}")
    else:
        print(f"  ✅ All checks passed — data is clean")

    if fixes:
        print(f"  🔧 {len(fixes)} auto-fixes applied:")
        for f_ in fixes:
            print(f"     - {f_}")
    print(sep + "\n")

    return {"issues": issues, "fixes": fixes}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ares Data Reconciliation")
    parser.add_argument("--fix",    action="store_true", help="Auto-fix stale ownership + ic_valid errors")
    parser.add_argument("--alpaca", action="store_true", help="Also compare against live Alpaca positions")
    args = parser.parse_args()
    run_reconcile(fix=args.fix, check_alpaca=args.alpaca)
