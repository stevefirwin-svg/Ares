"""
outcome_tracker.py — Ares Outcome Tagging
==========================================
Pulls closed trades from the Ares ledger, tags each with engine_id,
signals at entry, and realized P&L. Writes tagged records to
sub_engine_outcomes.json and populates forward_return_Nd after the IC
horizon has elapsed.

Run:
    python outcome_tracker.py              # tag all untagged closed trades
    python outcome_tracker.py --summary    # per-engine summary table
    python outcome_tracker.py --forward    # backfill forward returns where due

Called automatically after exit_monitor.py after any execution.

IC validity rules:
  - ic_valid = True only for terminal exits (hard_stop, trail_stop, time_stop,
    target, manual) — NOT partial trims
  - forward_return_Nd is the N-day return anchored at entry_price on entry_date+N
    (N = IC_HORIZON_DAYS[engine_id]). Never anchored to exit.
  - IC calibration gate: 30 ic_valid trades per engine before weights replace bootstrap
"""

import os
import json
import logging
import argparse
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional
from dotenv import load_dotenv

from ares_config import IC_HORIZON_DAYS, IC_CALIBRATION_GATE

load_dotenv()

ALPACA_KEY    = os.getenv("ARES_ALPACA_API_KEY")
ALPACA_SECRET = os.getenv("ARES_ALPACA_SECRET_KEY")
BASE_URL      = os.getenv("ARES_ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# RF-22: Feed selection for bar data.
# IEX = ~2-3% of consolidated tape — acceptable for intraday signals during paper trading
# but volume-based metrics (b_volume_climax, f_breakout_strength) are biased on IEX.
# Forward-return IC calculation must use SIP (full consolidated tape) for live accounts
# to avoid systematically over/under-stating returns.
#
# Rule: use SIP when ARES_ALPACA_BASE_URL is the live URL; IEX otherwise (paper/test).
_is_live = "paper" not in BASE_URL.lower() and "sandbox" not in BASE_URL.lower()
BAR_FEED = "sip" if _is_live else "iex"

LEDGER_FILE   = "ares_position_ledger.json"
OUTCOMES_FILE = "sub_engine_outcomes.json"

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [OutcomeTracker] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"logs/outcome_tracker_{datetime.now():%Y%m%d}.log"),
    ],
)
logger = logging.getLogger("ares.outcome_tracker")

TERMINAL_EXIT_PATHS = {
    "hard_stop", "trail_stop", "time_stop", "target", "manual",
    # Engine-specific terminal labels added as engines are built:
    "avwap_reached",     # Engine B
    "thesis_invalid",    # Engine B/A
    "measured_move",     # Engine F
    "sector_rotation",   # Engine E
    "trend_exhaustion",  # Engine A — momentum exhaustion terminal exit
    "rs_lost",           # Engine E — relative strength reversal terminal exit
    "exhausted_at_loss", # Cross-engine hold-health override, added 2026-06-28
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def alpaca_headers():
    return {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }


def parse_ts(ts_str: str) -> datetime:
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fetch_price_after(symbol: str, after_date: str, days: int) -> Optional[float]:
    """
    Fetch the closing price exactly `days` TRADING DAYS after `after_date`.

    RF-12 fix: previous implementation used calendar-day windowing with an
    off-by-two start offset, causing the returned bar to be ~14 trading days
    after entry for a declared 10-day horizon (systematic +40% bias).

    Correct method:
      1. Fetch daily closes starting from after_date (inclusive), with a generous
         calendar-day buffer to cover weekends + holidays.
      2. Drop the entry bar itself (date == after_date).
      3. Select index [days-1] of the strictly-ascending post-entry bars —
         this is the Nth trading bar after entry, regardless of calendar gaps.

    Uses yfinance (replaces Alpaca/IEX which had coverage gaps on paper accounts).
    Returns None if fewer than N trading bars are available.
    """
    try:
        import yfinance as yf
        entry_dt   = parse_ts(after_date).date()
        cal_buffer = int(days * 1.6) + 10
        end_dt     = entry_dt + timedelta(days=cal_buffer)

        raw = yf.download(
            tickers=symbol,
            start=entry_dt.strftime("%Y-%m-%d"),
            end=end_dt.strftime("%Y-%m-%d"),
            interval="1d",
            auto_adjust=True,
            progress=False,
        )
        if raw.empty:
            return None

        raw.index = raw.index.normalize()
        # Drop the entry bar itself — forward return is strictly post-entry
        post_entry = raw[raw.index.date > entry_dt]
        if len(post_entry) < days:
            return None

        close_col = "Close" if "Close" in post_entry.columns else "close"
        return float(post_entry[close_col].iloc[days - 1])

    except Exception:
        return None


def _load_outcomes() -> dict:
    if os.path.exists(OUTCOMES_FILE):
        with open(OUTCOMES_FILE) as f:
            return json.load(f)
    return {"_schema": {}, "trades": []}


def _save_outcomes(data: dict):
    tmp = OUTCOMES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, OUTCOMES_FILE)


