# Ares Trading System — Development Skill
*Last updated: 2026-05-30 | Version: 1.1*

---

## System Overview

Ares is a quantitative swing trading system on a dedicated Alpaca paper account (~$50K equity).
It is architecturally independent from Raptor. The two systems compete — same universe, different edges, separate accounts, no shared state.

**Core architectural principle:** No composite score. No routing classifier. No shared factor weights. Each engine is a self-contained trading system with its own math, its own signals, its own exit logic, and its own Kelly calibration. Engines share only infrastructure (data feeds, universe, macro gate, ledger format).

---

## Architecture

```
Shared infrastructure (no trading logic lives here):
  data_feeds.py         OHLCV bars, sector ETF bars, SPY bars
  universe_builder.py   Screens 6800 assets → dynamic symbol list
  macro_context.py      SPY trend + VIX + credit + breadth → regime label (NO FRED)
  margin_guard.py       Per-engine capital ceiling enforcement
  ledger.py             Entry/exit record keeping (engine_id keyed) + update_metadata
  kelly_engine.py       Per-engine bootstrap Kelly (shadow until 30 trades per engine)
  symbol_ownership.json Cross-engine symbol lock (Ares-internal only)
  capital_allocator.py  Advisory engine weight rebalancer
  daily_recap.py        Per-engine P&L, IC, and classification breakdown

Engine runners (one file per engine, fully independent):
  engine_a.py           Momentum Continuation          ✓ shadow
  engine_b.py           Mean Reversion / Capitulation  ✓ shadow
  engine_c.py           Squeeze Break                  NOT BUILT
  engine_d.py           Gap Reversion (deferred)       DEFERRED
  engine_e.py           RS Rotation                    NOT BUILT
  engine_f.py           Structural Breakout            ✓ shadow

Hold and exit (engine-aware dispatchers):
  ares_exit_monitor.py  Engine-dispatched exit runner (A, B, C, E, F all wired)
  ares_hold_monitor.py  Engine-dispatched health scorer (A, B, C, E, F all wired)

Engine-specific exit and hold modules:
  exit_monitor_a.py     Engine A exits (6 conditions)
  exit_monitor_b.py     Engine B exits (5 conditions)
  exit_monitor_f.py     Engine F exits (5 conditions + partial trim)
  hold_monitor_a.py     Engine A momentum health (5 layers + ADX slope penalty)
  hold_monitor_b.py     Engine B OU health (5 layers)
  hold_monitor_f.py     Engine F breakout health (5 layers)

Outcome tracking:
  outcome_tracker.py        Trade labeling — engine_id field mandatory
                            forward_return_Nd anchored at entry_date+N / entry_price
  sub_engine_outcomes.json  Per-engine trade history for IC calibration
```

---

## Engine Summary

| ID | Name | Edge | Hold | Stop model | Primary exit |
|----|------|------|------|-----------|-------------|
| A | Momentum Continuation | Established uptrend + institutional accumulation | 7–25d | 2.8–3.5× ATR (vol-adjusted) | ADX rollover / MA stack collapse |
| B | Mean Reversion | OU pull to equilibrium after fear-driven selloff | OU θ × 1.5 | 2.0–2.5× ATR | AVWAP reached / time stop |
| C | Squeeze Break | Compression energy release | 3d → extends on confirm | 1.5–1.8× ATR | Wrong direction / no fire in 3d |
| D | Gap Reversion | Price void fill (market microstructure) | Same day max | 1.0× ATR | Gap filled / 120min elapsed |
| E | RS Rotation | Sector capital flow momentum | 12–35d | 3.0× ATR | RS lost vs sector |
| F | Structural Breakout | Consolidation equilibrium resolution | 10–20d | Base low (structural) | Base violated / measured move hit |

Engine D is deferred. Requires a separate intraday runtime (minute bars, 9:30–11:30 AM loop). Its allocation stays in cash until that runtime is built.

---

## Capital Model

