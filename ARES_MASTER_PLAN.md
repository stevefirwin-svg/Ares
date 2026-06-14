# Ares — Master Priority Plan
*Last updated: 2026-06-14 (session 7 — yfinance, ADX fix, Engine F consolidation gate, logging pipeline)*
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
| RF-28 | HIGH | MATH | ADX Wilder smoothing used cumsum seed → values 300-500x too large | ✅ CLOSED 2026-06-14 — Wilder EMA fixed: mean seed + (prev*(n-1)+x)/n recurrence |
| RF-29 | HIGH | ARCH | Engine A `BARS_LOOKBACK=120` < 252-bar minimum requirement | ✅ CLOSED 2026-06-14 — `ENGINE_A_BARS_LOOKBACK=400` |
| RF-30 | HIGH | ARCH | Engine F `CONSOL_RANGE_ATR_MAX` compared total range to per-bar threshold → rejected 100% of universe | ✅ CLOSED 2026-06-14 — normalized to per-bar: `(range/ATR)/n_bars` |
| RF-31 | HIGH | ARCH | Engine B `submit_order` had `limit_price=None` hardcoded → 422 on every order | ✅ CLOSED 2026-06-14 — `limit_price` param added and wired |
| RF-32 | MED | ARCH | `BAR_FEED` missing from ares_config imports in engines A, B, C, F → NameError crash | ✅ CLOSED 2026-06-14 — added to all four |
| RF-33 | MED | ARCH | `def count_shadow_entries` definition missing in engine_a.py (body orphaned) → NameError | ✅ CLOSED 2026-06-14 — def line restored |
| RF-34 | MED | ARCH | hamilton_filter.py and outcome_tracker.py had no file logging → silent failures in Task Scheduler | ✅ CLOSED 2026-06-14 — FileHandler added to both |
| RF-35 | MED | ARCH | outcome_tracker `fetch_price_after` used Alpaca/IEX endpoint → same coverage gap as engines | ✅ CLOSED 2026-06-14 — switched to yfinance |

---

## System Identity

Ares is architecturally independent from Raptor. No composite score. No shared factor weights.
Six independent engines (A–F), each with its own math and exit logic.
Paper account ~$50K equity via Alpaca Markets.

Bar data: **yfinance** (daily OHLCV, all 141 universe symbols, 5yr history).
Orders: **Alpaca paper API** (limit orders, day TIF).
Account state: **Alpaca paper API**.

Engines compete for symbols via `symbol_ownership.json`. First engine to claim wins.

---

## Current System State (2026-06-14 — Session 7)

| Component | Status |
|-----------|--------|
| All 5 engines (A,B,C,E,F) | ✅ LIVE — paper trading |
| Bar data source | ✅ yfinance (all 141 symbols) |
| Task Scheduler | ✅ All 12 tasks configured |
| Log pipeline | ✅ All scripts write to logs/YYYYMMDD.log |
| Engine E | 27/30 shadows — 3 from calibration gate |
| Engine A | 6/30 shadows |
| Engine B | 3/30 shadows (first live trade: ASTS today) |
| Engine C | 1/30 shadows |
| Engine F | 0/30 shadows (consolidation gate was broken — fixed today) |
| EVT calibration | RF-7 OPEN — fallback stops in use |

### Open positions
| Symbol | Engine | Entry date | Entry | Stop | Hold |
|--------|--------|-----------|-------|------|------|
| CSCO | E | 2026-06-03 | $125.97 | $114.56 | 20d |
| IONQ | E | 2026-06-03 | $70.05 | $55.38 | 20d |
| ASTS | B | 2026-06-14 | $82.43 | $55.02 | 8d |

---

## OPEN ITEMS (not gated on trade accumulation)

| Priority | Item | Action |
|----------|------|--------|
| HIGH | RF-7: EVT fallback stops | Run `python evt_calibrator.py` — all engines on 2.0× ATR fallback |
| MED | ASTS stop too wide (-33%) | Engine B stop for high-volatility names is effectively the time stop (8d); EVT fix will improve this |
| MED | macro_context.json stale (14d) | hamilton_filter.py Task Scheduler task may not be running — verify Monday |
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

WAITING ON DATA (gate: ≥ 30 closed trades per engine):
  Sprint 5 execution — run statistical_validation.py, ares_threshold_deriver.py
  Sprint 5-d — re-run evt_calibrator.py (RF-7)
  Sprint 5-b — activate capital_allocator.py advisory recommendations

WHEN ≥ 30 closed trades per engine:
  Sprint 6 — Validation and go-live at full Kelly
```

---

## FILE INVENTORY (session 7)

| File | Purpose | Status |
|------|---------|--------|
| ares_config.py | All Ares constants | ✓ |
| ledger.py | engine_id keyed position ledger | ✓ |
| symbol_ownership.json | Cross-engine symbol lock | ✓ |
| macro_context.py | Signal fetcher backup | ✓ |
| hamilton_filter.py | HMM macro regime | ✓ file logging added |
| margin_guard.py | Per-engine budget enforcement | ✓ |
| outcome_tracker.py | Trade tagging + forward returns | ✓ yfinance + file logging |
| capital_allocator.py | Advisory weight calculator | ✓ |
| daily_recap.py | Per-engine P&L summary | ✓ INFO-level file logging |
| evt_calibrator.py | GPD/EVT stop derivation | ✓ fallback active (RF-7 open) |
| ares_universe.py | Universe builder | ✓ 141 symbols |
| engine_a.py | Momentum Continuation | ✓ LIVE — yfinance, ADX fixed, 400-bar |
| exit_monitor_a.py | Engine A exits | ✓ 6 conditions |
| hold_monitor_a.py | Engine A health | ✓ |
| engine_b.py | Mean Reversion | ✓ LIVE — yfinance, limit_price fixed |
| exit_monitor_b.py | Engine B exits | ✓ 5 conditions |
| hold_monitor_b.py | Engine B health | ✓ |
| engine_c.py | Squeeze Break | ✓ LIVE — yfinance |
| exit_monitor_c.py | Engine C exits | ✓ 4 conditions |
| hold_monitor_c.py | Engine C health | ✓ |
| engine_e.py | RS Rotation | ✓ LIVE — yfinance |
| exit_monitor_e.py | Engine E exits | ✓ |
| hold_monitor_e.py | Engine E health | ✓ |
| engine_f.py | Structural Breakout | ✓ LIVE — yfinance, consolidation gate fixed |
| exit_monitor_f.py | Engine F exits | ✓ 5 conditions + partial trim |
| hold_monitor_f.py | Engine F health | ✓ |
| ares_exit_monitor.py | Engine-dispatched exit monitor | ✓ A,B,C,E,F wired |
| ares_hold_monitor.py | Engine-dispatched hold monitor | ✓ A,B,C,E,F wired |
| ares_watchdog.py | Intraday hard stop + circuit breaker | ✓ |
| ares_diagnose.py | System health diagnostic | ✓ new session 7 |
| statistical_validation.py | IC + FDR + Ledoit-Wolf | ✓ |
| ares_signal_audit.py | Signal distribution audit | ✓ |
| ares_threshold_deriver.py | Derives TODO:DERIVE constants | ✓ |
| sub_engine_params.json | Per-engine signal weights | ✓ bootstrap equal |
| evt_calibration.json | Stop multiplier file | ✓ fallback active |
| universe.json | Daily symbol list | ✓ 141 symbols |
| macro_context.json | Current macro regime | ✓ HMM — verify Monday |
| shadow_classifications.json | Shadow mode log | ✓ |
| sub_engine_outcomes.json | Per-engine trade history | ✓ |
