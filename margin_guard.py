"""
margin_guard.py — Ares Capital Utilization and Margin Safety
=============================================================
Two-level guard:

  1. PORTFOLIO GUARD — checks overall account utilization.
     Same logic as Raptor: BLOCK > 90%, REDUCE > 85%.

  2. ENGINE GUARD — checks per-engine budget before any entry.
     Each engine has a hard capital ceiling from ares_config.py.
     If an engine is at or over its ceiling, it cannot add new positions.

Usage:
    from margin_guard import check_margin_safety, check_engine_budget

    # Portfolio-level gate (run once per scan, before any entries)
    account   = get_account()        # raw Alpaca /v2/account JSON
    positions = get_positions()      # raw Alpaca /v2/positions JSON (list)
    allowed, max_new, reason = check_margin_safety(account, positions)
    if not allowed:
        return

    # Engine-level gate (run before each engine entry)
    ok, headroom, reason = check_engine_budget(ledger, engine_id, total_equity)
    if not ok:
        continue

    python margin_guard.py   # standalone snapshot of all engines

NOTE (2026-06-28): check_margin_safety() previously took a Raptor-style `dm`
(DataManager) object with a `.alpaca.get_positions()` interface. None of the
live Ares engines (engine_a.py..engine_f.py) use that interface — they all
fetch account/positions via direct Alpaca REST calls — which is exactly why
this guard was built but never actually wired into any live engine despite
being marked "Done" in ARES_STARTUP.md. Signature changed to take plain
`account` (dict) and `positions` (list) so every engine can call it directly.
"""

import os
import logging
from typing import Tuple

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

from ares_config import (
    INITIAL_ENGINE_WEIGHTS,
    ENGINE_CAPITAL_FLOOR,
    TOTAL_EQUITY,
)

logger = logging.getLogger("ares.margin_guard")

# ── Portfolio-level thresholds ────────────────────────────────────────────────
# Derived from: max_positions=10 × max_position_pct=0.08 = 80% theoretical max.
# WARN at 75%: 5% below theoretical max — first signal of approaching limits.
# REDUCE at 85%: 5% above theoretical max — one oversized position or creep.
# BLOCK at 90%: clear breach, fail closed.
# TODO:DERIVE from empirical margin call distance analysis after equity curve data.
BLOCK_THRESHOLD  = 0.90
REDUCE_THRESHOLD = 0.85
WARN_THRESHOLD   = 0.75

_UNLIMITED = 10_000  # Sentinel for normal operation — explicit, not magic 99


def check_margin_safety(account: dict, positions: list) -> Tuple[bool, int, str]:
    """
    Portfolio-level margin and utilization check.

    Args:
        account   — raw Alpaca /v2/account JSON (dict)
        positions — raw Alpaca /v2/positions JSON (list of dicts)

    Returns:
        allowed  (bool) — True if new entries are permitted
        max_new  (int)  — max new positions allowed this scan (0 = blocked)
        reason   (str)  — explanation for log

    Fail-safe: returns BLOCKED on any API error (fail closed, not open).
    """
    try:
        equity = float(account.get("equity", 0))
        cash   = float(account.get("cash", 0))

        if equity <= 0:
            return False, 0, "equity is zero or negative — blocking all entries"

        total_mv  = sum(
            abs(float(p.get("qty", 0))) * float(p.get("current_price", 0))
            for p in positions
        )
        util      = total_mv / equity
        on_margin = cash < 0
        margin_pct = abs(cash) / equity if on_margin else 0.0

        logger.info(
            "MARGIN GUARD: equity=$%s  cash=$%s  positions=%d  util=%.1f%%  margin=%s",
            f"{equity:,.0f}", f"{cash:,.0f}", len(positions),
            util * 100, "YES" if on_margin else "NO"
        )

        if util > BLOCK_THRESHOLD:
            return False, 0, f"utilization {util:.1%} > {BLOCK_THRESHOLD:.0%} — blocking all entries"

        if util > REDUCE_THRESHOLD:
            return True, 1, f"utilization {util:.1%} > {REDUCE_THRESHOLD:.0%} — capping at 1 new entry"

        if on_margin:
            logger.warning(
                "MARGIN GUARD: ON MARGIN ($%s, %.1f%%) — capping at 1 new entry",
                f"{abs(cash):,.0f}", margin_pct * 100
            )
            return True, 1, f"on margin (${abs(cash):,.0f}, {margin_pct:.1%}) — capping at 1 new entry"

        if util > WARN_THRESHOLD:
            logger.warning("MARGIN GUARD: WARNING — utilization %.1f%%", util * 100)

        return True, _UNLIMITED, f"utilization {util:.1%} — normal operation"

    except Exception as e:
        logger.error("MARGIN GUARD: check failed (%s) — BLOCKING (fail closed)", e)
        return False, 0, f"guard error (fail closed): {e}"