```
Total equity:          ~$50,000
Engines at launch:     5 (A, B, C, E, F) — D deferred
Cash floor:            10% ($5,000) — ALWAYS held uninvested as shock absorber  [RF-8]
Deployable capital:    90% ($45,000)
Initial allocation:    Equal — 5 engines × 18% = 90% of total equity
                       Engine D: 0% — deferred; its slice stays in the cash floor
Engine capital cap:    Hard ceiling per engine (enforced by margin_guard.py)

Kelly operates underneath the capital cap:
  position_size_$ = min(
      equity * engine_base_kelly * kelly_scale,    # Kelly-derived, conviction-scaled
      engine_capital_remaining,                    # per-engine budget minus deployed
      buying_power_available * 0.95                # broker constraint
  )
  engine_base_kelly: bootstrap = KELLY_BOOTSTRAP_FRACTION × KELLY_SHADOW_MULTIPLIER
                     until 30 closed trades (sign/sanity screen — see RF-3), then
                     engine's own empirical Kelly from expanding trade history
  kelly_scale:       conviction multiplier ∈ [0.5, 1.0] from engine_score percentile

  RF-8 WARNING: Kelly is mathematically correct only for confirmed positive expectancy.
  Until IC gates establish edge with statistical power, sizing parameters are
  CONSERVATIVE PLACEHOLDERS, not optimal fractions.

Engine F exception — structural stop sizing:
  shares = risk_budget / (entry_price - stop_price)
  position_size_$ = shares × entry_price, capped by engine_capital_remaining
  Stop distance is structural (base low), not ATR — sizing must respect actual risk.

Capital reallocation: ADVISORY only
  capital_allocator.py computes recommended weights nightly based on:
    rolling_sharpe (20-trade window), win_rate, IC vs forward returns
  Weights shift toward winning engines, away from losing ones.
  No automatic reallocation — Steve approves every shift.
  Gate: reallocation recommendations require ≥ 15 closed trades per engine
```

<!-- RF-8 CLOSED 2026-05-31: Cash floor (CASH_FLOOR_PCT=0.10) added to ares_config.py.
Engine weights corrected to 5×18%=90%. capital_allocator.py updated to respect
CASH_FLOOR_PCT in advisory weight normalization. Kelly sizing caveat documented. -->

---

## Macro Gate

Ares reads `macro_context.json` for regime label. No FRED data (no yield curve, no fed funds rate).

**Macro inputs used:**
- SPY trend (20d return + MA relationship)
- VIX (implied volatility)
- Credit spread (HY-IG via HYG/LQD proxy)
- Sector breadth (% above 50/150/200 MA)

**Regime labels:** RISK_ON / NEUTRAL / RISK_OFF / CRISIS

Each engine uses the regime gate in its own way — baked into engine-specific entry gates, not enforced by a shared filter. Engine B fires hardest in NEUTRAL/RISK_OFF. Engine A shuts down in RISK_OFF/CRISIS. Engine F requires RISK_ON or NEUTRAL.

---

## Symbol Ownership

`symbol_ownership.json` — `{symbol: engine_id}` for every open Ares position.

Rules:
- At entry: engine checks ownership before submitting order. Owned symbol → skip.
- At exit: full exit releases ownership. Partial trim (Engine F measured move) does not release.
- No two engines may hold the same symbol simultaneously.
- Ownership check is the FIRST gate in every engine scan, before any signal computation.

<!-- RF-10 ACCEPTED / DOCUMENTED 2026-05-31:
Ownership lock is a correctness requirement (no two engines can hold the same symbol).
Selection bias is real but accepted at this stage — architecture-phase constraint.

DOCUMENTED LIMITATION: Each engine's IC sample is scan-order-dependent. Engine B only
evaluates symbols not already claimed by engines that scan earlier. The IC measured is
conditional on the routing, not the signal in isolation.

Mitigation path (implement when shadow has > 30 trades per engine):
  In shadow mode, log a secondary "unconditioned_classifications" field — every symbol
  each engine *would have* taken if ownership were not enforced. Use this for IC
  calibration. Apply the ownership lock only at the live-trading layer.
  Until then, treat all IC estimates as conditioned on routing (flag in calibration output).
-->

**Known limitation:** Symbol ownership creates IC selection bias. IC estimates reflect
signal quality *within the symbols that engine was allowed to evaluate*, not unconditionally.
This is documented in the calibration output and accepted until unconditioned logging is added.

