# Ares — Session 8 Audit & Fix Log
*Session date: 2026-06-19 | Scope: data reconciliation, Alpaca vs ledger sync, order type, ic_valid, recap pipeline*

---

## Summary

Full reconciliation pass triggered by inaccurate daily recap email. Found and fixed 6 distinct bugs across the data pipeline, order submission, and position tracking layers. Most critical: all 5 engines were using DAY LIMIT orders which expired unfilled, leaving ghost ledger entries and ultimately creating accidental short positions in Alpaca.

---

## Findings & Fixes

### RF-36 — CRITICAL / ARCH — All 5 engines used DAY LIMIT orders

**Finding:** Every engine submitted `type=limit, time_in_force=day` at `price * 1.001` (0.1% above prior close). If the stock opened above that level, the order expired at EOD unfilled. The engine checked `order.get("id")` — which Alpaca returns for an *accepted* order — and immediately called `ledger.record_entry()`. But the order never filled. Result: ledger showed positions open; Alpaca had nothing.

When `ares_exit_monitor.py` later ran, it called `submit_sell()` on shares Alpaca never held, creating accidental short positions. Three shorts confirmed: CIFR (-19), INTC (-12), MRVL (-3).

**Fix:** Switched all 5 engines (`engine_a/b/c/e/f.py`) from `type=limit` to `type=market`. Market orders fill immediately at open. The `limit_price` parameter is still computed but no longer included in the order body (no behavioral change to sizing math).

**Verified:** `grep -n '"type":' engine_*.py` → all show `"market"`.

**Ghost positions cleaned:**
- ASTS (B, June 14): voided from ledger at entry price
- XLF (C, June 15): voided from ledger at entry price
- C (A, June 19): voided from ledger at entry price
- CIFR (E): buy-to-cover order placed successfully (order d39d8e75, filled before market close)
- INTC (-12) + MRVL (-3): market closed, to be covered Monday 9:30 AM

---

### RF-37 — HIGH / ARCH — ares_exit_monitor.py still used IEX bar fetch

**Finding:** The exit monitor's `fetch_bars()` function was unchanged from the original IEX implementation (`feed=iex`, Alpaca data endpoint). IEX covers ~2-3% of consolidated tape on paper accounts. Symbols not covered by IEX returned no bars → exit monitor skipped them silently → exits never fired for those positions.

This was fixed in all scan engines in session 7 but not propagated to the exit monitor.

**Fix:** Rewrote `fetch_bars()` in `ares_exit_monitor.py` to use `yfinance.download()` with per-symbol fallback, matching the pattern used in all scan engines.

**Verified:** `grep -n "yf.download" ares_exit_monitor.py` → present.

---

### RF-38 — HIGH / MATH — trend_exhaustion and rs_lost missing from TERMINAL_EXIT_PATHS