def _load_ledger() -> dict:
    if os.path.exists(LEDGER_FILE):
        with open(LEDGER_FILE) as f:
            return json.load(f)
    return {"positions": {}, "closed": []}


# ── Core tagging ──────────────────────────────────────────────────────────────

def build_trade_id(engine_id: str, symbol: str, entry_date: str) -> str:
    date_slug = str(entry_date)[:10].replace("-", "")
    return f"{engine_id}-{symbol}-{date_slug}"


def is_terminal_exit(exit_reason: str) -> bool:
    if not exit_reason:
        return False
    return any(label in exit_reason for label in TERMINAL_EXIT_PATHS)


def run_tracker(verbose: bool = True) -> int:
    outcomes   = _load_outcomes()
    ledger     = _load_ledger()
    closed     = ledger.get("closed", [])

    existing_ids = {t["trade_id"] for t in outcomes.get("trades", [])}
    new_records  = []

    for trade in closed:
        engine_id  = trade.get("engine_id")
        symbol     = trade.get("symbol")
        entry_date = trade.get("entry_date")

        if not engine_id or not symbol or not entry_date:
            if verbose:
                print(f"  [SKIP] Missing engine_id/symbol/entry_date: {trade}")
            continue

        trade_id = build_trade_id(engine_id, symbol, entry_date)
        if trade_id in existing_ids:
            continue

        exit_reason  = trade.get("exit_reason", "unknown")
        terminal     = is_terminal_exit(exit_reason)
        # ic_valid: terminal exit + full position (no partial-trim-to-zero)
        # Trims that zero the position still use exit_reason — terminal check is sufficient
        # for now; can add shares_check when trim history is richer.
        ic_valid = terminal

        ic_horizon   = IC_HORIZON_DAYS.get(engine_id, 10)

        record = {
            "trade_id":          trade_id,
            "engine_id":         engine_id,
            "symbol":            symbol,
            "entry_date":        str(entry_date),
            "exit_date":         str(trade.get("exit_date", "")),
            "entry_price":       trade.get("entry_price"),
            "exit_price":        trade.get("exit_price"),
            "shares":            trade.get("shares"),
            "pnl":               trade.get("pnl"),
            "pnl_pct":           trade.get("pnl_pct"),
            "exit_reason":       exit_reason,
            "macro_regime":      trade.get("metadata", {}).get("macro_regime"),
            "signals_at_entry":  trade.get("metadata", {}).get("signals_at_entry", {}),
            "ic_horizon_days":   ic_horizon,
            "ic_valid":          ic_valid,
            "forward_return_Nd": None,   # filled by --forward pass
            "outcome_tagged_at": datetime.now(timezone.utc).isoformat(),
        }
        new_records.append(record)
        existing_ids.add(trade_id)

        if verbose:
            pnl_str = f"{record['pnl_pct']:+.2f}%" if record["pnl_pct"] is not None else "N/A"
            print(f"  [+] {engine_id}:{symbol:8s} | exit: {str(exit_reason):20s} | "
                  f"pnl: {pnl_str:8s} | ic_valid: {ic_valid}")

    if new_records:
        outcomes["trades"].extend(new_records)
        _save_outcomes(outcomes)
        print(f"\n[OutcomeTracker] Tagged {len(new_records)} new trade(s) → {OUTCOMES_FILE}")
    else:
        print("[OutcomeTracker] No new trades to tag.")

    return len(new_records)


# ── Forward return backfill ───────────────────────────────────────────────────

