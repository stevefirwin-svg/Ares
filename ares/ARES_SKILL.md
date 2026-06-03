# Ares Trading System — Development Skill
*Last updated: 2026-05-29 | Version: 1.0*

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
  ledger.py             Entry/exit record keeping (engine_id keyed)
  kelly_engine.py       Per-engine bootstrap Kelly (shadow until 30 trades per engine)
  symbol_ownership.json Cross-engine symbol lock (Ares-internal only)
  capital_allocator.py  Advisory engine weight rebalancer
  daily_recap.py        Per-engine P&L, IC, and classification breakdown

Engine runners (one file per engine, fully independent):
  engine_a.py           Momentum Continuation
  engine_b.py           Mean Reversion / Capitulation
  engine_c.py           Squeeze Break
  engine_d.py           Gap Reversion (deferred — needs intraday runtime)
  engine_e.py           RS Rotation
  engine_f.py           Structural Breakout

Hold and exit (engine-aware):
  hold_monitor.py       Engine-branched health scoring (separate layer weights per engine)
  exit_monitor.py       Engine-branched exit logic (each engine's own exit conditions)

Outcome tracking:
  outcome_tracker.py    Trade labeling — engine_id field mandatory
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

Engine D is deferred. Requires a separate intraday runtime (minute bars, 9:30–11:30 AM loop). Its 8% capital allocation stays in cash until that runtime is built.

---

## Capital Model

```
Total equity:          ~$50,000
Engines at launch:     5 (A, B, C, E, F) — D deferred
Initial allocation:    Equal — $10,000 per engine (20% each)
Engine capital cap:    Hard ceiling per engine (enforced by margin_guard.py)
Combined ceiling:      100% (no composite engine — Ares is all-engine)

Kelly operates underneath the capital cap:
  position_size_$ = min(
      equity * engine_base_kelly * kelly_scale,    # Kelly-derived, conviction-scaled
      engine_capital_remaining,                    # per-engine budget minus deployed
      buying_power_available * 0.95                # broker constraint
  )
  engine_base_kelly: bootstrap = 0.5 × composite fallback until 30 closed trades,
                     then engine's own empirical Kelly via bootstrap P25
  kelly_scale:       conviction multiplier ∈ [0.5, 1.0] from engine_score percentile

Engine F exception — structural stop sizing:
  position_size_$ = min(
      risk_budget / (entry_price - stop_price),    # risk-defined, not Kelly-first
      engine_capital_remaining
  )
  where risk_budget = equity * engine_f_kelly_fraction * 0.01
  Stop distance is structural (base low), not ATR — sizing must respect actual risk.

Capital reallocation: ADVISORY only
  capital_allocator.py computes recommended weights nightly based on:
    rolling_sharpe (20-trade window), win_rate, IC vs forward returns
  Weights shift toward winning engines, away from losing ones.
  No automatic reallocation — Steve approves every shift.
  Minimum allocation per active engine: 10% (no engine starved below $5K)
  Gate: reallocation recommendations require ≥ 15 closed trades per engine
```

---

## Macro Gate

Ares reads `macro_context.json` for regime label. No FRED data (no yield curve, no fed funds rate).

**Macro inputs used:**
- SPY trend (20d return + MA relationship)
- VIX (implied volatility)
- Credit spread (HY-IG)
- Sector breadth (% above 50/150/200 MA)

**Regime labels:** RISK_ON / NEUTRAL / RISK_OFF / CRISIS

Each engine uses the regime gate in its own way — baked into engine-specific entry gates, not enforced by a shared filter. Engine B may fire harder in RISK_OFF. Engine A shuts down in CRISIS. Engine F requires RISK_ON or NEUTRAL for breakout confirmation.

---

## Symbol Ownership

`symbol_ownership.json` — `{symbol: engine_id}` for every open Ares position.

Rules:
- At entry: engine checks ownership before submitting order. Owned symbol → skip.
- At exit: full exit releases ownership. Partial trim does not release.
- No two engines may hold the same symbol simultaneously.
- Ownership check is the FIRST gate in every engine scan, before any signal computation.

---

## Engine Signal Independence

Each engine computes only the signals it needs. No shared factor universe. No cross-engine z-scoring.

Signal naming: prefix all engine-specific signals with the engine letter to prevent namespace collision.
  Engine A: `a_mom_12_1`, `a_hi52_proximity`, `a_roc_accel`
  Engine B: `b_avwap_distance`, `b_stoch_divergence`, `b_volume_climax`
  Engine C: `c_squeeze_duration`, `c_hv_ratio`, `c_momentum_direction`
  Engine E: `e_rs_20d`, `e_rs_60d`, `e_sector_rs_spy`, `e_rs_acceleration`
  Engine F: `f_consol_quality`, `f_breakout_strength`, `f_overhead_clearance`

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
  - exit_reason

IC = Spearman(signal_at_entry, forward_return_Nd)
Gate: minimum 30 closed trades per engine before IC weights replace bootstrap equal weights
IC horizon is immutable once trades accumulate — changing N invalidates history
```

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
- Gate failure: signal stays in shadow, collect more data, retest after next 20 trades

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

Inputs: SPY trend, VIX, credit spread, sector breadth (NO FRED).
Implementation: `hmmlearn` library. Build time: 2-3 days. Sprint 1 requirement.

Rationale: vote-count thresholds produce cliff effects where a 0.02 score difference flips
all engine multipliers. Continuous probability eliminates this. All engines benefit from day one.

---

## Stop Calibration — EVT (Generalized Pareto Distribution)

All stop multipliers in `ares_config.py` are tagged `TODO:DERIVE`. EVT is the derivation method.

**Method:** Fit a Generalized Pareto Distribution (GPD) to the left tail of engine-specific
trade return distributions using `scipy.stats.genpareto`. The stop multiplier becomes the
ATR multiple at which P(stop trigger | noise) = 5% — 95% of legitimate noise doesn't hit
the stop. This is a number derived from actual return data, not convention.

**For Engine F:** structural stop distance (entry - consol_low) replaces ATR entirely.
EVT fits to the distribution of (entry - consol_low) / ATR ratios from historical setups
to determine whether the structural stop is within normal noise or genuinely protective.

**Timing:** Sprint 0 — EVT replaces TODO:DERIVE constants before any engine goes live.
Even with limited trade history, bootstrapped from universe return data initially,
then updated from engine-specific closed trades as they accumulate.

Implementation: `scipy.stats.genpareto.fit()` on left-tail exceedances. Build time: 1 day.

---

## Sprint State

```
SPRINT 0 — Plumbing (current — nothing runs without this)
  0a. ares_config.py — Ares-specific parameters, separate Alpaca keys  ✓ DONE
  0b. symbol_ownership.json + ownership lock functions
  0c. unified sizing formula in each engine (Kelly + cap)
  0d. ledger.py — engine_id keyed, Ares account
  0e. macro_context.py — strip FRED, keep SPY/VIX/credit/breadth
  0f. margin_guard.py — per-engine budget awareness
  0g. sub_engine_outcomes.json structure + logger
  0h. shadow_classifications.json — log would-be entries without trading
  0i. EVT stop calibration — scipy.stats.genpareto, replace all TODO:DERIVE stop mults

SPRINT 1 — Foundation data layer + macro architecture
  1a. outcome_tracker.py — engine_id field mandatory
  1b. capital_allocator.py — advisory weight calculator (stub)
  1c. daily_recap.py — per-engine P&L table
  1d. Hamilton Filter macro_context.py — hmmlearn HMM replaces vote-count
  1e. Shadow mode: all engines classify but do not trade

SPRINT 2 — Engine B (first, lowest complexity)
  Engine B: avwap, stochastic divergence, volume climax, OU theta
  All signals enter SHADOW lifecycle, promote to LIVE after IC gate

SPRINT 3 — Engines A and F
  Engine A: mom_12_1, hi52_proximity, roc_accel
  Engine F: consolidation detection, volume profile, measured move

SPRINT 4 — Engines C and E
  Engine C: TTM squeeze, HV ratio, phase-state management
  Engine E: sector ETF bars, RS signals (new data_feeds.py fetch)

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
13. **Every session ends with a patch file.** Ares cannot push to GitHub — patch is the canonical handoff.
14. **Update ARES_MASTER_PLAN.md same session as any architecture change.**
15. **Shadow mode before live capital.** Every engine must classify ≥30 setups without trading before taking real positions.
16. **EVT before any engine goes live.** Stop multipliers must be GPD-derived, not round numbers. `scipy.stats.genpareto` is the tool. No engine enters shadow with `TODO:DERIVE` stop constants.
17. **Hamilton Filter is the macro module.** Never revert to vote-count thresholds. Regime output is a probability vector, not a discrete label.
18. **Ledoit-Wolf + RMT are Sprint 5 co-requirements.** The IC calibrator must not be built without shrinkage. Building IC calibration without shrinkage on small samples amplifies noise — it is worse than equal weights.
19. **New signals follow the lifecycle: PROPOSED → SHADOW → LIVE.** IC > 0.05 AND ICIR > 0.5 gate before any signal influences live sizing. No exceptions regardless of theoretical elegance.
20. **ARES_RESEARCH.md is the holding pen.** Math from that document requires an offline IC test against `sub_engine_outcomes.json` before promotion to PROPOSED. Theoretical merit alone is not a build trigger.

---

## What Must Never Be Violated

- Do NOT add engine signals without shadow-mode IC validation gate
- Do NOT build a composite score or routing classifier — this is the core architectural departure from Raptor
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
