# Ares — Master Priority Plan
*Last updated: 2026-05-29 (session 1 — initial architecture)*
*Source of truth: GitHub (stevefirwin-svg/Ares) + live code audit*

---

## The Standard

Every number must be derivable from a formula, empirical data, or an optimization.
If "why that number?" cannot be answered, the number is wrong.

**Data integrity corollary:** Real data or skip. Never fabricate a fallback value that looks real.

**Audit integrity corollary:** A fix is only DONE when grep/test output is pasted in the same
session confirming it. Documented-but-unverified fixes are NOT done.

---

## System Identity

Ares is architecturally independent from Raptor. No composite score. No shared factor weights.
No routing classifier. Six independent engines, each with its own math and exit logic.
Separate Alpaca paper account (~$50K equity).

Engines compete for symbols via `symbol_ownership.json`. First engine to claim a symbol wins.
Capital starts equal ($10K per engine). Reallocation is advisory only, gated on trade history.

---

## Current System State (2026-05-29 — Day 1)

| Component | Status |
|-----------|--------|
| Repo | Initialized — cloned from Raptor infrastructure base |
| ARES_SKILL.md | DONE 2026-05-29 |
| ARES_MASTER_PLAN.md | DONE 2026-05-29 |
| ARES_STARTUP.md | DONE 2026-05-29 |
| ares_config.py | NOT BUILT — Sprint 0 |
| symbol_ownership.json | NOT BUILT — Sprint 0 |
| All engines (A–F) | NOT BUILT |
| capital_allocator.py | NOT BUILT — Sprint 1 |
| Shadow mode | NOT RUNNING |
| Live capital | NOT DEPLOYED |

---

## CATEGORY DEFINITIONS

| Symbol | Meaning | When to act |
|--------|---------|-------------|
| MATH | Static where dynamic required, or misspecified formula | Next session |
| ARCH | Correct direction, premature at current data scale | When data gate met |
| HYGIENE | Dead code, fragile I/O, missing instrumentation | Rolling |
| SPRINT | Build item — sequenced by dependency | Per sprint plan |

---

## OPEN — SPRINT 0 (Plumbing — nothing works without this)

All Sprint 0 items are blockers. No engine can run until these are done and tested.

| ID | Item | File | Notes |
|----|------|------|-------|
| S0-a | ares_config.py | NEW | Separate Alpaca keys, $50K equity, per-engine caps, IC horizons |
| S0-b | symbol_ownership lock | symbol_ownership.json + ledger.py | Cross-engine lock; owns on entry, releases on full exit |
| S0-c | Unified sizing formula | Each engine_X.py | Kelly + cap + Engine F structural variant |
| S0-d | ledger.py | MODIFY | engine_id keyed; Ares Alpaca account credentials |
| S0-e | macro_context.py | MODIFY | Strip FRED (yield curve, fed funds); keep SPY/VIX/credit/breadth |
| S0-f | margin_guard.py | MODIFY | Per-engine budget tracking, not portfolio-level only |
| S0-g | sub_engine_outcomes.json | NEW | Structure: engine_id, signals at entry, realized pnl, forward_return_Nd |
| S0-h | shadow_classifications.json | NEW | Log every would-be entry without trading (gate for live capital) |
| S0-i | EVT stop calibration | NEW: evt_calibrator.py | GPD fit to left tail of universe returns; replace all TODO:DERIVE stop multipliers before any engine enters shadow |

---

## OPEN — SPRINT 1 (Foundation data layer)

| ID | Item | File | Gate |
|----|------|------|------|
| S1-a | outcome_tracker.py | MODIFY | engine_id field mandatory; per-engine summary |
| S1-b | capital_allocator.py | NEW | Advisory weight calculator; requires S0 complete |
| S1-c | daily_recap.py | MODIFY | Per-engine P&L table, per-engine IC, capital deployment |
| S1-d | Shadow mode wiring | All engines | Classify but do not trade; write to shadow_classifications.json |
| S1-e | Hamilton Filter macro_context.py | MODIFY | hmmlearn HMM replaces vote-count; output = P(regime) vector not discrete label; inputs: SPY trend, VIX, credit spread, sector breadth (no FRED) |

---

## OPEN — SPRINT 2 (Engine B — first engine, lowest complexity)

Gate: Sprint 0 complete and tested.