def run_forward_fill(verbose: bool = True) -> int:
    """
    For each ic_valid trade where forward_return_Nd is still None,
    check if the IC horizon has elapsed. If so, fetch the forward price
    and compute the return.
    """
    outcomes = _load_outcomes()
    trades   = outcomes.get("trades", [])
    today    = datetime.now(timezone.utc).date()
    filled   = 0

    for t in trades:
        if not t.get("ic_valid"):
            continue
        if t.get("forward_return_Nd") is not None:
            continue

        entry_date_str = t.get("entry_date", "")
        if not entry_date_str:
            continue

        try:
            entry_date = parse_ts(entry_date_str).date()
        except Exception:
            continue

        horizon     = t.get("ic_horizon_days", 10)
        # Horizon elapsed when entry_date + N (in calendar days) has passed
        target_date = entry_date + timedelta(days=int(horizon * 1.45))  # calendar days

        if today < target_date:
            continue   # horizon not elapsed yet

        # Fetch closing price approximately N trading days after entry
        fwd_price = fetch_price_after(t["symbol"], entry_date_str, horizon)
        if fwd_price is None:
            if verbose:
                print(f"  [SKIP] {t['engine_id']}:{t['symbol']} — forward price unavailable")
            continue

        # Denominator is entry_price — IC = Spearman(signal_at_entry, return_from_entry)
        entry_price = t.get("entry_price")
        if not entry_price:
            continue

        fwd_return = round(((fwd_price / entry_price) - 1) * 100, 4)
        t["forward_return_Nd"] = fwd_return
        filled += 1

        if verbose:
            print(f"  [fwd] {t['engine_id']}:{t['symbol']:8s} "
                  f"entry={float(entry_price):.2f} → fwd={fwd_price:.2f}  "
                  f"return={fwd_return:+.2f}% ({horizon}d from entry)")

    if filled:
        _save_outcomes(outcomes)
        print(f"\n[OutcomeTracker] Filled {filled} forward return(s) → {OUTCOMES_FILE}")
    else:
        print("[OutcomeTracker] No forward returns due yet.")

    return filled


def run_shadow_forward_fill(verbose: bool = True) -> int:
    """
    RF-15: Backfill forward_return_Nd for shadow classifications past their IC horizon.

    This is the critical missing link: without it, shadow IC is uncomputable and
    the SHADOW→LIVE gate (Rule 19: IC>0.05 AND ICIR>0.5) can never be evaluated.

    For each shadow classification in shadow_classifications.json:
      - Conditioned (normal) and unconditioned (RF-10) records both receive backfill
      - Only fills records where outcome_tagged=False and horizon has elapsed
      - Uses the corrected fetch_price_after (RF-12) for exact N trading-day lookup
      - Writes forward_return_Nd = (fwd_price / entry_price) - 1 (decimal, not %)
        matching the sub_engine_outcomes.json schema (IC is computed on decimals)

    Run after market close daily alongside --forward.
    """
    shadow_file = "shadow_classifications.json"
    if not os.path.exists(shadow_file):
        print("[ShadowFwd] shadow_classifications.json not found — nothing to fill.")
        return 0

    with open(shadow_file) as f:
        shadow_data = json.load(f)

    classifications = shadow_data.get("classifications", [])
    today = datetime.now(timezone.utc).date()
    filled = 0

    for rec in classifications:
        if rec.get("outcome_tagged"):
            continue
        if rec.get("forward_return_Nd") is not None:
            continue

        engine_id = rec.get("engine_id")
        symbol    = rec.get("symbol")
        entry_date_str = rec.get("date")  # "YYYY-MM-DD"
        entry_price    = rec.get("entry_price")

        if not all([engine_id, symbol, entry_date_str, entry_price]):
            continue

        horizon = IC_HORIZON_DAYS.get(engine_id, 10)

        try:
            entry_dt = datetime.strptime(entry_date_str, "%Y-%m-%d").date()
        except Exception:
            continue

        # Check horizon elapsed: need N trading days which is ~N*1.4 calendar days minimum
        cal_elapsed = (today - entry_dt).days
        if cal_elapsed < int(horizon * 1.4):
            continue   # horizon not elapsed yet

        fwd_price = fetch_price_after(symbol, entry_date_str, horizon)
        if fwd_price is None:
            if verbose:
                print(f"  [SKIP] {engine_id}:{symbol} shadow — forward price unavailable")
            continue

        fwd_return = round((fwd_price / float(entry_price)) - 1.0, 6)  # decimal, not %
        rec["forward_return_Nd"] = fwd_return
        rec["outcome_tagged"]    = True
        filled += 1

        if verbose:
            conditioned = rec.get("conditioned_by_ownership", True)
            tag = "UNC" if conditioned is False else "cond"
            print(f"  [shadow-fwd] {engine_id}:{symbol:8s} [{tag}] "
                  f"entry={float(entry_price):.2f} → fwd={fwd_price:.2f}  "
                  f"return={fwd_return:+.4f} ({horizon}d)")

    if filled:
        tmp = shadow_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(shadow_data, f, indent=2, default=str)
        os.replace(tmp, shadow_file)
        print(f"\n[ShadowFwd] Filled {filled} shadow forward return(s) → {shadow_file}")
    else:
        print("[ShadowFwd] No shadow forward returns due yet.")

    return filled