---

## Engine Signal Independence

Each engine computes only the signals it needs. No shared factor universe. No cross-engine z-scoring.

Signal naming: prefix all engine-specific signals with the engine letter to prevent namespace collision.
  Engine A: `a_mom_12_1`, `a_hi52_proximity`, `a_roc_accel`
  Engine B: `b_avwap_distance`, `b_stoch_divergence`, `b_volume_climax`
  Engine C: `c_squeeze_duration`, `c_hv_ratio`, `c_momentum_direction`
  Engine E: `e_rs_20d`, `e_rs_60d`, `e_sector_rs_spy`, `e_rs_acceleration`
  Engine F: `f_consol_quality`, `f_breakout_strength`, `f_overhead_clearance`, `f_vol_declining`, `f_rel_strength_spy`

Sign conventions must be documented at every signal definition:
  # Convention: higher = stronger signal for this engine's thesis
  # Range: [X, Y] | Unit: ATR multiples | Positive means: ...

---

## IC Calibration — Per Engine

Each engine tracks its own IC independently.

```
sub_engine_outcomes.json stores per trade:
  - engine_id, symbol, entry_date, entry_price
  - engine_score at entry
  - each signal value at entry (prefixed)
  - realized pnl_pct (variable hold)
  - forward_return_Nd (fixed horizon, engine-specific):
      A/B/C/F: 10d    E: 20d    D: 1d
      Anchored at entry_date+N / entry_price — NOT exit-anchored (RF-1 fixed)
  - exit_reason

IC = Spearman(signal_at_entry, forward_return_Nd)
Gate: 30 closed trades per engine = sign/sanity screen only (see RF-3)
IC horizon is immutable once trades accumulate — changing N invalidates history
```

<!-- RF-3 MITIGATED 2026-05-31: 30-trade gate already documented as "sign/sanity screen only."
IC@n=30 is explicitly labeled non-evidence in ARES_MASTER_PLAN.md ARBITRARY CONSTANTS table.
LIVE weighting waits for larger regime-spanning n. No code change needed — the gate was
never promoted to IC evidence in code; statistical_validation.py handles it correctly. -->

<!-- RF-11 OPEN: Engine E IC horizon (20d) < typical hold (12–35d). Accepted as intentional
given academic basis (Moskowitz & Grinblatt 1-month horizon). Document explicitly in
engine_e.py header once IC calibration is active. -->

---

## Signal Lifecycle

New signals can always be proposed. They must earn their way into live engines through this pipeline:

```
PROPOSED → SHADOW → LIVE → DEPRECATED
```

**PROPOSED → SHADOW**
- Implement signal computation (engine-prefixed, sign-convention documented)
- Attach to shadow output only — zero influence on live sizing or entries
- Collect IC data against forward_return_Nd (engine-specific horizon)

**SHADOW → LIVE**
- Gate: Spearman IC > 0.05 AND ICIR > 0.5 over minimum 30-trade shadow window
- Both conditions required. ICIR = IC.mean() / IC.std() across rolling 20-trade windows
- **IMPORTANT (RF-3):** 30-trade gate is a SIGN/SANITY SCREEN only, not a validated IC
  threshold. SE(Spearman)≈0.186 at n=30; IC=0.05 is statistically indistinguishable from 0.
  Passing this gate confirms the signal is non-degenerate and the system is operating.
  LIVE capital weighting requires regime-spanning samples (ideally hundreds of trades).
- Gate failure: signal stays in shadow, collect more data, retest after next 20 trades

<!-- RF-9 MITIGATED 2026-05-31: Regime-coverage concern is now documented here and in
ARES_MASTER_PLAN.md. 30-trade gate demoted to sanity screen (RF-3 fix). Full mitigation
requires unconditioned IC sampling (RF-10). Accepted as architecture-phase constraint;
revisit when 100+ regime-spanning trades are available per engine. -->

**LIVE → DEPRECATED**
- Remove if: Spearman IC < 0.03 AND t-stat < 1.0 for 3+ consecutive 20-trade windows
- Threshold changes: derive from distributional analysis of bar data, never hand-picked