# ── Engine-level budget check ─────────────────────────────────────────────────

def check_engine_budget(ledger, engine_id: str, total_equity: float = None) -> Tuple[bool, float, str]:
    """
    Per-engine capital budget check.

    Compares the engine's currently deployed capital (from ledger)
    against its allocated ceiling from ares_config.py.

    Returns:
        ok        (bool)  — True if engine may add a new position
        headroom  (float) — $ available under engine ceiling
        reason    (str)   — explanation for log

    Args:
        ledger       — Ares Ledger instance
        engine_id    — Engine to check ("A" through "F")
        total_equity — Current equity (defaults to TOTAL_EQUITY from config)
    """
    if total_equity is None:
        total_equity = TOTAL_EQUITY

    weight  = INITIAL_ENGINE_WEIGHTS.get(engine_id, 0.0)
    ceiling = total_equity * weight
    floor   = total_equity * ENGINE_CAPITAL_FLOOR

    deployed = ledger.get_engine_capital_deployed(engine_id)
    headroom = ceiling - deployed

    if ceiling == 0.0:
        return False, 0.0, f"Engine {engine_id} has 0% allocation — deferred or disabled"

    if deployed >= ceiling:
        return (
            False, 0.0,
            f"Engine {engine_id} at ceiling: deployed=${deployed:,.0f} >= ceiling=${ceiling:,.0f}"
        )

    if headroom < floor * 0.5:
        # Headroom too small for a meaningful position — less than half the floor
        return (
            False, headroom,
            f"Engine {engine_id} headroom ${headroom:,.0f} below minimum viable position"
        )

    logger.info(
        "ENGINE BUDGET %s: deployed=$%s  ceiling=$%s  headroom=$%s",
        engine_id, f"{deployed:,.0f}", f"{ceiling:,.0f}", f"{headroom:,.0f}"
    )
    return True, headroom, f"Engine {engine_id} headroom=${headroom:,.0f} available"


# ── Standalone snapshot ───────────────────────────────────────────────────────

def print_snapshot():
    from ares_config import ENGINE_STATUS
    from data_feeds import DataManager
    from config import CONFIG
    from ledger import Ledger

    dm     = DataManager(CONFIG)
    ledger = Ledger()

    account   = dm.alpaca.get_account()
    positions = dm.alpaca.get_positions()

    equity   = float(account.get("equity", 0))
    cash     = float(account.get("cash", 0))
    bp       = float(account.get("buying_power", 0))
    total_mv = sum(abs(float(p.get("qty", 0))) * float(p.get("current_price", 0)) for p in positions)
    util     = total_mv / equity if equity > 0 else 0
    on_margin = cash < 0

    print(f"\n{'='*60}")
    print(f"  ARES CAPITAL SNAPSHOT")
    print(f"{'='*60}")
    print(f"  Equity:         ${equity:>12,.2f}")
    print(f"  Cash:           ${cash:>12,.2f}  {'⚠ ON MARGIN' if on_margin else '✓'}")
    print(f"  Buying Power:   ${bp:>12,.2f}")
    print(f"  Market Value:   ${total_mv:>12,.2f}")
    print(f"  Utilization:    {util:>11.1%}  ", end="")

    if util > BLOCK_THRESHOLD:
        print("🔴 BLOCKED")
    elif util > REDUCE_THRESHOLD:
        print("🟠 REDUCED — max 1 new entry")
    elif util > WARN_THRESHOLD:
        print("🟡 WARNING — elevated")
    else:
        print("🟢 NORMAL")

    print(f"\n  Per-engine budget:")
    print(f"  {'Engine':<8} {'Status':<8} {'Allocation':>12} {'Deployed':>12} {'Headroom':>12} {'Gate'}")
    print(f"  {'-'*8} {'-'*8} {'-'*12} {'-'*12} {'-'*12} {'-'*8}")

    for eid in sorted(INITIAL_ENGINE_WEIGHTS.keys()):
        weight   = INITIAL_ENGINE_WEIGHTS[eid]
        ceiling  = equity * weight
        deployed = ledger.get_engine_capital_deployed(eid)
        headroom = ceiling - deployed
        status   = ENGINE_STATUS.get(eid, "off")
        ok, _, _ = check_engine_budget(ledger, eid, equity)
        gate     = "✓" if ok else "✗"
        print(f"  {eid:<8} {status:<8} ${ceiling:>11,.0f} ${deployed:>11,.0f} ${headroom:>11,.0f} {gate}")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    print_snapshot()