def print_summary():
    outcomes = _load_outcomes()
    trades   = outcomes.get("trades", [])

    if not trades:
        print("[OutcomeTracker] sub_engine_outcomes.json is empty — no closed trades yet.")
        return

    from collections import defaultdict

    by_engine = defaultdict(list)
    for t in trades:
        by_engine[t.get("engine_id", "?")].append(t)

    print(f"\n{'='*72}")
    print(f"  ARES OUTCOME TRACKER — {len(trades)} total closed trades")
    print(f"{'='*72}")
    print(f"  {'Engine':<8} {'Trades':>7} {'IC-Valid':>9} {'Win%':>6} {'Avg P&L':>9} "
          f"{'Net P&L':>10} {'Fwd Filled':>11}")
    print(f"  {'-'*8} {'-'*7} {'-'*9} {'-'*6} {'-'*9} {'-'*10} {'-'*11}")

    for eid in sorted(by_engine.keys()):
        etrades   = by_engine[eid]
        ic_valid  = [t for t in etrades if t.get("ic_valid")]
        with_pnl  = [t for t in etrades if t.get("pnl_pct") is not None]
        wins      = [t for t in with_pnl if t["pnl_pct"] > 0]
        win_pct   = round(len(wins) / len(with_pnl) * 100, 1) if with_pnl else 0.0
        avg_pnl   = round(sum(t["pnl_pct"] for t in with_pnl) / len(with_pnl), 2) if with_pnl else 0.0
        net_pnl   = round(sum(t.get("pnl", 0) or 0 for t in etrades), 2)
        fwd_done  = sum(1 for t in ic_valid if t.get("forward_return_Nd") is not None)
        ic_gate   = IC_CALIBRATION_GATE

        gate_str  = f"{len(ic_valid)}/{ic_gate}"
        print(f"  {eid:<8} {len(etrades):>7} {gate_str:>9} {win_pct:>5.1f}% "
              f"{avg_pnl:>+8.2f}% ${net_pnl:>9,.2f} {fwd_done:>5}/{len(ic_valid)}")

    print()

    # Exit path breakdown across all engines
    by_path = defaultdict(list)
    for t in trades:
        path = t.get("exit_reason", "unknown")
        if t.get("pnl_pct") is not None:
            by_path[path].append(t["pnl_pct"])

    print(f"  Exit path breakdown:")
    print(f"  {'Path':<24} {'N':>4} {'Win%':>6} {'Avg':>8}")
    for path, pnls in sorted(by_path.items()):
        wins = sum(1 for p in pnls if p > 0)
        avg  = sum(pnls) / len(pnls)
        print(f"  {path:<24} {len(pnls):>4} {wins/len(pnls)*100:>5.0f}% {avg:>+7.2f}%")

    pending_fwd = sum(
        1 for t in trades
        if t.get("ic_valid") and t.get("forward_return_Nd") is None
    )
    if pending_fwd:
        print(f"\n  Pending forward returns: {pending_fwd} — run --forward to fill")

    print(f"{'='*72}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ares Outcome Tracker")
    parser.add_argument("--summary",        action="store_true", help="Per-engine summary table")
    parser.add_argument("--forward",        action="store_true", help="Backfill forward returns for closed trades")
    parser.add_argument("--shadow-forward", action="store_true", help="Backfill forward returns for shadow classifications (RF-15)")
    parser.add_argument("--all-forward",    action="store_true", help="Run both --forward and --shadow-forward")
    parser.add_argument("--quiet",          action="store_true", help="Suppress per-trade output")
    args = parser.parse_args()

    if args.summary:
        print_summary()
    elif args.shadow_forward:
        run_shadow_forward_fill(verbose=not args.quiet)
    elif args.all_forward:
        run_forward_fill(verbose=not args.quiet)
        run_shadow_forward_fill(verbose=not args.quiet)
        print_summary()
    elif args.forward:
        n = run_forward_fill(verbose=not args.quiet)
        if n > 0:
            print_summary()
    else:
        n = run_tracker(verbose=not args.quiet)
        if n > 0:
            print_summary()
