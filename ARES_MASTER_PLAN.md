# Ares — Master Priority Plan
*Last updated: 2026-06-19 (session 8 — reconciliation pipeline, position sync, market order fix, ic_valid fix)*
*Source of truth: GitHub (stevefirwin-svg/Ares) + live code audit*

---

## The Standard

Every number must be derivable from a formula, empirical data, or an optimization.
If "why that number?" cannot be answered, the number is wrong.

**Data integrity corollary:** Real data or skip. Never fabricate a fallback value that looks real.

**Audit integrity corollary:** A fix is only DONE when grep/test output is pasted in the same
session confirming it. Documented-but-unverified fixes are NOT done.

---

## RED-FLAG REGISTER

| ID | Sev | Cat | One-line finding | Status |
|----|-----|-----|------------------|--------|
| RF-1 | CRITICAL | MATH | `forward_return_Nd` measured from EXIT not ENTRY+N | ✅ CLOSED 2026-05-30 |
| RF-2 | CRITICAL | MATH | Blueprint §12.2 used `forward_pnl_pct` | ✅ CLOSED 2026-05-31 |
| RF-3 | CRITICAL | MATH | IC>0.05 @ n=30 statistically powerless | ✅ MITIGATED 2026-05-31 — demoted to sign screen |
| RF-4 | HIGH | MATH | HMM posterior near-one-hot → cliff switching | ✅ CLOSED 2026-05-31 |
| RF-5 | MED | ARCH | Breadth computed but never fed to HMM | ✅ CLOSED 2026-05-31 |
| RF-6 | MED | MATH | State→regime labels sorted on VIX alone | ✅ CLOSED 2026-05-31 |
| RF-7 | MED | MATH | All EVT stops are round-number fallbacks | OPEN — re-run `evt_calibrator.py` |
| RF-8 | MED | ARCH | 100% capital deployed, no cash floor | ✅ CLOSED 2026-05-31 |
| RF-9 | MED | MATH | Shadow samples single-regime | ✅ MITIGATED 2026-05-31 |
| RF-10 | MED | ARCH | Ownership lock creates IC selection bias | ✅ CLOSED 2026-05-31 |
| RF-11 | LOW | MATH | Engine E IC horizon < hold target | ✅ CLOSED 2026-05-31 (accepted) |
| RF-12 | HIGH | MATH | Forward-return horizon biased +40% | ✅ CLOSED 2026-05-31 |
| RF-13 | HIGH | MATH | Kelly clip floor overrides bootstrap sizing | ✅ CLOSED 2026-05-31 |
| RF-14 | CRITICAL | ARCH | Calibrated weights written but never consumed | ✅ CLOSED 2026-05-31 |
| RF-15 | HIGH | ARCH | Shadow forward returns never backfilled | ✅ CLOSED 2026-05-31 |
| RF-16 | MED | ARCH | Macro gating inconsistent across engines | ✅ CLOSED 2026-05-31 |
| RF-17 | MED | MATH | LW covariance had spurious mu²p term | ✅ CLOSED 2026-05-31 |
| RF-18 | MED | HYGIENE | Engine E signal name mismatch | ✅ CLOSED 2026-05-31 |
| RF-19 | HIGH | ARCH | Engines fabricate equity on API failure | ✅ CLOSED 2026-05-31 |
| RF-20 | MED | ARCH | Leveraged/inverse ETFs in universe | ✅ CLOSED 2026-05-31 |
| RF-21 | HYGIENE | HYGIENE | Config drift — two filter constant sets | ✅ CLOSED 2026-05-31 |
| RF-22 | HYGIENE | HYGIENE | IEX feed hardcoded everywhere | ✅ CLOSED 2026-06-14 — all engines switched to yfinance |
| RF-23 | HYGIENE | HYGIENE | Raptor files coexist in Ares directory | ✅ MITIGATED 2026-05-31 |
| RF-24 | HYGIENE | MATH | Ledger multi-trim PnL reflects only final tranche | ✅ CLOSED 2026-05-31 |
| RF-25 | MATH | MATH | BH-FDR applied per-engine not system-wide | ✅ CLOSED 2026-05-31 |
| RF-26 | MATH | MATH | DSR n_trials=1 always | ✅ CLOSED 2026-05-31 |
| RF-27 | HYGIENE | HYGIENE | Dead `if False` branch in GPD quantile | ✅ CLOSED 2026-05-31 |
| RF-28 | HIGH | MATH | ADX Wilder smoothing used cumsum seed → values 300-500x too large | ✅ CLOSED 2026-06-14 |
| RF-29 | HIGH | ARCH | Engine A `BARS_LOOKBACK=120` < 252-bar minimum | ✅ CLOSED 2026-06-14 — `ENGINE_A_BARS_LOOKBACK=400` |
| RF-30 | HIGH | ARCH | Engine F consolidation gate rejected 100% of universe | ✅ CLOSED 2026-06-14 — normalized to per-bar |
| RF-31 | HIGH | ARCH | Engine B `submit_order` had `limit_price=None` → 422 on every order | ✅ CLOSED 2026-06-14 |
| RF-32 | MED | ARCH | `BAR_FEED` missing from imports in engines A,B,C,F → NameError crash | ✅ CLOSED 2026-06-14 |
| RF-33 | MED | ARCH | `def count_shadow_entries` missing in engine_a.py → NameError | ✅ CLOSED 2026-06-14 |
| RF-34 | MED | ARCH | hamilton_filter.py / outcome_tracker.py had no file logging → silent Task Scheduler failures | ✅ CLOSED 2026-06-14 |
| RF-35 | MED | ARCH | outcome_tracker `fetch_price_after` used Alpaca/IEX → coverage gaps | ✅ CLOSED 2026-06-14 — yfinance |
| RF-36 | CRITICAL | ARCH | All 5 engines used DAY LIMIT orders — unfilled orders wrote ghost ledger entries, causing accidental short positions in Alpaca | ✅ CLOSED 2026-06-19 — switched all engines to MARKET orders |
| RF-37 | HIGH | ARCH | `ares_exit_monitor.py` still used IEX bar fetch — skipped symbols with no IEX coverage, missing exits | ✅ CLOSED 2026-06-19 — switched to yfinance |
| RF-38 | HIGH | MATH | `trend_exhaustion` and `rs_lost` missing from `TERMINAL_EXIT_PATHS` → all Engine A trades and 1 Engine E trade had `ic_valid=False` incorrectly | ✅ CLOSED 2026-06-19 — added to set, 8 existing records backfilled |
| RF-39 | MED | HYGIENE | `daily_recap.py` macro signal cards showed "—" — `build_macro_section()` read nested dict keys that don't exist in `macro_context.json` flat schema | ✅ CLOSED 2026-06-19 — fixed to read `vix_level`, `spy_ret_20d_pct`, `credit_norm`, `breadth_z` |
| RF-40 | MED | HYGIENE | `symbol_ownership.json` had stale INTC/TSM locks (Engine F) with no open positions | ✅ CLOSED 2026-06-19 — cleaned up; `ares_reconcile.py --fix` now auto-removes stale locks |
| RF-41 | HIGH | ARCH | No daily Alpaca vs ledger reconciliation — divergences accumulated silently for days | ✅ CLOSED 2026-06-19 — `ares_reconcile.py` + `ares_position_sync.py` added; `Ares_PositionSync_EOD` task scheduled at 4:05 PM |