**Finding:** `outcome_tracker.py` determines `ic_valid` by checking if `exit_reason` matches any label in `TERMINAL_EXIT_PATHS`. The set contained Engine B/E/F labels but was missing `trend_exhaustion` (Engine A's primary exit) and `rs_lost` (Engine E). Result: all 7 Engine A trades and 1 Engine E trade had `ic_valid=False`, meaning the IC gate counter showed 2/30 valid trades system-wide when it should have shown 10/30.

**Fix:** Added both labels to `TERMINAL_EXIT_PATHS` in `outcome_tracker.py`. Backfilled `ic_valid=True` on all 8 affected records in `sub_engine_outcomes.json`.

**Verified:** Post-fix ic_valid summary: Engine A 7/7, Engine E 3/3.

---

### RF-39 — MED / HYGIENE — Daily recap macro cards all showing "—"

**Finding:** `build_macro_section()` in `daily_recap.py` read `sigs.get("vix", {})`, `sigs.get("spy_trend", {})`, etc., expecting nested dict values. But `macro_context.json` stores signals as a flat dict with keys `vix_level`, `spy_ret_20d_pct`, `credit_norm`, `breadth_z`. Every macro card in the recap email was blank.

**Fix:** Rewrote the section to read the actual flat schema keys and derive sub-labels (LOW/ELEVATED/HIGH for VIX, UPTREND/FLAT/DOWNTREND for SPY, etc.) from the raw values.

**Verified:** `macro_context.json` signals dict confirmed flat; new code reads `sigs.get("vix_level")` etc.

---

### RF-40 — MED / HYGIENE — Stale symbol_ownership.json locks

**Finding:** `symbol_ownership.json` contained `{"INTC": "F", "TSM": "F"}` with no corresponding open positions in the ledger for Engine F. These were stale locks from exits that didn't clean up ownership.

**Fix:** Removed stale entries. Cleaned state: `{ASTS: B, XLF: C, APLD: E, CIFR: E, C: A}`. Then further cleaned after position sync reduced open positions to just APLD.

`ares_reconcile.py --fix` now auto-detects and removes stale ownership entries on every run.

---

### RF-41 — HIGH / ARCH — No daily Alpaca vs ledger reconciliation

**Finding:** There was no tool to compare live Alpaca positions against the ledger. Divergences accumulated silently for days (ASTS since June 14, XLF since June 15).

**Fix:** Built two new tools:

**`ares_reconcile.py`** — 8-check data integrity audit:
- Ownership vs ledger consistency
- Ledger closed trades vs sub_engine_outcomes.json
- P&L cross-check (dollar-level agreement)
- ic_valid flag integrity
- Same-day round-trip detection
- Forward return due-date status
- Optional Alpaca live position comparison (`--alpaca` flag)
- Auto-fix mode (`--fix`) for stale ownership and ic_valid errors

**`ares_position_sync.py`** — Alpaca vs ledger sync tool:
- Detects positions in Alpaca not in ledger (ghost entries or re-entries)
- Detects positions in ledger not in Alpaca (unfilled orders)
- Detects and covers accidental short positions
- `--close-shorts`: submit buy-to-cover for any short positions
- `--fix`: execute all corrective actions
- `--void-ghosts`: EOD mode — only close ledger entries with no Alpaca position

**`Ares_PositionSync_EOD` Task Scheduler task** added at 4:05 PM ET daily, running `ares_position_sync.py --void-ghosts`. This catches any unfilled orders (now impossible with market orders, but provides a clean backstop) before the 4:20 PM recap.

---

## Session 8 — Files Changed

| File | Change |
|------|--------|
| `engine_a.py` | DAY LIMIT → MARKET order |
| `engine_b.py` | DAY LIMIT → MARKET order |
| `engine_c.py` | DAY LIMIT → MARKET order |
| `engine_e.py` | DAY LIMIT → MARKET order |
| `engine_f.py` | DAY LIMIT → MARKET order |
| `ares_exit_monitor.py` | IEX bar fetch → yfinance |
| `outcome_tracker.py` | Added `trend_exhaustion`, `rs_lost` to TERMINAL_EXIT_PATHS |
| `daily_recap.py` | Fixed macro signal keys + added ledger↔outcomes reconciliation warning |
| `sub_engine_outcomes.json` | Backfilled `ic_valid=True` on 8 trades |
| `symbol_ownership.json` | Removed stale INTC/TSM Engine F locks |
| `ares_reconcile.py` | NEW — daily 8-check data integrity audit |
| `ares_position_sync.py` | NEW — Alpaca vs ledger sync with short-cover |
| `Add-Ares_PositionSync_EOD.ps1` | NEW — Task Scheduler registration script |

---

## State at Session End

**Ledger:** 1 open position (APLD, Engine E). All ghost entries voided.

**Alpaca:** APLD +10 confirmed. CIFR covered. INTC -12 and MRVL -3 shorts remain — to be covered Monday 9:30 AM.

**Outcomes:** 10 closed trades, all reconciled. ic_valid: A=7/7, E=3/3.

**ic_valid gate progress:** 10/30 system-wide (vs 2/30 before fix).

**Monday morning procedure:**
```powershell
python ares_position_sync.py --close-shorts   # cover INTC + MRVL at open
python ares_reconcile.py --alpaca             # confirm clean
```
