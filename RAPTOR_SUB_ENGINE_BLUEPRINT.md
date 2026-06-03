# RAPTOR SUB-ENGINE ARCHITECTURE BLUEPRINT
## Parallel Trade-Type Classification System — Full Build Specification
*Version 1.1 — May 2026 (Hardened after code review)*

---

## CHANGELOG v1.1 — CODE-REVIEW CORRECTIONS

This version was revised after auditing the actual Raptor codebase (`signals.py`,
`exit_monitor.py`, `hold_monitor.py`, `main.py`, `ledger.py`, `margin_guard.py`,
`position_ledger.json`). The original v1.0 made assumptions that conflict with how
the system actually works. Ten issues were found — three are blockers that would
have made the architecture non-functional as written. All are addressed in
**Section A (Critical Corrections)** below, and the affected engine sections have
been annotated inline. **Read Section A before building anything.**

---

## SECTION A — CRITICAL CORRECTIONS (READ FIRST)

### A.1 — BLOCKER: engine_id must be plumbed end-to-end (it currently can't reach the monitors)

**Finding.** The entire engine-aware exit/hold design assumes `exit_monitor.py` and
`hold_monitor.py` can branch on `entry_metadata["engine_id"]`. They cannot today:

- `exit_monitor.py` iterates over **Alpaca broker positions**, not ledger entries. It
  consults the ledger only for `entry_date` (`_ledger_map`, building
  `{symbol: ledger_row}`), and never reads `metadata`.
- The Alpaca account is **shared** across composite (v5.4), Viper options, and the
  crypto engine. Broker positions carry no engine tag.
- The live `position_ledger.json` shows every current position with
  `"regime": "BACKFILL"` and `null` for `t_stat`, `kelly_fraction`, `composite_score`.
  Positions were reconstructed from Alpaca, so even `entry_date` is approximate and
  metadata is effectively empty.

**Required fix (precondition for the whole project):**

1. **Entry writes engine_id.** Every engine runner must call
   `ledger.record_entry(model=engine_id, ...)` where `model` ∈ {`A`,`B`,`C`,`D`,`E`,`F`,`v5.4`}.
   The ledger already keys on `f"{model}:{symbol}"`, so this is supported — but the
   `metadata` dict must now include the full `entry_params` block (stop_mult, trail
   model, hold_target, kelly, engine-specific anchors like `breakout_level`,
   `avwap_anchor`, `theta`, `momentum_direction`).
2. **Monitors look up engine by (model, symbol), not symbol alone.** Replace
   `_ledger_map = {v["symbol"]: v for v in ...}` (which silently overwrites duplicate
   symbols — last writer wins) with a map keyed on the full ledger key
   `f"{model}:{symbol}"`. The monitor must resolve which engine owns the shares before
   selecting exit logic.
3. **No-metadata fallback.** If a position has no engine_id (e.g. backfilled, or a
   pre-existing composite position), it is treated as Engine 0 (composite) with the
   existing logic. This guarantees the new system degrades gracefully.

### A.2 — BLOCKER: one Alpaca position per symbol breaks parallel ownership

**Finding.** Alpaca aggregates holdings by symbol. If composite holds NVDA and Engine A
later buys NVDA, the broker shows **one** position with a blended `avg_entry`. exit_monitor
exits the **entire** position on the first triggering signal — it cannot exit "just the
Engine A shares." Priority routing (Option 2) prevents two *sub-engines* from claiming a
symbol, but does **not** stop composite + one sub-engine from both holding it.

**Required fix — Global Symbol Lock (cross-engine, not just sub-engine):**

- Maintain a single `symbol_ownership.json`: `{symbol: owning_engine_id}` for every
  open position across ALL engines including composite.
- At entry, an engine may only buy a symbol if it is unowned. Ownership is released on
  full exit. This makes broker-level aggregation safe: one symbol → one engine → one
  exit logic path.
- Consequence for routing: the classifier must check ownership BEFORE classification.
  A held symbol is skipped by all engines (no re-entry by a different engine while held).
- `main.py` composite scan must also respect the lock — composite skips any symbol owned
  by a sub-engine, and vice versa. This replaces the current
  `signals = [s for s in signals if s.symbol not in all_held]` with an ownership check
  that is engine-aware.

### A.3 — BLOCKER: capital model (8% cap) and Kelly sizing are not reconciled

**Finding.** v1.0 said both "8% per engine" and "full Kelly." These are two independent
control systems and v1.0 never connected them:

- `signals.py` clips composite Kelly to `0.02–0.12` of equity (0.20 for qualified
  leveraged names). A single composite position can already be 12–20% of equity.
- Engines A/B/C/E/F define a `kelly_scale` ∈ [0,1] **multiplier** but never define what
  it multiplies. Engine D computes an **absolute** `kelly_fraction` ∈ [0.02, 0.08].
  Two incompatible schemes.
- `config.py`: `max_positions=10`; 10 positions at $105K already "requires margin by design."

**Required fix — single, explicit sizing formula per engine:**

```
position_size_$ = min(
    equity * engine_base_kelly * kelly_scale,     # Kelly-derived, conviction-scaled
    engine_capital_remaining,                     # 8% cap minus this engine's deployed
    combined_subengine_remaining,                 # 48% pool minus all sub-engine deployed
    buying_power_available * 0.95                 # broker constraint (existing)
)

where:
  engine_base_kelly = the engine's OWN base fraction, derived from that engine's
                      historical win/loss distribution via Kelly (f* = W - (1-W)/R),
                      NOT inherited from composite. Bootstrap = 0.5 × composite base
                      until the engine has 30 closed trades, then switch to the
                      engine's empirical Kelly. Stored in sub_engine_params.json.
  kelly_scale       = conviction multiplier ∈ [0.5, 1.0] from engine_score percentile.
```

The 8% figure is therefore a **hard capital ceiling per engine**, and Kelly operates
**underneath** it. If Kelly sizing would exceed 8%, the 8% cap binds. This makes the
two systems consistent: Kelly proposes, the cap disposes. A single full-conviction
position can consume most of an engine's 8% — that is acceptable and intended (one
strong trade per engine), but it can never exceed it.

**Position-count interaction:** `max_positions=10` is now a *combined* ceiling across
all engines + composite. Each engine additionally caps its own open positions at
`floor(0.08 / typical_position_fraction)` ≈ 1–2 concurrent positions. The blueprint's
"2–4 engines active simultaneously" assumption must be validated against the global
10-position / margin limit in shadow mode (Section 18).

### A.4 — MAJOR: cluster sign convention (MR factors are inverted)

**Finding.** In `signals.py`, MR-cluster factors are deliberately inverted so positive =
bullish: `rsi_mr=(50-RSI)/50`, `bollinger_z=-(price-mean)/std`, `ma_distance` negated.
A deeply oversold capitulation name therefore has a **high positive MR cluster score**,
not negative.

`compute_cluster_dominance()` uses `abs()`, so a strong-momentum name (high +TREND) and
a strong-oversold name (high +MR) both yield high dominance ratios; the winner is just
whichever cluster has the larger magnitude. A stock that is oversold *within* an uptrend
(high +MR and mildly +TREND) can misroute between Engine A and Engine B.

**Required fix — sign-aware routing, not magnitude-only:**

- Dominance must use **signed, semantically-correct** cluster scores, and each engine's
  gate must check the FULL sign pattern, not just its own cluster magnitude:
  - Engine A (momentum): `TREND > θ` AND `MR < +0.5` (not deeply oversold) AND
    `micro=TRENDING`. Reject if MR dominates.
  - Engine B (reversion): `MR > θ` AND `TREND < 0` (genuinely falling) AND
    `REV > 0` AND `micro=REVERTING`. The `TREND < 0` gate must be strict — a mildly
    positive TREND disqualifies Engine B and routes to A or composite.
- Add an explicit **conflict resolver**: if both `TREND > θ_trend` and `MR > θ_mr`
  (oversold-in-uptrend), this is a genuinely ambiguous setup → route to **composite**,
  not to a sub-engine. Sub-engines only take *clean* archetypes; ambiguity is the
  composite's job. This tightens the "right of first refusal" to "right of first refusal
  on unambiguous setups."

### A.5 — MAJOR: IC calibration needs fixed-horizon forward returns, not realized P&L

**Finding.** v1.0's `sub_engine_outcomes.json` stores `pnl_pct` (realized return over a
variable hold). Spearman IC (Grinold–Kahn) requires a **fixed-horizon forward return**.
Mixing horizons across engines (Engine E uses 20d, others 10d) and using
variable-hold realized P&L makes the IC values incommensurable — the same category of
error as the known `daily_recap.py` Sharpe bug (annualizing per-trade returns with a flat
`sqrt(252)`).

**Required fix:**

- `sub_engine_outcomes.json` must store, **in addition to** realized `pnl_pct`, a
  dedicated `forward_return_Nd` field where N is the engine's native IC horizon
  (`A/B/C/D=10`, `E=20`, `F=15`). This is computed from bar data at entry+N regardless
  of when the position actually exited.
- `sub_engine_calibrator.py` computes Spearman IC of each signal at entry vs
  `forward_return_Nd` — never vs realized `pnl_pct`.
- Each engine's horizon N is declared in `sub_engine_params.json` and is immutable once
  trades accumulate (changing N invalidates the IC history).

### A.6 — MAJOR: Engine D requires an intraday runtime that does not exist

**Finding.** Engine D fires pre-market, exits same-day by ~11:30 AM, and references
`_minutes_since_open()` and pre-market/minute bars. But Raptor is a **daily-bar** system:
all other modules use daily OHLCV, and exit_monitor runs only twice/day (≈9:52 AM,
3:50 PM). There is no intraday monitoring loop, and the current `data_feeds.py` may not
even expose pre-market or minute bars from Alpaca.

**Required fix — reclassify Engine D as a separate runtime, deprioritized:**

- Engine D is **moved out of the equity daily-bar build** and treated like the crypto
  engine: its own loop (`Start_Engine_D.bat`, e.g. 5-min cadence 9:30–11:30 AM), its own
  intraday data path, its own monitor. It does NOT share exit_monitor.
- Until that intraday harness exists, Engine D is **OUT OF SCOPE for the initial build**.
  The first deployable system is the five daily-bar engines (A, B, C, E, F) + composite.
  Reallocate D's 8% across the live engines or hold it in composite until D's runtime is
  built. (See revised capital table in A.8.)
- A daily-bar approximation of gap reversion (enter at next daily open, exit on daily
  bars) is possible as "Engine D-lite" but has materially different (worse) statistics
  than the intraday research cited — flagged for separate validation, not assumed.

### A.7 — MODERATE: trail multiplier double-application

**Finding.** `_trail_mult()` already applies a signal-quality modifier (GAP 1: ×1.3 /
×0.75 based on `(composite+health)/2`). v1.0's engine logic then multiplies that output
by an engine `trail_scale`, **compounding** two signal-quality adjustments unpredictably
and inconsistently across engines.

**Required fix — one unified trail model:**