**Math from ARES_RESEARCH.md → PROPOSED**
- Any math in the research backlog can be promoted to PROPOSED at any time
- Promotion trigger: IC > 0.05 computed offline against existing `sub_engine_outcomes.json`
- That offline test is the first gate — no implementation until it passes

Capital weight changes: ADVISORY only. Never shift capital manually without `capital_allocator.py` recommendation and explicit approval.

---

## Macro Architecture — Hamilton Filter

Ares uses a **Hamilton Filter (HMM)** for regime classification, not vote-count thresholds.

The Hamilton Filter outputs a probability vector P(regime=k | observations) that updates
daily via Bayes. Output is a 4-vector summing to 1.0:
  [P(RISK_ON), P(NEUTRAL), P(RISK_OFF), P(CRISIS)]

This probability vector feeds ENGINE_REGIME_GATE as a weighted blend — no cliff-switching.
If P(RISK_OFF)=0.6 and P(NEUTRAL)=0.4, Engine A fires at 0.6×0.0 + 0.4×0.75 = 0.30 of full
Kelly, not a binary on/off.

Posterior temperature smoothing (T=2.5) is applied before ENGINE_REGIME_GATE to prevent
near-one-hot posteriors from recreating cliff-switching despite the blending design.  [RF-4]

Inputs (4 features — all now in model): SPY trend, VIX, credit spread, sector breadth.  [RF-5]
Implementation: `hmmlearn` library, covariance_type='diag'. Sprint 1 — complete.

<!-- RF-4 CLOSED 2026-05-31: hamilton_filter.py updated — covariance_type='diag' (reduces
over-separation), transition diagonal raised to 0.97 (stickier priors), posterior
temperature-smoothed with T=2.5 before ENGINE_REGIME_GATE. Model must be refit
(delete hamilton_model.pkl) on first run after update. -->

<!-- RF-5 CLOSED 2026-05-31: hamilton_filter.py fetch_features() now builds a 4-column
feature matrix: SPY 20d return, log VIX, HYG/LQD credit norm, sector breadth z-score.
_init_means_for_states() updated to 4-column. Breadth is now in the model, not orphaned. -->

<!-- RF-6 CLOSED 2026-05-31: _state_order_to_regime() now uses composite stress rank
= z(-SPY_ret) + z(log_VIX) + z(-credit_norm) + z(-breadth_z) instead of VIX alone.
Prevents RISK_ON/RISK_OFF label swap in low-vol regimes with different SPY trends. -->

---

## Stop Calibration — EVT (Generalized Pareto Distribution)

All stop multipliers in `ares_config.py` are tagged `TODO:DERIVE`. EVT is the derivation method.

**Method:** Fit a Generalized Pareto Distribution (GPD) to the left tail of engine-specific
trade return distributions using `scipy.stats.genpareto`. The stop multiplier becomes the
ATR multiple at which P(stop trigger | noise) = 5%.

**Current status:** All stops are bootstrap fallbacks (`method=fallback_insufficient_data`).
The universe-bootstrap path failed on a 401 error before .env was fixed. Re-run `evt_calibrator.py`
to replace round-number stops with empirically-derived values.

**For Engine F:** structural stop distance (entry - consol_low) replaces ATR entirely.

**Standing method:** Universe bootstrap is the de-facto stop source until ~200 trades/engine
accumulate sufficient tail exceedances for per-engine GPD.

<!-- RED-FLAG RF-7 START | SEV=MED | CAT=MATH | ADDED=2026-05-29 -->
> **[RED FLAG | RF-7 | MED | MATH]**
> **Claim under review:** Stop multipliers are GPD-derived; bootstrapped from universe return data initially.
> **Reality:** `evt_calibration.json` = all `fallback_insufficient_data`, `n_observations=0`, stops = round 3.0/2.0/1.8/3.0. `fit_gpd` requires ≥20 tail exceedances ≈ ~200 trades/engine — so per-engine GPD is unreachable for a long time; the universe bootstrap is the standing method.
> **Fix:** Re-run `evt_calibrator.py`; verify `n_observations>0`/`method!=fallback`; paste JSON.
<!-- RED-FLAG RF-7 END -->

---

## Sprint State