---

## System Identity

Ares is architecturally independent from Raptor. No composite score. No shared factor weights.
Six independent engines (A–F), each with its own math and exit logic.
Paper account ~$50K equity via Alpaca Markets.

Bar data: **yfinance** (daily OHLCV, all 141 universe symbols, 5yr history).
Orders: **Alpaca paper API** (market orders, day TIF).
Account state: **Alpaca paper API**.

Engines compete for symbols via `symbol_ownership.json`. First engine to claim wins.

---

## Current System State (2026-06-19 — Session 8)

| Component | Status |
|-----------|--------|
| All 5 engines (A,B,C,E,F) | ✅ LIVE — paper trading, market orders |
| Bar data source | ✅ yfinance (all engines + exit monitor) |
| Task Scheduler | ✅ 13 tasks configured (added Ares_PositionSync_EOD) |
| Log pipeline | ✅ All scripts write to logs/YYYYMMDD.log |
| Reconciliation pipeline | ✅ NEW — ares_reconcile.py + ares_position_sync.py |
| ic_valid counts | ✅ Fixed — A: 7/10, E: 3/10 now correct |
| Daily recap macro cards | ✅ Fixed — VIX/SPY/Credit/Breadth now display correctly |

### Open positions (as of 2026-06-19 EOD)
| Symbol | Engine | Entry date | Entry Price | Stop | Hold |
|--------|--------|-----------|-------------|------|------|
| APLD | E | 2026-06-18 | $45.57 | $33.93 | 20d |

### Confirmed shorts to cover Monday 9:30 AM
| Symbol | Qty Short | Action |
|--------|-----------|--------|
| INTC | -12 | `python ares_position_sync.py --close-shorts` |
| MRVL | -3 | same command covers both |

### Closed trade summary (10 trades)
| Engine | Trades | ic_valid | Net P&L |
|--------|--------|----------|---------|
| A | 7 | 7 | +$128.62 |
| E | 3 | 3 | -$93.26 |
| B | 0 | 0 | — |
| C | 0 | 0 | — |
| F | 0 | 0 | — |

---

## OPEN ITEMS (not gated on trade accumulation)

| Priority | Item | Action |
|----------|------|--------|
| CRITICAL | Cover INTC (-12) and MRVL (-3) shorts | Monday 9:30 AM: `python ares_position_sync.py --close-shorts` |
| HIGH | RF-7: EVT fallback stops | Run `python evt_calibrator.py` — all engines on ATR fallback |
| MED | Verify CIFR cover (order d39d8e75) actually filled | Monday: `python ares_position_sync.py --alpaca` |
| LOW | Raptor files in Ares directory | Move to `raptor/` subdir when convenient |

---

## ARCHITECTURE — Gated on Data