| ID | Item | Notes |
|----|------|-------|
| S2-a | engine_b.py | MR classification gate: MR dominant + TREND < 0 + crowd_panic |
| S2-b | Engine B signals | b_avwap_distance, b_stoch_divergence, b_volume_climax |
| S2-c | Engine B OU theta | 30-day rolling OLS on log-price; hold_target = OU half-life × 1.5 |
| S2-d | Engine B exit logic | exit_monitor.py engine B branch: AVWAP reached / time stop / hard stop |
| S2-e | Engine B hold monitor | hold_monitor.py engine B branch: MR-weighted layers |
| S2-f | Engine B shadow | Run ≥ 30 shadow classifications before live capital |

---

## OPEN — SPRINT 3 (Engines A and F)

Gate: Sprint 2 shadow complete.

| ID | Item | Notes |
|----|------|-------|
| S3-a | engine_a.py | Momentum gate: TREND dominant + VOL confirming + micro=TRENDING |
| S3-b | Engine A signals | a_mom_12_1, a_hi52_proximity, a_roc_accel |
| S3-c | engine_f.py | Breakout gate: consolidation detected + volume confirm + no overhead HVN |
| S3-d | Engine F signals | f_consol_quality, f_breakout_strength, f_overhead_clearance, f_vol_declining |
| S3-e | Engine F structural stop | stop = consol_low − 0.1×ATR; sizing = risk_budget / (entry - stop) |
| S3-f | Engine F measured move | breakout_level + base_height; stored in entry_metadata |
| S3-g | A and F exit + hold branches | exit_monitor.py + hold_monitor.py engine branches |

---

## OPEN — SPRINT 4 (Engines C and E)

Gate: Sprint 3 shadow complete.

| ID | Item | Notes |
|----|------|-------|
| S4-a | engine_c.py | Squeeze gate: VOLAT dominant + currently_squeezing + vol returning |
| S4-b | Engine C signals | c_squeeze_duration, c_hv_ratio, c_momentum_direction |
| S4-c | Engine C phase state | squeeze_firing → break_confirmed; mutable position state file |
| S4-d | engine_e.py | RS gate: stock_rs_vs_sector + sector_rs_spy both above threshold |
| S4-e | Engine E signals | e_rs_20d, e_rs_60d, e_sector_rs_spy, e_rs_acceleration, e_new_rs_high |
| S4-f | Sector ETF bars | data_feeds.py: get_sector_bars() for XLK, XLF, XLV, XLE, XLY, XLI, XLB, XLC, XLU, XLRE, XLP |
| S4-g | C and E exit + hold branches | exit_monitor.py + hold_monitor.py engine branches |

---

## OPEN — SPRINT 5 (Calibration)

Gate: ≥ 30 closed trades per engine.

| ID | Item | Notes |
|----|------|-------|
| S5-a | IC-weighted signal weights | Per engine; Spearman IC vs forward_return_Nd |
| S5-b | Ledoit-Wolf shrinkage | BUNDLED with S5-a — must not be separated; shrinks IC estimates toward equal weights; α = f(sample_size); prevents noise amplification at 30-trade sample size |
| S5-c | RMT covariance cleaning | BUNDLED with S5-a — noise eigenvalues zeroed via Marchenko-Pastur threshold before IC weighting; factors sharing noise eigenspace grouped first |
| S5-d | capital_allocator.py go-live | Advisory recommendations using rolling Sharpe + IC |
| S5-e | Threshold empirical derivation | Replace all remaining TODO:DERIVE constants with bootstrapped values |

---

## OPEN — SPRINT 6 (Validation and go-live)

| ID | Item | Notes |
|----|------|-------|
| S6-a | Shadow mode validation | 2 weeks per engine; verify classification frequency 2–8% of universe |
| S6-b | Signal distribution check | std > 0, NaN < 20%, cross-sectional mean ≈ 0 |
| S6-c | Priority conflict check | Measure A/E overlap in shadow; adjust if > 30% |
| S6-d | Live capital at half-Kelly | All engines start at 50% of bootstrap Kelly until 30 trades |

---

## OPEN — ENGINE D (Deferred — separate track)

Engine D requires an intraday runtime that does not exist:
- Minute bar data feed
- 9:30–11:30 AM monitoring loop
- Same-day forced exit mechanism