```
SPRINT 0 — Plumbing                              ✓ COMPLETE
SPRINT 1 — Foundation data layer                 ✓ COMPLETE
SPRINT 2 — Engine B (shadow running)             ✓ COMPLETE
SPRINT 3 — Engines A and F (shadow running)      ✓ COMPLETE (2026-05-30)
  - engine_a.py, exit_monitor_a.py, hold_monitor_a.py
  - engine_f.py, exit_monitor_f.py, hold_monitor_f.py
  - ares_exit_monitor.py + ares_hold_monitor.py wired for A, B, F
  - RF-1 fixed: forward_return_Nd anchored at entry+N / entry_price
  - ledger.py: update_metadata method added

SPRINT 4 — Engines C and E                       ✓ COMPLETE (2026-05-30)
  Engine C: TTM squeeze, HV ratio, phase-state management
  Engine E: sector ETF bars, RS signals (fetched inline)
  ares_exit_monitor.py + ares_hold_monitor.py fully wired for all engines

SPRINT 5 — Calibration (gate: 30 closed trades per engine)
  IC-weighted signal weights per engine
  Ledoit-Wolf shrinkage on IC estimates (bundled — must not be separated)
  RMT covariance cleaning (bundled — must not be separated)
  capital_allocator.py advisory recommendations go live

SPRINT 6 — Validation and go-live
  Shadow mode 2 weeks minimum per engine before live capital
  Derive all thresholds from shadow data, not assumptions
  Go live at 50% of engine Kelly until 30 trades each
```

---

## Immutable Rules

1. **Math-first.** Every constant needs a derivation or `# TODO:DERIVE`. Round numbers without derivation are bugs.
2. **str_replace for edits, create_file for new files only.**
3. **No fabricated fallbacks.** Missing data → skip with warning. Never substitute invented values.
4. **Symbol ownership check is the first gate.** Every engine scan starts here before any signal computation.
5. **Engine F stop is structural, not ATR.** Sizing formula differs — never apply ATR-based Kelly to F without converting to risk-per-share.
6. **Kelly is SHADOW until 30 trades per engine.** Do not override sizing.
7. **Capital reallocation is ADVISORY.** `capital_allocator.py` recommends — Steve approves. Never auto-shift.
8. **IC horizon is immutable.** A/B/C/F=10d, E=20d, D=1d. Never change once trades accumulate.
9. **Engine D is deferred.** No intraday runtime exists. Do not attempt to adapt D to daily bars without separate validation.
10. **Sign conventions documented at every signal.** Prefix with engine letter. State range, unit, and what positive means.
11. **No FRED data in Ares.** Macro = SPY trend + VIX + credit spread + sector breadth only.
12. **Syntax check every file before committing.**
13. **Every session ends with updated files for download.** Patch file is the canonical handoff once git is initialized.
14. **Update ARES_MASTER_PLAN.md same session as any architecture change.**
15. **Shadow mode before live capital.** Every engine must classify ≥30 setups without trading before taking real positions.
16. **EVT before any engine goes live.** Stop multipliers must be empirically derived, not round numbers. Re-run `evt_calibrator.py` before live capital.
17. **Hamilton Filter is the macro module.** Never revert to vote-count thresholds.
18. **Ledoit-Wolf + RMT are Sprint 5 co-requirements.** IC calibration must not be built without shrinkage.
19. **New signals follow the lifecycle: PROPOSED → SHADOW → LIVE.** IC > 0.05 AND ICIR > 0.5 gate before any signal influences live sizing.
20. **ARES_RESEARCH.md is the holding pen.** Offline IC test required before promotion to PROPOSED.

---

## What Must Never Be Violated

- Do NOT add engine signals without shadow-mode IC validation gate
- Do NOT build a composite score or routing classifier
- Do NOT let two engines hold the same symbol — ownership lock is non-negotiable
- Do NOT use Raptor's `signals.py` Factors class in Ares — engines compute their own signals
- Do NOT shift capital allocations without advisory output and explicit approval
- Do NOT use CMD syntax — use PowerShell

---

## Steve's Preferences

- Math-first, PhD-level rigor
- Direct critical feedback, no softening
- PowerShell commands
- Explicit step-by-step instructions
- Family financially depending on this