| ID | Description | Gate |
|----|-------------|------|
| ARCH-1 | IC-weighted signal weights per engine | 30+ closed trades per engine |
| ARCH-2 | Capital reallocation advisory go-live | 15+ closed trades per engine |
| ARCH-3 | Engine D intraday runtime | Explicitly deferred |
| ARCH-4 | Engine-specific Kelly (replace bootstrap) | 30+ closed trades per engine |
| ARCH-5 | Cross-engine IC correlation analysis | 100+ total closed trades |

---

## BUILD ORDER SUMMARY

```
COMPLETE:
  Sprint 0 — Plumbing ✓
  Sprint 1 — Foundation data layer ✓
  Sprint 2 — Engine B (live) ✓
  Sprint 3 — Engines A and F (live) ✓
  Sprint 4 — Engines C and E (live) ✓
  Sprint 5 tools — statistical_validation.py, ares_signal_audit.py,
                   ares_threshold_deriver.py, ares_watchdog.py ✓
  Session 7 — yfinance migration, ADX fix, Engine F gate fix,
               full logging pipeline, Task Scheduler ready ✓
  Session 8 — reconciliation pipeline, position sync, market order fix,
               ic_valid fix, macro recap fix, EOD ghost-void task ✓

WAITING ON DATA (gate: ≥ 30 closed trades per engine):
  Sprint 5 execution — run statistical_validation.py, ares_threshold_deriver.py
  Sprint 5-d — re-run evt_calibrator.py (RF-7)
  Sprint 5-b — activate capital_allocator.py advisory recommendations

WHEN ≥ 30 closed trades per engine:
  Sprint 6 — Validation and go-live at full Kelly
```

---

## FILE INVENTORY (session 8)

| File | Purpose | Status |
|------|---------|--------|
| ares_config.py | All Ares constants | ✓ |
| ledger.py | engine_id keyed position ledger | ✓ |
| symbol_ownership.json | Cross-engine symbol lock | ✓ cleaned session 8 |
| macro_context.py | Signal fetcher backup | ✓ |
| hamilton_filter.py | HMM macro regime | ✓ file logging |
| margin_guard.py | Per-engine budget enforcement | ✓ |
| outcome_tracker.py | Trade tagging + forward returns | ✓ TERMINAL_EXIT_PATHS fixed session 8 |
| capital_allocator.py | Advisory weight calculator | ✓ |
| daily_recap.py | Per-engine P&L summary | ✓ macro cards fixed session 8 |
| evt_calibrator.py | GPD/EVT stop derivation | ✓ fallback active (RF-7 open) |
| ares_universe.py | Universe builder | ✓ 141 symbols |
| engine_a.py | Momentum Continuation | ✓ LIVE — market orders session 8 |
| exit_monitor_a.py | Engine A exits | ✓ 6 conditions |
| hold_monitor_a.py | Engine A health | ✓ |
| engine_b.py | Mean Reversion | ✓ LIVE — market orders session 8 |
| exit_monitor_b.py | Engine B exits | ✓ 5 conditions |
| hold_monitor_b.py | Engine B health | ✓ |
| engine_c.py | Squeeze Break | ✓ LIVE — market orders session 8 |
| exit_monitor_c.py | Engine C exits | ✓ 4 conditions |
| hold_monitor_c.py | Engine C health | ✓ |
| engine_e.py | RS Rotation | ✓ LIVE — market orders session 8 |
| exit_monitor_e.py | Engine E exits | ✓ |
| hold_monitor_e.py | Engine E health | ✓ |
| engine_f.py | Structural Breakout | ✓ LIVE — market orders session 8 |
| exit_monitor_f.py | Engine F exits | ✓ 5 conditions + partial trim |
| hold_monitor_f.py | Engine F health | ✓ |
| ares_exit_monitor.py | Engine-dispatched exit monitor | ✓ yfinance bars fixed session 8 |
| ares_hold_monitor.py | Engine-dispatched hold monitor | ✓ A,B,C,E,F wired |
| ares_watchdog.py | Intraday hard stop + circuit breaker | ✓ |
| ares_diagnose.py | System health diagnostic | ✓ |
| ares_reconcile.py | Daily data integrity audit | ✓ NEW session 8 |
| ares_position_sync.py | Alpaca vs ledger sync + short cover | ✓ NEW session 8 |
| statistical_validation.py | IC + FDR + Ledoit-Wolf | ✓ |
| ares_signal_audit.py | Signal distribution audit | ✓ |
| ares_threshold_deriver.py | Derives TODO:DERIVE constants | ✓ |
| sub_engine_params.json | Per-engine signal weights | ✓ bootstrap equal |
| evt_calibration.json | Stop multiplier file | ✓ fallback active |
| universe.json | Daily symbol list | ✓ 141 symbols |
| macro_context.json | Current macro regime | ✓ NEUTRAL as of 2026-06-19 |
| shadow_classifications.json | Shadow mode log | ✓ |
| sub_engine_outcomes.json | Per-engine trade history | ✓ ic_valid backfilled session 8 |
| symbol_ownership.json | Cross-engine symbol lock | ✓ stale locks cleaned session 8 |