- Introduce `_trail_mult_v2(days_held, profit_atr, rcfg, engine_id, engine_state)` that
  computes the trail from a SINGLE source of truth per engine. The GAP 1 composite
  modifier applies ONLY to Engine 0. Each sub-engine supplies its own trail policy
  (base width + the engine's own health/conviction adjustment), and there is exactly one
  multiplicative adjustment, not two stacked.
- The OU-theta trail (SKILL backlog item) becomes Engine B's native trail policy; the
  momentum trail becomes Engine A's; etc. `trail_scale` as a standalone post-multiplier
  is removed from the design.

### A.8 — Revised capital allocation (after removing Engine D from initial build)

```
INITIAL BUILD (5 daily-bar engines + composite):
  Engine A: 8%    Engine B: 8%    Engine C: 8%
  Engine E: 8%    Engine F: 8%
  Sub-engine pool ceiling: 40% (was 48%)
  Composite (Engine 0): 60% + all undeployed sub-engine capital
  Combined position ceiling: max_positions = 10 (shared, existing)

FUTURE (when Engine D intraday runtime is built):
  Add Engine D: 8% (intraday, separate runtime, does not draw from daily-bar pool
  during equity hours — funded from its own intraday capital slice)
  Re-evaluate combined ceiling against margin at that time.
```

### A.9 — MODERATE: shadow-mode needs its own outcome plumbing

**Finding.** Sprint 1 says "classify but don't trade," but v1.0 defines no mechanism to
record a shadow classification and score it later. Without it, Sprint 6 has no empirical
data to set thresholds.

**Required fix:** `shadow_classifications.json` logs every would-be entry
`{date, symbol, engine_id, engine_score, cluster_scores, exclusive_signals, entry_price}`.
A nightly job (fold into `sub_engine_calibrator.py`) computes forward returns at each
engine's horizon and writes `shadow_outcomes.json`. Thresholds in Section 18 are derived
from this, not assumed. Shadow mode must run long enough to collect ≥30 shadow entries
per engine before that engine goes live.

### A.10 — MINOR: reused factor names have opposite sign/range conventions

**Finding.** Two reuse hazards:
- `atr_pctile` in `signals.py` returns `[-1,+1]` with **negative = high volatility**
  (`-(pctile/100−0.5)*2`). v1.0's `stop_mult = 3.5 − 0.7*atr_pctile` assumes `[0,1]` with
  high = high vol — sign-flipped, would widen stops in calm markets and tighten them in
  volatile ones (backwards).
- `bb_squeeze` similarly returns `[-1,+1]` with negative = wide bands. Engine C reuses the
  name expecting "high = squeezed."

**Required fix:** Sub-engines must NOT reuse the composite factor names for their own
parameter math. Define explicit, freshly-named helpers with documented ranges
(`atr_pct_0to1`, `squeeze_intensity_0to1`) computed inside `trade_classifier.py`, and
state the sign convention in a comment at each use site. Every stop/trail formula must be
re-derived against the correct convention.

### A.11 — Summary impact on build order

- Sprints 1–6 stand, but **Engine D is removed from Sprints 2 and onward** (its runtime
  is a separate future track — see A.6).
- A new **Sprint 0** is inserted before everything: build the plumbing in A.1 (engine_id
  end-to-end), A.2 (symbol ownership lock), A.3 (unified sizing), and the no-metadata
  fallback. Nothing else can be trusted until Sprint 0 is done and tested against the
  shared Alpaca account.
- `daily_recap.py` known bugs (Sharpe/Sortino horizon, `actual_exit_path=unknown`,
  `regime_score=0`) should be fixed **before** sub-engine outcome tracking is trusted —
  they share the forward-return/horizon machinery (A.5).

---

## 0. GOVERNING PRINCIPLES

Every number in this document must satisfy the math-first mandate:
- No round numbers without derivation
- No static thresholds — all derived from rolling distributional analysis of bar data
- No hand-picked weights — all derived from Spearman IC against forward returns once 30+ trades per engine accumulate
- All scoring systems are cross-sectionally z-scored within the current universe (same framework as composite)
- All capital parameters obey Kelly criterion, not fixed fractions
- Each engine's scoring is orthogonal — signals chosen for low mutual information with each other and with the composite

**Capital allocation: 8% of equity per engine × 6 engines = 48% sub-engine capital pool**
**Composite Engine (Engine 0): remaining 52% — unchanged**
**Total always ≤ margin_guard thresholds — engines share a combined sub-engine capital budget**

---

## 1. SYSTEM OVERVIEW — PARALLEL ARCHITECTURE

```
Raw bar data (OHLCV, 120 bars minimum)
│
├─ Computed once per scan — shared data pool:
│   ├─ 16 composite factors (existing signals.py — NEVER MODIFIED)
│   ├─ SPY bars (relative strength denominator)
│   ├─ Sector ETF bars (new — required for Engine E)
│   └─ Sub-engine exclusive signals (new — computed in trade_classifier.py)
│
│  PRIORITY ROUTING (Option 2 — sub-engines get right of first refusal)
│
├─→ [PRE-MARKET ~9:05 AM]
│   Engine D: Overnight Gap Reversion — time-critical, narrow window
│
├─→ [9:35 AM SCAN — alongside composite]
│   Engine F: Structural Breakout — highest conviction, checked first
│   Engine E: RS Rotation — sector leadership context required
│   Engine A: Momentum Continuation — most frequent classification
│   Engine B: Mean Reversion / Capitulation — fires in corrections
│   Engine C: Squeeze / Volatility Break — rarest, requires compression
│
├─→ Engine 0: Composite (existing) — trades what sub-engines do NOT claim
│
└─→ Capital allocation gate:
    sub_engine_capital = equity × 0.08 per engine
    If engine capital exhausted → pass candidate to composite
    Combined sub-engine pool ≤ 0.48 × equity
    Composite pool = remaining after sub-engine deployments
```

---

## 2. NEW FILES — BUILD ORDER

```
Phase 1 (Foundation):
  trade_classifier.py         ← core router + all sub-engine signal computation
  sub_engine_params.json      ← calibrated parameters (auto-updated, not hand-set)
  sub_engine_ledger.json      ← per-engine position tracking and capital accounting
  sub_engine_outcomes.json    ← per-engine trade outcomes (feeds IC calibration)

Phase 2 (Engine Runners):
  engine_d_gap.py             ← Gap Reversion pre-market runner
  engine_a_momentum.py        ← Momentum Continuation daily runner
  engine_b_reversal.py        ← Mean Reversion daily runner
  engine_c_squeeze.py         ← Squeeze Break daily runner
  engine_e_rotation.py        ← RS Rotation daily runner
  engine_f_breakout.py        ← Structural Breakout daily runner

Phase 3 (Monitor Integration):
  hold_monitor.py             ← ADD: engine-aware health scoring branches
  exit_monitor.py             ← ADD: engine-aware exit logic branches
  main.py                     ← ADD: sub-engine priority gate before composite scan
  margin_guard.py             ← ADD: per-engine capital budget tracking
  daily_recap.py              ← ADD: per-engine P&L and IC reporting

Phase 4 (Calibration):
  sub_engine_calibrator.py    ← IC-weighted parameter updates (runs post-close)
  Start_SubEngine_Premarket.bat
  Start_SubEngine_Scan.bat
  Start_SubEngine_Calibrator.bat
```

---

## 3. trade_classifier.py — CORE ROUTER

### 3.1 Responsibility
- Receives the same `bars_dict`, `spy_bars`, `macro_data` that `signals.py` receives
- Computes all sub-engine-exclusive signals (never touches signals.py)
- Returns a `ClassificationResult` per symbol: which engine claims it, with what score
- Enforces priority ordering and capital budget gates

### 3.2 Data Structures

```python
@dataclass
class SubEngineSignal:
    symbol: str
    engine_id: str          # "A"|"B"|"C"|"D"|"E"|"F"
    engine_score: float     # normalized 0→1 (engine-specific scoring)
    cluster_scores: dict    # raw cluster sub-scores used for classification
    exclusive_signals: dict # engine-exclusive factor values (not in composite)
    entry_params: dict      # engine-specific stop_mult, trail_mult, hold_target, kelly_scale
    classification_confidence: float  # derived from cluster dominance ratio
    timestamp: str

@dataclass
class ClassificationResult:
    symbol: str
    claimed_by: str         # engine_id or "COMPOSITE"
    sub_signal: Optional[SubEngineSignal]
    composite_signal: Optional[Signal]  # from existing signals.py
```

### 3.3 Cluster Sub-Score Computation

From the existing `factor_scores` dict on each Signal object — zero new data fetching:

```python
def compute_cluster_subscores(factor_scores: dict) -> dict:
    """
    Decompose the composite into 5 cluster sub-scores.
    Uses the same cross-sectional z-scores already in factor_scores.
    Weights within each cluster = equal initially, IC-weighted after 30+ trades.
    """
    CLUSTERS = {
        "MR":    ["rsi_mr", "bollinger_z", "crowd_panic", "ma_distance", "hurst"],
        "TREND": ["ma_stack", "macd_accel", "adx_dir", "price_cloud"],
        "VOL":   ["vol_ratio", "obv_r2", "accum_dist"],
        "VOLAT": ["atr_pctile", "bb_squeeze", "rel_strength"],
        "REV":   ["rev_momentum"],
    }
    subscores = {}
    for cluster, factors in CLUSTERS.items():
        vals = [factor_scores.get(f, 0.0) for f in factors if f in factor_scores]
        subscores[cluster] = float(np.mean(vals)) if vals else 0.0
    return subscores

def compute_cluster_dominance(subscores: dict) -> tuple[str, float]:
    """
    Returns the dominant cluster and its dominance ratio.
    Dominance ratio = max_cluster_score / sum(abs(all_scores))
    High ratio = clear single-cluster signal. Low ratio = mixed = composite.
    Threshold derived from 80th percentile of historical dominance ratios.
    """
    abs_scores = {k: abs(v) for k, v in subscores.items()}
    total = sum(abs_scores.values())
    if total < 1e-10:
        return "NONE", 0.0
    dominant = max(abs_scores, key=abs_scores.get)
    ratio = abs_scores[dominant] / total
    return dominant, ratio
```

### 3.4 Classification Priority Logic

```python
PRIORITY_ORDER = ["D", "F", "E", "A", "B", "C"]
# D first — time-critical, pre-market only (not in daily scan)
# F second — structural breakouts are rarest and highest conviction
# E third — sector leadership is medium-frequency, requires sector context
# A fourth — momentum continuation, most common
# B fifth — mean reversion, regime-dependent frequency
# C sixth — squeeze, rarest intraday engine

def classify_symbol(symbol, bars, spy_bars, sector_bars,
                    factor_scores, macro_data, sub_engine_capital_remaining):
    """
    Returns SubEngineSignal or None (None = route to composite).
    Engines evaluated in priority order. First qualifying engine claims symbol.
    """
    subscores = compute_cluster_subscores(factor_scores)
    dominant, dominance_ratio = compute_cluster_dominance(subscores)

    # Dominance ratio threshold — derived from 80th percentile of universe distribution
    # Bootstrapped from first 30 trading days, updated rolling thereafter
    dominance_threshold = load_calibrated_param("dominance_threshold", default=0.42)

    if dominance_ratio < dominance_threshold:
        return None  # Mixed signal — composite gets it

    # Engine D is pre-market only — skip in daily scan call
    # F, E, A, B, C evaluated in order
    for engine_id in ["F", "E", "A", "B", "C"]:
        if sub_engine_capital_remaining[engine_id] <= 0:
            continue
        result = ENGINE_CLASSIFIERS[engine_id](
            symbol, bars, spy_bars, sector_bars,
            factor_scores, subscores, macro_data
        )
        if result is not None:
            return result

    return None  # No engine claimed it — composite gets it
```

---

## 4. ENGINE A — MOMENTUM CONTINUATION

### 4.1 Trade Thesis
Stock is in an established uptrend with institutional accumulation confirming. Entry is continuation, not reversal. Trend cluster dominant, VOL cluster confirming, TREND micro regime active.

### 4.2 Classification Gate

> **v1.1 CORRECTION (A.4 — sign-aware routing):** Added `MR < +0.5` ceiling. MR factors
> are inverted in signals.py (oversold = HIGH +MR), so a high +MR means deeply oversold —
> that belongs to Engine B. If BOTH `TREND > θ_trend` AND `MR > θ_mr` → route to
> COMPOSITE (ambiguous; sub-engines only take clean archetypes).

```
Required (ALL must hold):
  subscores["TREND"] > θ_trend    (θ derived: 70th pctile of universe TREND scores)
  subscores["VOL"] > θ_vol        (θ derived: 60th pctile of universe VOL scores)
  subscores["MR"] < +0.5          (NOT deeply oversold — A.4; MR is inverted in signals.py)
  micro_regime == "TRENDING"      (Hurst > 0.55 AND ADX > 25 — existing logic)
  adx_dir > 0                     (directional — positive momentum only)
  ma_stack > 0                    (EMA 8 > EMA 21 > EMA 50 — stacked bullish)
  dominance_ratio TREND/total > dominance_threshold
  NOT (TREND > θ_trend AND MR > θ_mr)  → if both fire, COMPOSITE claims it (A.4)

Exclusive signals computed additionally (not in composite):
  momentum_12_1      ← 12-month return excluding last month (Jegadeesh-Titman 1993)
  hi52w_proximity    ← (52w_high - close) / ATR — proximity to new high
  roc_acceleration   ← ROC_5 - ROC_20, normalized by rolling std (2nd derivative)
```

### 4.3 Engine A Exclusive Signals

```python
def compute_engine_a_signals(bars, spy_bars):
    c = bars["close"]

    # Signal A1: Time-series momentum 12-1
    # Jegadeesh & Titman (1993) — one of the most replicated anomalies in finance
    # Formation: 252 bars back to 21 bars back (excludes reversal month)
    if len(c) >= 252:
        mom_12_1 = float((c.iloc[-21] / c.iloc[-252]) - 1.0)
    else:
        mom_12_1 = np.nan

    # Signal A2: 52-week high proximity
    # George & Hwang (2004) — anchoring bias creates predictable order flow at highs
    # Negative score = above 52w high (best), Large positive = far below (worst for A)
    hi52 = bars["high"].rolling(252).max().iloc[-1] if len(bars) >= 252 else bars["high"].max()
    atr14 = _atr(bars, 14)
    hi52_proximity = float((hi52 - c.iloc[-1]) / atr14) if atr14 > 0 else np.nan
    # For Engine A scoring: lower proximity (closer to/above 52w high) = stronger

    # Signal A3: ROC acceleration (2nd derivative of momentum)
    # Is momentum accelerating or plateauing?
    roc_5  = float((c.iloc[-1] / c.iloc[-6]) - 1.0) if len(c) >= 6 else np.nan
    roc_20 = float((c.iloc[-1] / c.iloc[-21]) - 1.0) if len(c) >= 21 else np.nan
    roc_accel = float(roc_5 - roc_20) if not (np.isnan(roc_5) or np.isnan(roc_20)) else np.nan

    return {
        "mom_12_1":     mom_12_1,
        "hi52_proximity": hi52_proximity,
        "roc_accel":    roc_accel,
    }
```

### 4.4 Engine A Scoring System

```
Engine A Score = weighted combination of:
  ├─ TREND cluster z-score          (base cluster — must be positive)
  ├─ VOL cluster z-score            (institutional confirmation)
  ├─ mom_12_1 (cross-sectionally z-scored vs universe)
  ├─ hi52_proximity (inverted — closer to high = higher score)
  └─ roc_accel (normalized by rolling std of universe)

Weights:
  Bootstrap phase (< 30 Engine A trades): equal weights across 5 components
  Post-calibration: Spearman IC of each component vs Engine A forward returns,
  normalized to sum 1.0, updated rolling 60-day window
  IC computation: rank(component_at_entry) vs rank(forward_return_10d)

Final score normalized to [0, 1] via logistic transform of weighted sum:
  engine_a_score = 1 / (1 + exp(-weighted_sum))
  Classification threshold: engine_a_score > 0.60 (60th percentile of Engine A score
  distribution over rolling 60-day window)
```

### 4.5 Engine A Entry Parameters

> **v1.1 CORRECTION (A.10):** Do NOT reuse `atr_pctile` from signals.py — it returns
> [-1,+1] with NEGATIVE = high vol. Use a freshly-defined `atr_pct_0to1` (raw percentile,
> 0=calm, 1=volatile). All stop formulas below assume the 0→1 convention.
> **v1.1 CORRECTION (A.3):** `kelly_scale` is the conviction multiplier in the unified
> sizing formula; it multiplies `engine_base_kelly` (Engine A's own empirical Kelly),
> NOT the composite's. **v1.1 CORRECTION (A.7):** `trail_scale` is removed — Engine A
> supplies its own single trail policy via `_trail_mult_v2`.

```python
# All parameters derived, not chosen
def engine_a_entry_params(bars, macro_data, engine_a_score):
    atr = _atr(bars, 14)
    atr_pctile = _atr_percentile(bars)   # where current ATR sits in 60-bar history

    # Hard stop: wider in momentum trades — trending stocks need room
    # Base: 3.5 ATR in low vol, 2.8 in high vol (inverse relationship — wide trend = tight stop)
    # Derived: atr_pctile maps linearly from [0,1] to [3.5, 2.8]
    stop_mult = 3.5 - (0.7 * atr_pctile)

    # Trail: starts wide (momentum needs room), tightens only after thesis shows weakness
    # Base trail from composite config, scaled by engine_a_score conviction
    # score 0.60 → trail_scale 1.0; score 0.90 → trail_scale 1.4
    trail_scale = 1.0 + (0.4 * (engine_a_score - 0.60) / 0.30)

    # Hold target: momentum trades hold longer — OU speed irrelevant (not mean reverting)
    # Derived from 12-1 momentum signal half-life research (Jegadeesh-Titman ~6 months)
    # But for swing: ADX-based — stronger trend = longer hold
    adx_val = abs(bars.get("_adx_raw", 25))
    hold_target_days = int(np.clip(5 + (adx_val - 20) * 0.5, 7, 25))

    # Kelly scale: Engine A gets full Kelly at score > 0.80, half Kelly at 0.60
    kelly_scale = np.clip((engine_a_score - 0.60) / 0.20, 0.5, 1.0)

    return {
        "stop_mult": round(stop_mult, 3),
        "trail_scale": round(trail_scale, 3),
        "hold_target_days": hold_target_days,
        "kelly_scale": round(kelly_scale, 3),
        "engine_id": "A",
    }
```

### 4.6 Engine A Hold Monitor Integration

In `hold_monitor.py`, add engine-aware branch in `compute_health_score()`:

```python
if entry_metadata.get("engine_id") == "A":
    # Engine A health emphasizes TREND cluster maintenance
    # ADX rolling over = earliest warning
    # Cluster weights remapped for momentum context
    ENGINE_A_LAYER_WEIGHTS = {
        # Derived from IC of each layer vs Engine A forward returns
        # Bootstrap: upweight TREND-sensitive layers
        "composite_slope":  0.20,   # trend continuation via composite
        "cluster_health":   0.30,   # TREND cluster dominance must persist
        "price_momentum":   0.25,   # ROC acceleration must stay positive
        "volume":           0.15,   # institutional accumulation confirmation
        "volatility":       0.05,   # less relevant for momentum
        "factor_agreement": 0.03,   # de-emphasized vs momentum specifics
        "stop_distance":    0.01,
        "hold_duration":    0.01,
    }
    # Health check adds: ADX slope (is trend strengthening or exhausting?)
    # ADX peak detection — ADX rolling over from > 30 = critical warning for Engine A
    adx_slope = _compute_adx_slope(snapshots)  # new function
    adx_penalty = -0.3 if adx_slope < -2.0 else 0.0  # ADX declining from high = reduce health
    health = _compute_weighted_health(snapshots, ENGINE_A_LAYER_WEIGHTS) + adx_penalty
```

### 4.7 Engine A Exit Logic

In `exit_monitor.py`, add engine-aware exit branch:

```python
if engine_id == "A":
    # Engine A specific exits:
    # EXIT A1: Trend Exhaustion — ADX peaks and reverses (not price-based)
    adx_now = factor_scores.get("adx_dir", 0)
    adx_3d_ago = get_historical_factor("adx_dir", 3)  # from hold_history.json
    if adx_now > 25 and adx_3d_ago is not None and (adx_now - adx_3d_ago) < -5:
        if pnl_pct > 0:  # Only exit on ADX rollover if profitable
            return "trend_exhaustion", "ADX peaked and rolling over"

    # EXIT A2: Momentum Break — ROC_5 flips negative while in profit
    roc_5 = (current_price / price_5d_ago - 1)
    if roc_5 < -0.02 and pnl_pct > 0.03:
        return "momentum_break", f"ROC5={roc_5:.2%} flipped negative"

    # EXIT A3: Modified hard stop (wider for momentum)
    hard_stop = entry_price - (stop_mult * atr)  # stop_mult from engine_a_entry_params
    if current_price <= hard_stop:
        return "hard_stop_A", f"price {current_price:.2f} <= stop {hard_stop:.2f}"

    # EXIT A4: MA stack collapse — EMA 8 crosses below EMA 21
    if e8 < e21 and days_held > hold_target_days * 0.5:
        return "ma_stack_collapse", "EMA8 crossed below EMA21"

    # Trail: wider multiplier from engine_a_entry_params["trail_scale"]
    trail_mult_a = _trail_mult(days_held, profit_atr, rcfg,
                               composite, health) * trail_scale
```

---

## 5. ENGINE B — MEAN REVERSION / CAPITULATION

### 5.1 Trade Thesis
Stock has experienced fear-driven selloff beyond fundamental justification. Crowd panic has peaked. Smart money is beginning to absorb supply. The statistical edge is the OU mean-reversion pull back toward equilibrium. Shorter duration, tighter management — if it doesn't reverse quickly, it's not reverting.

### 5.2 Classification Gate
```
Required (ALL must hold):
  subscores["MR"] > θ_mr           (θ derived: 75th pctile of universe MR scores)
  subscores["REV"] > 0             (rev_momentum positive — bounce starting)
  subscores["TREND"] < 0           (trend is negative — stock has been falling)
  crowd_panic > 0                  (fear-driven selling confirmed)
  micro_regime == "REVERTING"      (Hurst < 0.45 AND ADX < 20 — existing logic)

Exclusive signals:
  avwap_distance     ← (close - anchored_vwap_swing_high) / ATR
  stoch_divergence   ← bullish divergence score (price LL vs Stochastic HL)
  volume_climax      ← Wyckoff selling climax detection score
  short_interest_score ← days-to-cover from universe_builder (already fetched)
```

### 5.3 Engine B Exclusive Signals

```python
def compute_engine_b_signals(bars, entry_price=None):
    c, h, l, v = bars["close"], bars["high"], bars["low"], bars["volume"]
    atr14 = _atr(bars, 14)

    # Signal B1: Anchored VWAP distance from swing high (start of selloff)
    # VWAP anchored to highest close in last 20 bars — origin of the decline
    # Institutional cost basis — they will buy back toward this level
    # Brian Shannon (2022) — AVWAP as institutional fair value reference
    lookback = min(20, len(c))
    swing_high_idx = c.iloc[-lookback:].idxmax()
    # Compute AVWAP from swing_high_idx to today
    anchor_slice = bars.loc[swing_high_idx:]
    if len(anchor_slice) >= 2:
        pv = (anchor_slice["close"] * anchor_slice["volume"]).cumsum()
        vol_cum = anchor_slice["volume"].cumsum()
        avwap = float(pv.iloc[-1] / vol_cum.iloc[-1]) if vol_cum.iloc[-1] > 0 else c.iloc[-1]
        avwap_distance = float((c.iloc[-1] - avwap) / atr14) if atr14 > 0 else np.nan
        # Negative = below AVWAP = oversold relative to institutional fair value
    else:
        avwap_distance = np.nan

    # Signal B2: Stochastic divergence (bullish)
    # Price making lower lows while Stochastic %K making higher lows
    # More specific than RSI divergence — captures momentum exhaustion precisely
    period_k = 14
    if len(c) >= period_k + 5:
        low_min = l.rolling(period_k).min()
        high_max = h.rolling(period_k).max()
        pct_k = 100 * (c - low_min) / (high_max - low_min + 1e-10)
        # 5-bar price slope vs 5-bar %K slope
        price_slope = float(np.polyfit(np.arange(5), c.iloc[-5:].values, 1)[0])
        stoch_slope = float(np.polyfit(np.arange(5), pct_k.iloc[-5:].values, 1)[0])
        # Bullish divergence: price falling (slope < 0) but stochastic rising (slope > 0)
        stoch_divergence = float(stoch_slope - price_slope) / (abs(price_slope) + 1e-10)
        stoch_divergence = float(np.clip(stoch_divergence, -3, 3))
    else:
        stoch_divergence = np.nan

    # Signal B3: Volume climax detection (Wyckoff selling climax)
    # Highest volume bar in last 5 days, closing in upper half of range
    # = sellers exhausted, buyers absorbing at close
    if len(bars) >= 5:
        recent = bars.iloc[-5:]
        max_vol_idx = recent["volume"].idxmax()
        max_vol_bar = recent.loc[max_vol_idx]
        bar_range = float(max_vol_bar["high"] - max_vol_bar["low"])
        close_pos = float((max_vol_bar["close"] - max_vol_bar["low"]) / bar_range) \
                    if bar_range > 1e-6 else 0.5
        vol_ratio_climax = float(max_vol_bar["volume"] / v.iloc[-21:-1].mean()) \
                           if v.iloc[-21:-1].mean() > 0 else 1.0
        # Score: high vol ratio × close in upper half = climax
        volume_climax = float(vol_ratio_climax * close_pos) if close_pos > 0.5 else 0.0
    else:
        volume_climax = np.nan

    return {
        "avwap_distance":   avwap_distance,
        "stoch_divergence": stoch_divergence,
        "volume_climax":    volume_climax,
    }
```

### 5.4 Engine B Scoring System

```
Engine B Score = weighted combination of:
  ├─ MR cluster z-score              (must be dominant and positive)
  ├─ REV cluster z-score             (reversal momentum confirming)
  ├─ avwap_distance (inverted sign)  (more negative = more oversold = higher score)
  ├─ stoch_divergence                (positive = bullish divergence)
  └─ volume_climax                   (higher = more exhaustion = higher score)

Short interest score (days_to_cover from universe_builder) used as MULTIPLIER:
  If days_to_cover > 5: score_multiplier = 1 + (days_to_cover - 5) * 0.05
  Capped at 1.25× — short squeeze potential amplifies score without dominating it

IC weighting: same Spearman IC framework as Engine A
Bootstrap: equal weights across 5 components
Post-calibration: rolling 60-day IC update

Classification threshold: engine_b_score > 0.60 AND avwap_distance < -1.0 ATR
The AVWAP condition is a hard gate — must be genuinely oversold relative to
institutional fair value, not just bottom of composite score range.
```

### 5.5 Engine B Entry Parameters

```python
def engine_b_entry_params(bars, macro_data, engine_b_score):
    atr = _atr(bars, 14)

    # OU theta estimation — 30-day rolling OLS on log-price
    # Leung & Zhang (2019) — OU theta determines optimal hold duration for MR
    log_p = np.log(bars["close"].values[-30:])
    if len(log_p) >= 10:
        # OLS: log_p[t] = alpha + beta * log_p[t-1] — beta decay = OU theta
        X = log_p[:-1]; y = log_p[1:]
        beta = float(np.linalg.lstsq(
            np.column_stack([np.ones_like(X), X]), y, rcond=None)[0][1])
        theta = float(-np.log(max(beta, 0.01)))  # OU mean-reversion speed
        # Half-life = ln(2) / theta (days to half-reversion)
        half_life = float(np.log(2) / max(theta, 0.01))
        # Cap 1-15 days — longer than 15 = not a MR trade
        hold_target_days = int(np.clip(half_life * 1.5, 3, 15))
    else:
        hold_target_days = 7  # fallback only — flagged as unestimated

    # Hard stop: TIGHTER for MR — if it fails to reverse, cut fast
    # Low vol regime: 2.0 ATR (tighter — price deviations are meaningful)
    # High vol regime: 2.5 ATR (slightly wider — noise is higher)
    atr_pctile = _atr_percentile(bars)
    stop_mult = 2.0 + (0.5 * atr_pctile)

    # Trail: very tight — MR trades revert fast or not at all
    # Start at 1.5 ATR trail immediately (no early-period wideness like composite)
    trail_scale = 0.75  # tighter than composite trail

    # Kelly: MR wins are smaller and faster — size according to MR win rate
    # Kelly scale inversely weighted with OU half-life (longer half-life = less conviction)
    half_life_norm = float(np.clip(hold_target_days / 15.0, 0.2, 1.0))
    kelly_scale = float(np.clip(1.2 - half_life_norm, 0.4, 1.0))

    return {
        "stop_mult": round(stop_mult, 3),
        "trail_scale": round(trail_scale, 3),
        "hold_target_days": hold_target_days,
        "kelly_scale": round(kelly_scale, 3),
        "theta": round(theta, 4) if 'theta' in dir() else None,
        "half_life": round(half_life, 2) if 'half_life' in dir() else None,
        "engine_id": "B",
    }
```

### 5.6 Engine B Hold Monitor Integration

```python
if entry_metadata.get("engine_id") == "B":
    ENGINE_B_LAYER_WEIGHTS = {
        # MR trades: price momentum and cluster health most predictive
        # Volume and composite slope less relevant short-term
        "composite_slope":  0.10,
        "factor_agreement": 0.15,
        "price_momentum":   0.30,   # Is price actually moving back toward mean?
        "cluster_health":   0.25,   # MR cluster must stay dominant
        "volume":           0.10,   # Decreasing selling volume = good
        "volatility":       0.05,
        "stop_distance":    0.04,
        "hold_duration":    0.01,
    }
    # Engine B adds: OU reversion progress tracking
    # How far has price moved back toward avwap? Normalize by (avwap - entry)
    avwap_current = _compute_avwap(bars, entry_date)
    reversion_progress = (current_price - entry_price) / (avwap_current - entry_price + 1e-10)
    # >0.7 = 70% reverted = approaching target, begin tightening trail
    if reversion_progress > 0.70:
        trail_tighten_flag = True
```

### 5.7 Engine B Exit Logic

```python
if engine_id == "B":
    # EXIT B1: Mean reversion target reached (primary)
    # Target = AVWAP from swing high (institutional fair value)
    avwap = _compute_avwap_live(bars, entry_date)
    if current_price >= avwap * 0.99:  # 99% of AVWAP = close enough
        return "mr_target_reached", f"price {current_price:.2f} ≈ AVWAP {avwap:.2f}"

    # EXIT B2: Stochastic normalization (secondary)
    # %K returns to 50+ = oversold condition resolved
    pct_k = _stochastic_k(bars)
    if pct_k > 55 and days_held >= 2 and pnl_pct > 0:
        return "stoch_normalized", f"%K={pct_k:.0f} — oversold resolved"

    # EXIT B3: Time stop — MR trades must work fast
    # Half-life-derived: if price hasn't moved after 1.5× half-life, thesis failed
    half_life = entry_metadata.get("half_life", 7)
    time_stop_days = int(half_life * 1.5)
    if days_held >= time_stop_days and pnl_pct < 0.01:
        return "mr_time_stop", f"held {days_held}d > half_life × 1.5 = {time_stop_days}d"

    # EXIT B4: Hard stop (tight)
    hard_stop = entry_price - (stop_mult * atr)
    if current_price <= hard_stop:
        return "hard_stop_B", f"price {current_price:.2f} <= tight_stop {hard_stop:.2f}"

    # EXIT B5: Thesis invalidation — if trend resumes downward
    if subscores["MR"] < 0 and subscores["TREND"] < -0.5 and pnl_pct < -0.03:
        return "mr_thesis_invalid", "MR cluster collapsed and trend resumed down"
```

---

## 6. ENGINE C — SQUEEZE / VOLATILITY BREAK

### 6.1 Trade Thesis
Stock has been in an anomalously quiet compression period. Volatility is at historical extremes of low. Energy is being coiled. The break — when it comes — tends to be sharp and directional. This engine catches the ignition, not the continuation.

### 6.2 Classification Gate
```
Required (ALL must hold):
  subscores["VOLAT"] > θ_volat     (θ: 80th pctile — must be extreme compression)
  bb_squeeze is in active squeeze   (BBands inside Keltner Channels)
  vol_ratio < 0                    (volume below average — coiling, not breaking yet)
  hv_ratio < 0.65                  (HV10/HV30 — short-term vol well below longer-term)
  squeezing for >= N bars           (N derived: median squeeze duration in universe history)

Direction confirmed by:
  subscores["TREND"] > 0 → long bias
  subscores["MR"] > 0 AND subscores["TREND"] < 0 → mean-reversion squeeze (rarer)
```

### 6.3 Engine C Exclusive Signals

```python
def compute_engine_c_signals(bars):
    c, h, l, v = bars["close"], bars["high"], bars["low"], bars["volume"]

    # Signal C1: TTM Squeeze — Bollinger Bands inside Keltner Channels
    # John Carter (2013) — original quantification of the coiled spring
    bb_period = 20
    bb_std = 2.0
    kc_period = 20
    kc_atr_mult = 1.5

    bb_mid = c.rolling(bb_period).mean()
    bb_std_val = c.rolling(bb_period).std()
    bb_upper = bb_mid + bb_std * bb_std_val
    bb_lower = bb_mid - bb_std * bb_std_val

    atr_kc = _atr_series(bars, kc_period)
    kc_upper = bb_mid + kc_atr_mult * atr_kc
    kc_lower = bb_mid - kc_atr_mult * atr_kc

    in_squeeze = (bb_upper < kc_upper) & (bb_lower > kc_lower)
    squeeze_duration = int(in_squeeze.iloc[-20:].sum())  # bars in squeeze in last 20
    currently_squeezing = bool(in_squeeze.iloc[-1])

    # Signal C2: Historical Volatility Ratio (HV10/HV30)
    # Ratio < 1 = recent vol below longer-term vol = compression
    # Ratio < 0.65 = historically extreme (derived from 10th percentile of HV ratio universe)
    log_r = np.log(c / c.shift(1)).dropna()
    hv10 = float(log_r.iloc[-10:].std() * np.sqrt(252)) if len(log_r) >= 10 else np.nan
    hv30 = float(log_r.iloc[-30:].std() * np.sqrt(252)) if len(log_r) >= 30 else np.nan
    hv_ratio = float(hv10 / hv30) if (hv30 and hv30 > 1e-10) else np.nan

    # Signal C3: Momentum direction for the squeeze break
    # Linear momentum of last 10 bars (where will the spring fire?)
    if len(c) >= 10:
        momentum_slope = float(np.polyfit(np.arange(10), c.iloc[-10:].values, 1)[0])
        momentum_direction = 1.0 if momentum_slope > 0 else -1.0
    else:
        momentum_direction = 0.0

    return {
        "squeeze_duration":      squeeze_duration,
        "currently_squeezing":   currently_squeezing,
        "hv_ratio":              hv_ratio,
        "momentum_direction":    momentum_direction,
    }
```

### 6.4 Engine C Scoring System

```
Engine C Score = weighted combination of:
  ├─ VOLAT cluster z-score       (compression intensity — must be high)
  ├─ squeeze_duration            (normalized: longer squeeze = more energy stored)
  ├─ hv_ratio (inverted)         (lower ratio = more compressed = higher score)
  ├─ momentum_direction          (1.0 or -1.0 — sets direction)
  └─ bb_squeeze factor z-score   (already in VOLAT cluster but used separately here)

Entry only fires when currently_squeezing == True (in active squeeze)
AND vol_ratio starts to RISE (volume returning = squeeze beginning to fire)
This is the trigger — not classification alone

squeeze_duration_score = (squeeze_duration - median_duration) / std_duration
  where median and std derived from universe's historical squeeze durations
  rolling 90-day window. Longer than median = more coiled = higher score.
```

### 6.5 Engine C Entry / Exit Parameters

```python
# Engine C is unique: entry timing is everything
# Stop is close (if squeeze fires wrong way = immediate exit)
# Once break confirmed, transitions to Engine A trail logic

def engine_c_entry_params(bars, engine_c_score):
    atr = _atr(bars, 14)

    # Hard stop: tight — 1.5 ATR (squeeze resolve wrong = exit fast)
    # Tighter than any other engine because the thesis is binary
    # If squeeze fires wrong direction, every bar against you is new information
    atr_pctile = _atr_percentile(bars)
    stop_mult = 1.5 + (0.3 * atr_pctile)  # 1.5 low vol, 1.8 high vol

    # Hold target: short initially, extend after break confirms
    # Phase 1 (squeeze firing): 3-5 bars to confirm direction
    # Phase 2 (confirmed break): transition to Engine A parameters
    hold_target_phase1 = 3
    hold_target_phase2 = 12  # if break confirms — extend

    # Kelly: smaller initially due to binary outcome nature
    # Scale with squeeze duration — longer squeeze = more conviction
    squeeze_dur = engine_c_score  # used as proxy here; full score computed
    kelly_scale = 0.60  # conservative at entry; increase after break confirms

    return {
        "stop_mult": round(stop_mult, 3),
        "trail_scale": 0.80,  # tight initially
        "hold_target_days": hold_target_phase1,
        "kelly_scale": kelly_scale,
        "phase": "squeeze_firing",
        "engine_id": "C",
    }
```

### 6.6 Engine C Exit Logic

```python
if engine_id == "C":
    phase = position_metadata.get("phase", "squeeze_firing")

    if phase == "squeeze_firing":
        # EXIT C1: Squeeze fired wrong direction (immediate)
        # If price moves against momentum_direction within first 3 bars — exit
        initial_direction = entry_metadata.get("momentum_direction", 1.0)
        price_move = (current_price - entry_price) / entry_price
        if price_move * initial_direction < -0.01 and days_held <= 3:
            return "squeeze_wrong_direction", f"move={price_move:.2%} against direction"

        # EXIT C2: Squeeze confirmed — upgrade to Engine A trail logic
        # If price moves in correct direction > 1 ATR in < 3 bars = break confirmed
        if price_move * initial_direction > atr / entry_price and days_held <= 3:
            position_metadata["phase"] = "break_confirmed"
            position_metadata["trail_scale"] = 1.2  # Engine A-style wider trail
            # Do not exit — upgrade position parameters

        # EXIT C3: Time stop — squeeze must fire within 3 bars of entry
        if days_held >= 4 and abs(price_move) < 0.005:
            return "squeeze_no_fire", "squeeze failed to resolve within time window"

    elif phase == "break_confirmed":
        # Once break confirmed, use Engine A momentum continuation exits
        # Re-classify as Engine A for exit purposes
        return _engine_a_exit_logic(...)
```

---

## 7. ENGINE D — OVERNIGHT GAP REVERSION

> **v1.1 — OUT OF INITIAL SCOPE (A.6).** Engine D requires an intraday runtime (pre-market
> data, minute bars, a 9:30–11:30 AM monitoring loop, same-day exits) that Raptor's
> daily-bar architecture does not provide. exit_monitor runs only twice/day and cannot
> manage an 11:30 AM gap exit. Engine D is therefore deferred to a separate runtime track
> (modeled on the crypto engine's standalone loop) and is NOT part of the first deployable
> system. Its 8% allocation is held in composite until the intraday harness exists. The
> specification below is retained for that future build. A daily-bar "Engine D-lite"
> (enter next open, manage on daily bars) is possible but has materially worse statistics
> than the intraday research cited and must be validated separately, not assumed.

### 7.1 Trade Thesis
Overnight gaps in individual stocks (not news-driven) create price voids with zero traded volume. Market microstructure predicts these voids attract price back — institutions didn't trade at those prices and need to adjust their VWAP benchmarks. The edge decays rapidly — within the first 2 hours of the trading day.

Academic basis: Wibowo & Naimoli (2019) — 51.47% annualized, Sharpe 2.38 on S&P 500 constituents 1998-2015.

### 7.2 Pre-Market Classification Gate (run ~9:05 AM)
```
Gap detection (vs prior close):
  gap_pct = (open - prior_close) / prior_close

Qualifying criteria (ALL):
  abs(gap_pct) between θ_gap_min and θ_gap_max
    θ_gap_min = derived from 20th percentile of all gaps (exclude noise)
    θ_gap_max = derived from 80th percentile (exclude fundamental gaps)
    Typical empirical range: 0.5% - 4.0% (validated by Wibowo 2019)
  No major earnings in last 3 days (fundamental gap = excluded)
  No major news flag (categorical exclusion — agent layer check)
  macro_regime != "CRISIS" and != "RISK_OFF"
  Prior 5-day trend was in same direction as gap (gap with trend = more likely to fill)
    OR overnight session moved AGAINST the gap direction (83.3% fill rate per TradingStats 2026)
  Gap into prior support zone (High Volume Node within 0.5 ATR of gap open)

Direction: gap_pct > 0 → expect fade (short the gap open) → NOT AVAILABLE (long-only)
           gap_pct < 0 → expect fill (buy the gap open) → Engine D fires long only
```

### 7.3 Engine D Exclusive Signals

```python
def compute_engine_d_signals(bars, prior_close, pre_market_bars=None):
    open_price = float(bars["close"].iloc[-1])  # today's open as proxy if intraday not started
    gap_pct = float((open_price - prior_close) / prior_close)

    # Signal D1: Gap size normalized by ATR
    # Large gap relative to ATR = more meaningful signal
    atr14 = _atr(bars, 14)
    gap_atr = float(abs(open_price - prior_close) / atr14) if atr14 > 0 else np.nan

    # Signal D2: Prior day trend alignment
    # Was the stock trending UP before the gap down? (pullback more likely to fill)
    roc_5_prior = float((prior_close / bars["close"].iloc[-6]) - 1.0) if len(bars) >= 6 else 0.0
    trend_aligned = 1.0 if roc_5_prior > 0.01 and gap_pct < 0 else 0.0

    # Signal D3: Proximity to High Volume Node below gap
    # Price voids without nearby institutional HVN have weaker fill magnets
    # Volume profile: 20-day, identify price level of highest volume
    vol_profile = _compute_volume_profile(bars, lookback=20)
    hvn_price = vol_profile["hvn_price"]  # price with highest cumulative volume
    hvn_proximity = float(abs(open_price - hvn_price) / atr14) if atr14 > 0 else np.nan
    # Lower = closer to HVN = stronger magnet = better Engine D candidate

    # Signal D4: Pre-market volume direction (if available)
    # Pre-market rally after gap down = 83.3% fill rate (TradingStats 2026)
    premarket_direction = 0.0
    if pre_market_bars is not None and len(pre_market_bars) > 0:
        pm_move = float((pre_market_bars["close"].iloc[-1] - open_price) / open_price)
        # If gap was down AND pre-market moved up = strong signal
        if gap_pct < 0 and pm_move > 0.002:
            premarket_direction = 1.0

    return {
        "gap_pct":            gap_pct,
        "gap_atr":            gap_atr,
        "trend_aligned":      trend_aligned,
        "hvn_proximity":      hvn_proximity,
        "premarket_direction": premarket_direction,
    }
```

### 7.4 Engine D Scoring System

```
Engine D Score (for gap-down long setup):
  ├─ gap_atr (normalized)            — gap size relative to ATR (sweet spot: 0.8-2.5 ATR)
  │   penalty applied if > 2.5 ATR   — too large = likely fundamental
  ├─ trend_aligned (binary)          — 1.0 if prior trend supported fill
  ├─ hvn_proximity (inverted)        — closer to HVN = higher score
  └─ premarket_direction             — strongest single predictor when present

Gap size sweet spot scoring (inverted-U):
  gap_atr < 0.5 → disqualify (noise)
  gap_atr 0.8-2.0 → score peaks here
  gap_atr > 2.5 → score decays rapidly
  gap_atr > 4.0 → disqualify (likely fundamental)
  Functional form: triangular kernel centered at 1.5 ATR, derived from fill-rate data

No IC weighting needed for bootstrap — Engine D has binary outcome (fills or doesn't)
Instead: logistic regression on {gap_atr, trend_aligned, hvn_proximity, premarket_dir}
vs fill_binary outcome. Updated rolling as outcomes accumulate.
```

### 7.5 Engine D Entry / Exit Parameters

```python
def engine_d_entry_params(gap_signals, bars):
    atr = _atr(bars, 14)
    gap_pct = gap_signals["gap_pct"]
    prior_close = bars["close"].iloc[-2]

    # Target: gap fill (prior close) — mathematically defined, not arbitrary
    target_price = float(prior_close)
    target_pct = float(abs(gap_pct))  # the fill amount

    # Stop: 1.0 ATR below gap open — if it continues falling, not a gap fill
    # Tight because the edge is short-duration and directional
    stop_mult = 1.0  # tight — thesis is binary

    # Hold target: gap fills happen in first 2 hours or not at all
    # Wibowo (2019): mean-reverting property strongest 120 min after open
    hold_target_days = 1  # same-day exit only — by 11:30 AM or close

    # Kelly: sized by probability of fill × expected gain / expected loss
    # fill_prob derived from logistic regression on engine_d_score
    fill_prob = gap_signals.get("engine_d_score", 0.60)
    # Expected gain = gap_pct, Expected loss = stop_mult * ATR / price
    expected_gain = abs(gap_pct)
    expected_loss = stop_mult * atr / gap_signals.get("open_price", bars["close"].iloc[-1])
    kelly_fraction = float(fill_prob / expected_loss - (1 - fill_prob) / expected_gain)
    kelly_fraction = float(np.clip(kelly_fraction, 0.02, 0.08))  # 8% max = engine capital

    return {
        "stop_mult": stop_mult,
        "target_price": target_price,
        "hold_target_days": hold_target_days,
        "kelly_fraction": kelly_fraction,
        "intraday_only": True,  # must exit same day regardless
        "engine_id": "D",
    }
```

### 7.6 Engine D Exit Logic

```python
if engine_id == "D":
    target_price = entry_metadata.get("target_price")
    intraday_only = entry_metadata.get("intraday_only", True)

    # EXIT D1: Gap filled — target reached (primary, happy path)
    if current_price >= target_price * 0.995:
        return "gap_filled", f"price {current_price:.2f} ≈ prior_close {target_price:.2f}"

    # EXIT D2: Time stop — gap fill trades MUST exit same day
    # By 11:30 AM if not filled, trend day forming
    market_open_minutes = _minutes_since_open()
    if market_open_minutes >= 120 and pnl_pct < 0.005:
        return "gap_time_stop", "120min passed — gap fill not materializing"

    # EXIT D3: End of day forced exit (no overnight holds for gap trades)
    if _is_near_close():  # 3:45 PM
        return "gap_eod_exit", "gap trade mandatory EOD close"

    # EXIT D4: Hard stop (tight — gap continued against us)
    hard_stop = entry_price - (stop_mult * atr)
    if current_price <= hard_stop:
        return "gap_hard_stop", f"gap continued beyond stop {hard_stop:.2f}"
```

---

## 8. ENGINE E — RELATIVE STRENGTH ROTATION

### 8.1 Trade Thesis
Capital flows from lagging sectors to leading sectors in identifiable, persistent patterns. The strongest stock in the strongest sector during an active rotation has compounding tailwinds — institutional rebalancing, sector momentum, and individual stock leadership all aligned. Duration is longer than other engines because you're riding a macro capital flow, not a technical pattern.

Academic basis: Moskowitz & Grinblatt (1999) — industry momentum strongest at 1-month horizon. Jegadeesh & Titman (1993) cross-sectional momentum. Dorsey Wright Focus Five — sector rotation evidence.

### 8.2 Classification Gate
```
Requires sector ETF bars (new data fetch — one ETF per sector: XLK, XLF, XLV, XLE, etc.):

  stock_rs_vs_sector  = (stock_return_20d) - (sector_return_20d)  > θ_rs
  sector_rs_vs_spy    = (sector_return_20d) - (spy_return_20d) > θ_rs_sector
  Both computed cross-sectionally, z-scored vs universe
  
  rs_acceleration     = (stock_rs_vs_sector_now - stock_rs_vs_sector_5d_ago) > 0
  new_rs_high_20d     = stock_rs_vs_sector at highest point in 20 days (leadership breakout)
  
  θ_rs derived: 70th percentile of stock_rs_vs_sector distribution, rolling 60 days
  θ_rs_sector derived: 60th percentile of sector_rs_vs_spy distribution, rolling 60 days
```

### 8.3 Engine E Exclusive Signals

```python
SECTOR_ETFS = {
    "Technology": "XLK", "Financials": "XLF", "Healthcare": "XLV",
    "Energy": "XLE", "Consumer Discretionary": "XLY", "Industrials": "XLI",
    "Materials": "XLB", "Communication Services": "XLC", "Utilities": "XLU",
    "Real Estate": "XLRE", "Consumer Staples": "XLP",
}

def compute_engine_e_signals(symbol_bars, sector_etf_bars, spy_bars, symbol_sector):
    c = symbol_bars["close"]
    sector_c = sector_etf_bars["close"]
    spy_c = spy_bars["close"]

    # Signal E1: Stock RS vs Sector (20-day and 60-day)
    rs_20d = float((c.iloc[-1]/c.iloc[-21]) - (sector_c.iloc[-1]/sector_c.iloc[-21])) \
             if len(c) >= 21 and len(sector_c) >= 21 else np.nan
    rs_60d = float((c.iloc[-1]/c.iloc[-61]) - (sector_c.iloc[-1]/sector_c.iloc[-61])) \
             if len(c) >= 61 and len(sector_c) >= 61 else np.nan

    # Signal E2: Sector RS vs SPY (20-day)
    sector_rs_spy = float((sector_c.iloc[-1]/sector_c.iloc[-21]) - (spy_c.iloc[-1]/spy_c.iloc[-21])) \
                    if len(sector_c) >= 21 and len(spy_c) >= 21 else np.nan

    # Signal E3: RS acceleration (is leadership accelerating?)
    rs_now = rs_20d
    rs_5d_ago = float((c.iloc[-6]/c.iloc[-26]) - (sector_c.iloc[-6]/sector_c.iloc[-26])) \
                if len(c) >= 26 and len(sector_c) >= 26 else np.nan
    rs_acceleration = float(rs_now - rs_5d_ago) if not (np.isnan(rs_now) or np.isnan(rs_5d_ago)) else np.nan

    # Signal E4: New 20-day RS high (leadership breakout)
    # Is the stock setting a new relative strength high = fresh institutional interest
    rs_series = [(c.iloc[i]/c.iloc[i-21]) - (sector_c.iloc[i]/sector_c.iloc[i-21])
                 for i in range(-20, 0) if len(c) >= abs(i)+21]
    new_rs_high = 1.0 if (rs_series and rs_now >= max(rs_series)) else 0.0

    # Signal E5: Sector breadth (Zweig 1986 — % stocks above 50MA in sector)
    # Requires universe membership by sector — available from universe_builder
    # High breadth = rotation is broad, not just one stock = more sustainable
    # Computed separately in sector_breadth.py (new utility)

    return {
        "rs_20d":          rs_20d,
        "rs_60d":          rs_60d,
        "sector_rs_spy":   sector_rs_spy,
        "rs_acceleration": rs_acceleration,
        "new_rs_high":     new_rs_high,
    }
```

### 8.4 Engine E Scoring System

```
Engine E Score = weighted combination of:
  ├─ rs_20d (z-scored cross-sectionally)    — short-term RS leadership
  ├─ rs_60d (z-scored cross-sectionally)    — persistent RS (not just short burst)
  ├─ sector_rs_spy (z-scored)               — sector tailwind strength
  ├─ rs_acceleration                        — momentum of RS (2nd derivative)
  └─ new_rs_high (binary multiplier)        — 1.2× if at 20d RS high

IC weighting: Spearman IC of each component vs 20-day forward return
This engine tends to have longer formation, so forward return window = 20 days (vs 10d others)

Key distinction from Engine A:
  Engine A: stock in uptrend (absolute momentum)
  Engine E: stock outperforming its sector AND sector outperforming SPY (relative momentum)
  A stock can qualify for both — Engine E gets priority if sector rotation conditions hold
```

### 8.5 Engine E Entry / Exit Parameters

```python
def engine_e_entry_params(e_signals, bars, engine_e_score):
    # Hold target: rotation trades persist 3-8 weeks (Moskowitz & Grinblatt 1999)
    # RS strength proxy for persistence: higher rs_60d = longer rotation still active
    rs_60d = e_signals.get("rs_60d", 0.0)
    hold_target_days = int(np.clip(15 + rs_60d * 100, 12, 35))

    # Hard stop: wider — rotation positions are not pure technical trades
    # Loss of RS is the primary stop, not price alone
    stop_mult = 3.0  # standard — rely on RS-based exits more than price stop

    # Trail: wider because rotation can persist with normal pullbacks
    trail_scale = 1.2  # 20% wider than composite default

    # Kelly: scale with combined RS conviction
    kelly_scale = float(np.clip(engine_e_score, 0.5, 1.0))

    return {
        "stop_mult": stop_mult,
        "trail_scale": trail_scale,
        "hold_target_days": hold_target_days,
        "kelly_scale": kelly_scale,
        "engine_id": "E",
    }
```

### 8.6 Engine E Exit Logic (RS-based, not price-based)

```python
if engine_id == "E":
    # EXIT E1: Stock loses RS leadership vs sector (primary Engine E exit)
    current_rs = _compute_rs_20d(symbol_bars, sector_bars)
    entry_rs = entry_metadata.get("entry_rs_20d", 0)
    if current_rs < 0 and entry_rs > 0:  # RS flipped negative
        return "rs_lost", f"RS vs sector flipped: {entry_rs:.3f} → {current_rs:.3f}"

    # EXIT E2: Sector loses leadership vs SPY
    current_sector_rs = _compute_sector_rs(sector_bars, spy_bars)
    if current_sector_rs < -0.01:  # sector underperforming
        if pnl_pct > 0:  # protect profit if sector rotates out
            return "sector_rotation_out", "sector lost SPY leadership"

    # EXIT E3: RS acceleration reverses (deceleration = rotation peak)
    current_rs_accel = _compute_rs_acceleration(symbol_bars, sector_bars)
    if current_rs_accel < -0.005 and days_held > 10:
        return "rs_decelerating", "RS acceleration turned negative — rotation peak likely"

    # EXIT E4: Standard price hard stop (only if RS also degraded)
    hard_stop = entry_price - (stop_mult * atr)
    if current_price <= hard_stop:
        return "hard_stop_E", "price stop hit"

    # EXIT E5: Hold duration exceeded with no further RS improvement
    if days_held > hold_target_days and current_rs < entry_rs * 0.5:
        return "rotation_exhausted", "hold target exceeded with decaying RS"
```

---

## 9. ENGINE F — STRUCTURAL BREAKOUT

### 9.1 Trade Thesis
Price consolidation periods represent temporary supply/demand equilibrium. The breakout from consolidation — confirmed by volume — represents the resolution of that equilibrium and the beginning of a new directional move. Unlike Engine A (which enters an existing trend), Engine F enters at the origin of the move. Edwards & Magee (1948), Darvas box theory, IBD methodology — among the most documented technical edges in equity markets.

### 9.2 Classification Gate
```
Consolidation detection:
  Lookback 15 bars minimum (20 preferred)
  Range = max(high[-N:]) - min(low[-N:]) normalized by ATR
  Tight_range = range_ATR < 1.5 × ATR (stock moving less than 1.5 ATR per day on average)
  Volume declining during consolidation (OBV slope negative or flat)

Breakout bar (today):
  close > max(high[-N+1:]) — closes above entire consolidation
  volume > 1.8 × avg_vol_20d — institutional conviction required
    1.8× derived from 70th percentile of breakout volume distribution in universe
  close in top 25% of today's bar range (strong close = bullish)

No overhead HVN:
  No High Volume Node within 2.0 ATR above breakout price
  HVN above = immediate resistance = breakout likely fails
```

### 9.3 Engine F Exclusive Signals

```python
def compute_engine_f_signals(bars):
    c, h, l, v = bars["close"], bars["high"], bars["low"], bars["volume"]
    atr14 = _atr(bars, 14)
    avg_vol = float(v.iloc[-21:-1].mean())

    # Signal F1: Consolidation quality score
    # How tight was the consolidation? Tighter = more energy coiled
    lookback = 20
    consol_range = float(h.iloc[-lookback:-1].max() - l.iloc[-lookback:-1].min())
    range_atr = consol_range / atr14 if atr14 > 0 else np.nan
    # Lower range_atr = tighter consolidation = better setup
    consol_quality = float(np.clip(3.0 - range_atr, 0, 3.0)) if range_atr else np.nan

    # Signal F2: Breakout bar properties
    today_range = float(h.iloc[-1] - l.iloc[-1])
    close_pos = float((c.iloc[-1] - l.iloc[-1]) / today_range) if today_range > 1e-6 else 0.5
    vol_ratio_today = float(v.iloc[-1] / avg_vol) if avg_vol > 0 else 1.0
    # Breakout score: close in top quartile AND high volume
    breakout_strength = float(close_pos * np.log1p(vol_ratio_today - 1))
    # Close position × log(vol premium) — multiplicative, not additive

    # Signal F3: Volume profile — overhead resistance check
    # 20-day volume profile, check HVN above current price
    vol_profile = _compute_volume_profile(bars, lookback=20)
    nearest_hvn_above = vol_profile.get("nearest_hvn_above", np.inf)
    overhead_clearance = float(nearest_hvn_above / atr14) if atr14 > 0 else 5.0
    # Higher = more room above = better breakout candidate

    # Signal F4: Volume decline during consolidation (energy coiling)
    consol_vol = v.iloc[-lookback:-1]
    if len(consol_vol) >= 5:
        vol_slope = float(np.polyfit(np.arange(len(consol_vol)), consol_vol.values, 1)[0])
        vol_declining = 1.0 if vol_slope < 0 else 0.0
    else:
        vol_declining = 0.0

    return {
        "consol_quality":      consol_quality,
        "consol_range_atr":    range_atr,
        "breakout_strength":   breakout_strength,
        "close_position":      close_pos,
        "vol_ratio_today":     vol_ratio_today,
        "overhead_clearance":  overhead_clearance,
        "vol_declining":       vol_declining,
    }
```

### 9.4 Engine F Scoring System

```
Engine F Score = weighted combination of:
  ├─ consol_quality          — tighter consolidation = higher score
  ├─ breakout_strength       — close position × volume premium
  ├─ overhead_clearance      — room above = breakout can run
  ├─ vol_declining (binary)  — 1.2× multiplier if volume declined during consol
  └─ rel_strength vs SPY     — sector tailwind (use existing rel_strength factor)

Hard gate (BEFORE scoring):
  vol_ratio_today > 1.8 (volume must confirm — no exception)
  close_position > 0.75 (must close in top quartile of bar)
  No overhead HVN within 2.0 ATR

Measured move calculation (stored in entry_metadata):
  breakout_level = max(high[-N+1:])  — resistance just broken
  base_height = breakout_level - min(low[-N:])  — height of consolidation
  measured_target = breakout_level + base_height  — Edwards & Magee measured move
  Target is informational for trim logic — not a mechanical exit
```

### 9.5 Engine F Entry / Exit Parameters

```python
def engine_f_entry_params(f_signals, bars, engine_f_score):
    atr = _atr(bars, 14)
    breakout_level = f_signals.get("breakout_level")
    base_height = f_signals.get("base_height")

    # Hard stop: BELOW THE BASE of consolidation — structure-based, not ATR
    # Edwards & Magee principle: base violated = breakout failed
    consol_low = float(bars["low"].iloc[-20:-1].min())
    stop_price = consol_low - (0.1 * atr)  # slight buffer below base
    stop_mult_implied = float((bars["close"].iloc[-1] - stop_price) / atr)

    # Hold target: measured move is the target
    measured_target = breakout_level + base_height if (breakout_level and base_height) else None
    # Time target: breakout trades should show progress within 2 weeks
    # If measured move not achieved in hold_target_days → reassess
    hold_target_days = int(np.clip(10 + engine_f_score * 5, 10, 20))

    # Trail: moderate — let breakout run but protect against reversal below base
    trail_scale = 1.1  # slightly wider than composite

    # Kelly: engine_f_score as conviction proxy
    kelly_scale = float(np.clip(engine_f_score, 0.5, 1.0))

    return {
        "stop_price": stop_price,           # structure-based, not ATR multiple
        "stop_mult_implied": stop_mult_implied,
        "trail_scale": trail_scale,
        "hold_target_days": hold_target_days,
        "measured_target": measured_target,
        "kelly_scale": kelly_scale,
        "breakout_level": breakout_level,
        "engine_id": "F",
    }
```

### 9.6 Engine F Exit Logic

```python
if engine_id == "F":
    stop_price = entry_metadata.get("stop_price")
    measured_target = entry_metadata.get("measured_target")
    breakout_level = entry_metadata.get("breakout_level")

    # EXIT F1: Base violated — breakout failed
    # Price falls back below the consolidation base = false breakout
    if stop_price and current_price <= stop_price:
        return "breakout_failed", f"price {current_price:.2f} fell below base {stop_price:.2f}"

    # EXIT F2: Measured move achieved — take partial profit
    # At measured target: trim 40% (Kelly-derived fraction), let remainder trail
    if measured_target and current_price >= measured_target * 0.98:
        if not position_metadata.get("measured_move_trimmed"):
            position_metadata["measured_move_trimmed"] = True
            return "measured_move_trim", f"price {current_price:.2f} ≈ target {measured_target:.2f}"
            # Note: this triggers a TRIM not a full exit — exit_monitor handles the remainder with trail

    # EXIT F3: Breakout level becomes support — if price re-tests and holds, no exit
    # If price re-tests but FAILS to hold breakout level after 3 days in position → exit
    if days_held > 3 and current_price < breakout_level and pnl_pct < 0:
        return "breakout_support_failed", f"price fell back through breakout level {breakout_level:.2f}"

    # EXIT F4: Standard trailing stop (Engine F trail = slightly wider)
    trail_mult_f = _trail_mult(days_held, profit_atr, rcfg, composite, health) * trail_scale
    trail_stop = high_water - (trail_mult_f * atr)
    if current_price <= trail_stop:
        return "trail_stop_F", f"trail={trail_stop:.2f}"
```

---

## 10. HOLD MONITOR INTEGRATION — ENGINE-AWARE HEALTH SCORING

### 10.1 Changes to hold_monitor.py

> **v1.1 CORRECTION (A.1):** `entry_metadata["engine_id"]` is only available if Sprint 0
> plumbing is done. hold_monitor currently loads the ledger keyed by SYMBOL
> (`load_ledger()` builds `{symbol: row}`), which silently drops one record when two
> engines hold the same symbol. With the global symbol lock (A.2) a symbol has exactly one
> owner, so keying by symbol is safe ONLY after A.2 is enforced. The dispatcher below must
> default to Engine 0 (composite, existing logic) whenever `engine_id` is missing —
> backfilled/legacy positions must not crash or misroute.

```python
# In compute_health_score(), dispatch by engine_id:

def compute_health_score(symbol, snapshots, entry_metadata):
    engine_id = entry_metadata.get("engine_id", "0")  # "0" = composite

    if engine_id == "0":
        return _composite_health(snapshots)    # EXISTING logic — UNCHANGED

    elif engine_id == "A":
        return _engine_a_health(snapshots)     # Trend-focused layers

    elif engine_id == "B":
        return _engine_b_health(snapshots)     # MR-focused layers

    elif engine_id == "C":
        return _engine_c_health(snapshots)     # Squeeze-phase-aware

    elif engine_id == "D":
        return _engine_d_health(snapshots)     # Intraday only — minimal layers

    elif engine_id == "E":
        return _engine_e_health(snapshots)     # RS-decay focused

    elif engine_id == "F":
        return _engine_f_health(snapshots)     # Base-integrity focused
```

### 10.2 Engine-Specific Layer Weight Tables

All weights start as equal-weighted across relevant layers, then converge to IC-weighted after 30+ trades per engine. Layer weights stored in `sub_engine_params.json` and updated by `sub_engine_calibrator.py`.

```
Engine A — Trend Health Layers:
  composite_slope:  [ic-weighted] — base: 0.20
  cluster_health:   [ic-weighted] — base: 0.30  (TREND cluster dominance)
  price_momentum:   [ic-weighted] — base: 0.25  (ROC acceleration)
  volume:           [ic-weighted] — base: 0.15  (institutional accumulation)
  adx_slope:        [ic-weighted] — base: 0.10  (new: ADX trending up vs rolling over)

Engine B — MR Health Layers:
  price_momentum:   [ic-weighted] — base: 0.30  (is price reverting?)
  cluster_health:   [ic-weighted] — base: 0.25  (MR cluster still dominant)
  ou_progress:      [ic-weighted] — base: 0.20  (% reverted toward AVWAP target)
  volume:           [ic-weighted] — base: 0.15  (selling volume declining)
  factor_agreement: [ic-weighted] — base: 0.10

Engine C — Squeeze Health Layers:
  Phase "squeeze_firing":
    price_movement_direction:  0.60  (is it moving the right way?)
    volume_expansion:          0.25  (vol must expand on break)
    squeeze_persistence:       0.15  (is stock still above break level?)
  Phase "break_confirmed":
    → Transition to Engine A health layers

Engine D — Gap Health Layers (simplified — intraday):
  gap_fill_progress:     0.70  (primary — how far toward prior close?)
  volume_supporting:     0.20  (is volume continuing?)
  time_remaining:        0.10  (decay factor — time limit hard constraint)

Engine E — Rotation Health Layers:
  rs_vs_sector:          [ic-weighted] — base: 0.35  (primary exit trigger)
  sector_rs_spy:         [ic-weighted] — base: 0.25  (sector still leading?)
  rs_acceleration:       [ic-weighted] — base: 0.20  (rotation still accelerating?)
  composite_slope:       [ic-weighted] — base: 0.10
  price_momentum:        [ic-weighted] — base: 0.10

Engine F — Breakout Health Layers:
  base_integrity:        [ic-weighted] — base: 0.35  (price above base = healthy)
  volume_momentum:       [ic-weighted] — base: 0.25  (vol staying above avg)
  breakout_progress:     [ic-weighted] — base: 0.20  (% toward measured move)
  composite_slope:       [ic-weighted] — base: 0.10
  overhead_distance:     [ic-weighted] — base: 0.10  (room above current price)
```

---

## 11. CAPITAL ACCOUNTING — sub_engine_ledger.json

> **v1.1 CORRECTIONS:**
> - **Initial build is 5 engines (A,B,C,E,F).** Sub-engine pool ceiling = **40%** (not 48%);
>   composite = 60% + undeployed (A.8). Remove the `"D"` entry until its runtime exists.
> - **Sizing is unified (A.3):** `deployed_equity` is the sum of positions sized by the
>   single formula in A.3 (`engine_base_kelly × kelly_scale`, capped by 8%, then by the
>   40% pool, then by buying power). The 8% is a hard ceiling; Kelly operates beneath it.
> - **Global symbol lock (A.2):** before sizing, the engine must hold the symbol's
>   ownership in `symbol_ownership.json`. `positions` here must reconcile 1:1 with broker
>   positions owned by this engine — no symbol appears under two engines.
> - **Position-count (A.3):** the existing `max_positions=10` is a COMBINED ceiling across
>   all engines + composite, enforced in margin_guard, in addition to per-engine 8% caps.
> - Update the gate below: `0.08` cap stands per engine; `combined_max = equity * 0.40`.

### 11.1 Structure

```json
{
  "engine_capital_pct": 0.08,
  "engines": {
    "A": {
      "allocated_equity": 8400.0,
      "deployed_equity": 5200.0,
      "positions": ["NVDA", "MSFT"],
      "available": 3200.0,
      "daily_pnl": 142.50,
      "total_trades": 12,
      "win_rate": 0.583
    },
    "B": { ... },
    "C": { ... },
    "D": { ... },
    "E": { ... },
    "F": { ... },
    "COMPOSITE": {
      "allocated_equity": 54600.0,
      "deployed_equity": 38000.0,
      "positions": ["INTC", "AMD", "TSLL", "STM"],
      "available": 16600.0
    }
  },
  "combined_sub_engine_deployed": 28000.0,
  "combined_sub_engine_max": 50400.0,
  "last_updated": "2026-05-26T09:35:00"
}
```

### 11.2 Capital Gate in main.py

```python
def get_engine_available_capital(engine_id, equity, sub_engine_ledger):
    engine_alloc = equity * 0.08  # 8% per engine
    deployed = sub_engine_ledger["engines"][engine_id]["deployed_equity"]
    available = engine_alloc - deployed

    # Combined sub-engine check: total sub-engine deployment ≤ 48% of equity
    combined_deployed = sub_engine_ledger["combined_sub_engine_deployed"]
    combined_max = equity * 0.48
    if combined_deployed >= combined_max:
        return 0.0  # All sub-engine capital exhausted — composite only

    return max(0.0, min(available, combined_max - combined_deployed))
```

---

## 12. OUTCOME TRACKING — sub_engine_outcomes.json

> **v1.1 CORRECTION (A.5):** IC must be computed against a FIXED-HORIZON forward return,
> not realized `pnl_pct` (variable hold). Each record MUST add a `forward_return_Nd`
> field, where N is the engine's native horizon (A,B,C=10; E=20; F=15), computed from bar
> data at entry+N regardless of actual exit timing. `sub_engine_calibrator.py` uses
> `forward_return_Nd` for Spearman IC — never realized P&L. Realized `pnl_pct` is still
> stored for P&L reporting, but is NOT the IC target. This is the same horizon discipline
> the daily_recap.py Sharpe fix requires.

### 12.1 Structure (per trade record)

```json
{
  "symbol": "NVDA",
  "engine_id": "A",
  "entry_date": "2026-05-20",
  "exit_date": "2026-05-28",
  "hold_days": 8,
  "entry_price": 895.20,
  "exit_price": 942.10,
  "pnl_pct": 5.24,
  "exit_reason": "trail_stop_A",
  "engine_score_at_entry": 0.78,
  "cluster_scores_at_entry": {"TREND": 1.82, "VOL": 0.94, "MR": -0.31, "VOLAT": 0.12, "REV": 0.08},
  "exclusive_signals_at_entry": {
    "mom_12_1": 0.34,
    "hi52_proximity": 0.8,
    "roc_accel": 0.022
  },
  "entry_params": {"stop_mult": 3.2, "trail_scale": 1.28, "hold_target_days": 14, "kelly_scale": 0.75},
  "health_at_exit": 0.18,
  "composite_at_exit": -0.3
}
```

### 12.2 IC Calibration Flow (sub_engine_calibrator.py, runs post-close)

**RF-2 FIXED (2026-05-31):** IC target corrected from `forward_pnl_pct` (variable-hold P&L)
to `forward_return_Nd` (entry-anchored fixed-horizon return). This aligns the spec with A.5
and with the `outcome_tracker.py` RF-1 fix. The pseudocode below reflects the corrected spec.

```
For each engine with >= 30 closed trades:
  1. Load sub_engine_outcomes.json — filter by engine_id
  2. Purge overlapping return windows (embargo = IC_HORIZON_DAYS per engine)
     — eliminates label leakage from overlapping forward return windows (RF-2, R-2)
  3. For each signal component (3-5 per engine):
     spearman_ic = scipy.stats.spearmanr(
         component_values_at_entry,
         forward_return_Nd          # ← CORRECTED: entry-anchored N-day return (NOT forward_pnl_pct)
     ).correlation
     # forward_return_Nd = (price at entry_date + N) / entry_price - 1.0
     # N = IC_HORIZON_DAYS[engine_id] (A/B/C/F=10d, E=20d, D=1d) — immutable
  4. icir = spearman_ic / std(rolling_IC_series)  # rolling window = 20 trades
  5. BH FDR correction across all signals — statistical_validation.py
  6. weight_raw = max(0, icir) for signals passing FDR
  7. weights = weight_raw / sum(weight_raw)  — normalize to 1.0
  8. Write to sub_engine_params.json under engine_id
  9. Log: "Engine A weights updated — 47 purged trades. IC: mom12={0.31}, hi52={0.28}, ..."

For engines with < 30 trades:
  Equal weights maintained — DO NOT calibrate on insufficient data
  Log: "Engine B — 18 trades (need 30). Equal weights maintained."
  NOTE (RF-3): 30-trade gate is a sign/sanity screen only. Reliable IC estimation
  requires ~3,100 observations. Live-capital weighting should not treat IC@n=30 as evidence.
```

---

## 13. MARGIN GUARD UPDATES

### 13.1 Changes to margin_guard.py

```python
def check_margin_safety_with_engines(dm, sub_engine_ledger=None):
    """
    Extended margin guard that also checks per-engine capital budget.
    Returns: (allowed, max_new, reason, engine_budgets)
    """
    allowed, max_new, reason = check_margin_safety(dm)
    if not allowed:
        return allowed, max_new, reason, {}

    engine_budgets = {}
    if sub_engine_ledger:
        equity = float(dm.alpaca.get_account()["equity"])
        for eid in ["A", "B", "C", "D", "E", "F"]:
            engine_budgets[eid] = get_engine_available_capital(eid, equity, sub_engine_ledger)

    return allowed, max_new, reason, engine_budgets
```

---

## 14. BATCH FILES — NEW RUNNERS

```batch
# Start_SubEngine_Premarket.bat (9:05 AM — Engine D gap scan)
@echo off
:loop
python engine_d_gap.py
timeout /t 60
goto loop

# Start_SubEngine_Scan.bat (9:35 AM — alongside main.py)
@echo off
python trade_classifier.py --mode=classify
python engine_f_breakout.py
python engine_e_rotation.py
python engine_a_momentum.py
python engine_b_reversal.py
python engine_c_squeeze.py
python main.py  # composite runs last — gets unclaimed candidates

# Start_SubEngine_Calibrator.bat (post-close, 4:30 PM)
@echo off
python sub_engine_calibrator.py
```

---

## 15. RAPTOR_SKILL.md ADDITIONS REQUIRED

Add the following to RAPTOR_SKILL.md after Section 14:

```markdown
## 18. PARALLEL SUB-ENGINE ARCHITECTURE — v6.0

### Overview
6 sub-engines run in parallel with Engine 0 (composite). Each claims candidates
with priority (F > E > A > B > C > D pre-market). Composite gets unclaimed symbols.
Capital: 8% per engine (48% total sub-engine pool). Composite: 52%.

### Engine Summary Table
| ID | Name | Cluster | Duration | Stop | Trail | Primary Exit |
|----|------|---------|----------|------|-------|-------------|
| A | Momentum Continuation | TREND+VOL | 7-25d | 2.8-3.5 ATR | 1.2-1.4× | ADX rollover / MA collapse |
| B | Mean Reversion | MR+REV | OU half-life × 1.5 | 2.0-2.5 ATR | 0.75× | AVWAP reached / time stop |
| C | Squeeze Break | VOLAT | 3d → extends | 1.5-1.8 ATR | 0.8× | Wrong direction / no fire |
| D | Gap Reversion | — | Same day | 1.0 ATR | — | Gap filled / 120min |
| E | RS Rotation | TREND | 12-35d | 3.0 ATR | 1.2× | RS lost vs sector |
| F | Structural Breakout | TREND+VOL | 10-20d | Base low | 1.1× | Base violated / measured move |

### New Files
trade_classifier.py, sub_engine_params.json, sub_engine_ledger.json,
sub_engine_outcomes.json, sub_engine_calibrator.py,
engine_{a,b,c,d,e,f}.py

### Key Constraints
- Engine D: long-only, pre-market classification, same-day exit mandatory
- Engine C: phases (squeeze_firing → break_confirmed) — params update mid-trade
- Engine F: stop = base low (structural), not ATR-based
- Engine E: requires sector ETF bars (new data fetch in data_feeds.py)
- IC calibration: minimum 30 trades per engine before weight update
- Capital: per-engine budget tracked in sub_engine_ledger.json
```

---

## 16. DATA FEEDS ADDITIONS (data_feeds.py)

Engine E requires sector ETF bars — one new fetch per sector ETF:

```python
SECTOR_ETFS = ["XLK", "XLF", "XLV", "XLE", "XLY", "XLI", "XLB", "XLC", "XLU", "XLRE", "XLP"]

def get_sector_bars(self, lookback_days=80):
    """Fetch daily bars for all sector ETFs. Used by Engine E only."""
    sector_bars = {}
    for etf in SECTOR_ETFS:
        bars = self.get_bars(etf, lookback_days)
        if bars is not None and len(bars) >= 60:
            sector_bars[etf] = bars
    return sector_bars

def get_symbol_sector(self, symbol):
    """Map symbol to sector ETF. Uses universe_builder's sector metadata."""
    # universe_builder already has sector data — pass through
    return self.sector_map.get(symbol, None)
```

Engine D requires prior-day close and overnight data:

```python
def get_overnight_data(self, symbol):
    """Return prior close and pre-market bars for gap detection."""
    prior_close = self.get_prior_close(symbol)
    premarket_bars = self.get_premarket_bars(symbol)  # if Alpaca supports
    return {"prior_close": prior_close, "premarket_bars": premarket_bars}
```

---

## 17. BUILD ORDER — SEQUENCED IMPLEMENTATION

> **v1.1 NOTE:** A new **Sprint 0** precedes everything (see Section A.11). Engine D is
> removed from this sequence — it is a separate intraday runtime (Section A.6). The
> sequence below is for the five daily-bar engines (A, B, C, E, F) + composite.

```
SPRINT 0 — Plumbing preconditions (NOTHING works without these — Section A)
  0a. engine_id end-to-end: record_entry writes engine_id + full entry_params;
      monitors look up by (model, symbol) key, not symbol alone (A.1)
  0b. symbol_ownership.json — global cross-engine symbol lock (A.2)
  0c. unified sizing formula — engine_base_kelly + 8% cap reconciliation (A.3)
  0d. no-metadata fallback → treat as Engine 0 / composite (A.1)
  0e. fix daily_recap.py horizon bugs (shared forward-return machinery) (A.5)
  0f. TEST against shared Alpaca account before any engine takes capital

SPRINT 1 — Foundation (before any engine runs)
  1. sub_engine_ledger.json — define structure, write loader/saver
  2. sub_engine_outcomes.json — define structure, write logger
  3. sub_engine_params.json — define structure, initialize equal weights
  4. trade_classifier.py — compute_cluster_subscores(), compute_cluster_dominance()
     shadow mode only — classify but do not trade
  5. margin_guard.py — add engine budget awareness
  6. main.py — add sub-engine priority gate, engine budget check

SPRINT 2 — First engines (low-complexity)
  7. Engine B — mean reversion (builds on existing MR cluster)
     NOTE: respect inverted MR sign convention (A.4) — oversold = HIGH +MR
     Add avwap, stochastic divergence, volume climax signals
     Validate: OU theta estimation on 10 symbols before live
     (Engine D REMOVED from initial build — separate intraday runtime, see A.6)

SPRINT 3 — Core momentum engines
  9. Engine A — momentum continuation
     Add mom_12_1, hi52_proximity, roc_accel
     Validate: cross-sectional IC of exclusive signals vs 20d returns
  10. Engine F — structural breakout
      Add volume profile (new utility), consolidation detection
      Add measured move computation

SPRINT 4 — Advanced engines
  11. data_feeds.py — add sector ETF bars fetch
  12. Engine E — RS rotation (requires sector data infrastructure)
  13. Engine C — squeeze break (requires TTM squeeze and HV ratio)

SPRINT 5 — Calibration and integration
  14. hold_monitor.py — engine-aware health branches for all engines
  15. exit_monitor.py — engine-aware exit logic for all engines
  16. sub_engine_calibrator.py — IC-weighted parameter updates
  17. daily_recap.py — per-engine P&L, IC, and classification breakdown
  18. RAPTOR_SKILL.md — update with Section 18

SPRINT 6 — Validation
  19. Shadow mode: run all engines for 2 weeks, log classifications without trading
  20. Verify dominance ratios, signal distributions, classification frequencies
  21. Set empirical thresholds from shadow mode data (NOT from assumptions)
  22. Go live with 50% of engine Kelly (half-Kelly for all engines until 30 trades each)
```

---

## 18. VALIDATION GATES — BEFORE EACH ENGINE GOES LIVE

Each engine must pass these checks before taking real capital:

```
Signal distribution check:
  Run engine on 60 days of historical bars
  Verify each exclusive signal is non-degenerate (std > 0, no NaN > 20%)
  Verify cross-sectional z-scores have mean ≈ 0, std ≈ 1

Classification frequency check:
  Engine should classify 2-8% of universe per scan (not too rare, not too common)
  If classifying > 15% → thresholds too loose → tighten dominance_threshold
  If classifying < 1% → thresholds too tight → loosen or check signal computation

Priority conflict check:
  Over 60-day shadow: what % of Engine A signals were also Engine E candidates?
  High overlap → adjust priority rules or add mutual-exclusion criteria

Capital utilization check:
  8% per engine × 6 engines → in practice, 2-4 engines active simultaneously
  Combined sub-engine deployment should average 20-35% of equity
  If averaging > 40% → reduce max_orders_per_engine by 1

Calibration readiness check:
  After 30 trades per engine: run sub_engine_calibrator.py
  Verify IC values are meaningful (>0.05 for at least 3 of 5 signals per engine)
  If IC uniformly near 0 → signal selection needs revision before trusting weights
```

---

*End of Blueprint — v1.0*
*Next update: after Sprint 1 completion — add empirical shadow-mode threshold values*