Until built: Engine D's $10K allocation stays in cash. Do not adapt to daily bars without
separate validation — daily-bar gap reversion has materially worse statistics than intraday.

| ID | Item | Gate |
|----|------|------|
| D-1 | Intraday data feed (minute bars from Alpaca) | When explicitly prioritized |
| D-2 | engine_d.py + standalone loop | After D-1 |
| D-3 | Engine D shadow validation | After D-2 |

---

## ARBITRARY CONSTANTS — Must Derive

These are currently round numbers or educated guesses. All flagged as TODO:DERIVE.

| Engine | Constant | How to derive |
|--------|---------|---------------|
| A | stop_mult range 2.8–3.5 | EVT tail on Engine A closed trade returns |
| A | trail_scale 1.0–1.4 | Backtest trail width sensitivity |
| B | stop_mult range 2.0–2.5 | EVT tail on Engine B closed trade returns |
| B | OU theta estimation window 30d | Granger causality test on mean-reversion speed |
| C | stop_mult range 1.5–1.8 | Binary outcome analysis of squeeze fires |
| C | Phase 1 time limit 3 bars | Empirical distribution of squeeze resolution time |
| E | RS threshold 70th pctile | Rolling distribution of universe RS scores |
| E | stop_mult 3.0 | EVT tail on rotation trades |
| F | breakout volume threshold 1.8× | 70th pctile of breakout volume distribution in universe |
| F | overhead HVN clearance 2.0 ATR | Fill-rate analysis vs HVN distance |
| All | IC horizon A/B/C/F=10d, E=20d | Validated against Jegadeesh-Titman, Moskowitz-Grinblatt |
| All | Dominance threshold 0.42 | Bootstrap from first 30 trading days of shadow mode |
| All | Capital floor 10% | Minimum viable position size at $50K equity |

---

## SIGNAL LIFECYCLE

New signals can be proposed and added at any time, but must pass formal gates:

```
PROPOSED → SHADOW → LIVE → DEPRECATED

PROPOSED → SHADOW:
  - Offline IC test against sub_engine_outcomes.json must show IC > 0.05
  - Signal implemented in engine file, attached to shadow output only
  - Zero influence on live sizing or entries

SHADOW → LIVE:
  - Spearman IC > 0.05 AND ICIR > 0.5 over minimum 30-trade shadow window
  - Both conditions required — IC without stability (ICIR) is not sufficient

LIVE → DEPRECATED:
  - IC < 0.03 AND t-stat < 1.0 for 3+ consecutive 20-trade windows

ARES_RESEARCH.md → PROPOSED:
  - Offline IC test against existing sub_engine_outcomes.json
  - Pass = promote to PROPOSED and implement in shadow
  - Fail = stays in research, retest after more data accumulates
```

---

## ARCHITECTURE — Gated on Data

| ID | Description | Gate |
|----|-------------|------|
| ARCH-1 | IC-weighted signal weights per engine | 30+ closed trades per engine |
| ARCH-2 | Capital reallocation advisory go-live | 15+ closed trades per engine |
| ARCH-3 | Engine D intraday runtime | Explicitly prioritized |
| ARCH-4 | Engine-specific Kelly (replace bootstrap) | 30+ closed trades per engine |
| ARCH-5 | Cross-engine IC correlation analysis | 100+ total closed trades |

---

## COMPLETED

| Date | Item |
|------|------|
| 2026-05-29 | Ares repo initialized from Raptor infrastructure base |
| 2026-05-29 | ARES_SKILL.md — architecture, rules, engine summary |
| 2026-05-29 | ARES_MASTER_PLAN.md — this document |
| 2026-05-29 | ARES_STARTUP.md — session startup procedure |

---

## BUILD ORDER SUMMARY

```
NOW:
  Sprint 0 — Plumbing (S0-a through S0-h)
  Sprint 1 — Foundation data layer

WHEN Sprint 0 tested:
  Sprint 2 — Engine B (first engine live in shadow)

WHEN Sprint 2 shadow ≥ 30 entries:
  Sprint 3 — Engines A and F

WHEN Sprint 3 shadow ≥ 30 entries each:
  Sprint 4 — Engines C and E

WHEN ≥ 30 closed trades per engine:
  Sprint 5 — IC calibration + capital allocator go-live

CONTINUOUS:
  Shadow mode validation before any engine takes real capital
  All TODO:DERIVE constants resolved from actual data
```
