# ARES_RESEARCH.md — Mathematical Research Backlog
*Last updated: 2026-05-29 | Version: 1.0*

Items here are mathematically sound but have not yet demonstrated IC > 0.05 against
real Ares trade data. They live here until an offline IC test against
`sub_engine_outcomes.json` promotes them to PROPOSED status in ARES_MASTER_PLAN.md.

**Promotion protocol:**
1. Compute signal offline using existing bar data and `sub_engine_outcomes.json`
2. Run `spearmanr(signal_at_entry, forward_return_Nd)` across closed trades
3. If IC > 0.05: promote to PROPOSED in master plan, implement in shadow mode
4. If IC ≤ 0.05: stays here, note the IC result and retest after more data

Theoretical merit alone is not a build trigger.

---

## TIER 1 — High Priority (known calibration fixes, expected to pass IC gate)

### EVT / Generalized Pareto Distribution (Embrechts; Pickands-Balkema-de Haan)
**Status:** Promoted to Sprint 0 — build immediately (stop calibration, not a signal)
**What it does:** Fits GPD to left tail of return distributions. Derives stop multipliers
from P(stop trigger | noise) = 5% rather than round-number convention.
**Why Tier 1:** Fixes known wrong constants in `ares_config.py`. Not a signal — a calibration
method. Doesn't need IC gate because it's not predicting returns; it's parameterizing risk.
**References:** Embrechts et al. (1997) *Modelling Extremal Events*; Pickands (1975)

---

### Hamilton Filter / HMM Regime Classifier (Hamilton 1989)
**Status:** Promoted to Sprint 1 — build before any engine goes live
**What it does:** Replaces vote-count macro_context with a Hidden Markov Model.
Output: P(regime=k | observations) probability vector updated daily via Bayes.
No cliff-switching. Regime multipliers become probability-weighted blends.
**Why Tier 1:** Vote-count thresholds are architectural debt. A 0.02 score difference
currently flips all engine multipliers. HMM eliminates this discontinuity.
**Implementation:** `hmmlearn` library. ~2-3 days. Inputs: SPY trend, VIX, credit, breadth.
**References:** Hamilton (1989) *A New Approach to the Economic Analysis of Nonstationary
Time Series and the Business Cycle*; Diebold et al. (1994)

---

### Ledoit-Wolf Shrinkage (Ledoit-Wolf 2004)
**Status:** Locked to Sprint 5 — must be bundled with IC calibrator build
**What it does:** Shrinks IC estimates toward equal-weight prior.
Formula: w_shrunk = (1-α)·w_IC + α·w_equal, where α = f(sample_size).
At 30 trades: α ≈ 0.5. At 300 trades: α → 0.
**Why it matters:** Raw IC from 30 trades has high variance. Without shrinkage, the IC
calibrator overconfidently bets on noisy estimates — worse than equal weights.
**Implementation:** `sklearn.covariance.LedoitWolf`. < 1 day.
**References:** Ledoit & Wolf (2004) *A well-conditioned estimator for large-dimensional
covariance matrices*; James & Stein (1961)

---

### Random Matrix Theory — Marchenko-Pastur Cleaning (Marchenko-Pastur 1967)
**Status:** Locked to Sprint 5 — must be bundled with IC calibrator build
**What it does:** Identifies noise eigenvalues in the signal covariance matrix.
Any eigenvalue below the Marchenko-Pastur upper edge is pure sampling noise and gets
zeroed before IC weighting. Prevents overfitting to noise factors.
**Formula:** MP upper edge = σ²(1 + √(p/n))² where p=factors, n=observations
**Implementation:** `numpy.linalg.eigh`. ~1 day.
**References:** Marchenko & Pastur (1967); Bun, Bouchaud & Potters (2017)
*Cleaning large correlation matrices: Tools from Random Matrix Theory*

---

## TIER 2 — Medium Priority (additive alpha, needs IC validation)

### Heston Stochastic Volatility (Heston 1993)
**What it does:** Models variance as a mean-reverting SDE correlated with price.
dv = κ(θ-v)dt + σ√v dW
Outputs: (a) forward vol estimate for stop sizing; (b) vol-of-vol term σ that predicts
when ATR itself is about to expand — early warning of regime transition.
**Proposed signal:** `heston_vol_regime` — supplements or replaces ATR percentile in
volatility-sensitive engines (A, C, F).
**IC test method:** Calibrate Heston per stock over 90-day rolling window from bar data.
Extract vol-of-vol σ. Test spearmanr(σ_today, |return_10d_forward|) across universe.
Positive IC means σ predicts realized vol expansion — valid regime signal.
**Build cost:** 3-5 days. Requires optimization (scipy.optimize) per stock per calibration.
**References:** Heston (1993) *A Closed-Form Solution for Options with Stochastic Volatility*

---

### Wavelet Decomposition — Frequency-Purified Signals (Mallat 1989)
**What it does:** Decomposes price into frequency bands using Haar/Daubechies wavelets.
High-frequency residual (1-4 day) = pure mean-reversion component for Engine B.
Low-frequency component (>10 day) = pure trend component for Engine A.
Removes trend contamination from MR signals and MR noise from trend signals.
**This is preprocessing, not a new signal** — existing engine signals computed on
frequency-purified price series rather than raw price.
**IC test method:** Compute engine signals on wavelet-filtered series. Test whether
filtered-signal IC exceeds raw-signal IC on same closed trade sample.
**Build cost:** 1-2 days. `PyWavelets` library.
**References:** Mallat (1989) *A Theory for Multiresolution Signal Decomposition*;
Percival & Walden (2000) *Wavelet Methods for Time Series Analysis*

---

### Copula Theory — Tail-Dependent Portfolio Correlation (Sklar 1959; Clayton 1978)
**What it does:** Replaces Pearson correlation cap (currently 0.70 in config) with
Clayton copula lower tail dependence measure. Two positions may have Pearson ρ=0.5
in normal times but Clayton τ=0.8 — co-crash risk is far higher than Pearson implies.
**Proposed change:** Portfolio entry gate checks Clayton τ between candidate and all
open positions. No entry if Clayton τ > threshold (to be derived from EVT tail data).
**IC test method:** Not an IC test — this is a portfolio risk constraint, not a return
predictor. Validate via: does Clayton τ cap reduce max drawdown during drawdown periods
relative to Pearson cap? Test against position_ledger.json losing streaks.
**Build cost:** 2 days. `scipy.stats` for copula fitting.
**References:** Sklar (1959); Clayton (1978); Li (2000) *On Default Correlation*

---

### Transfer Entropy — Macro→Signal Causality (Schreiber 2000)
**What it does:** Measures directional information flow from macro signals to engine
factor scores. Does VIX lead Engine B's MR signals by N days? Does sector breadth
lead Engine E's RS signals? Gives empirical lag structure for macro multipliers.
**Proposed change:** Engine regime multipliers applied with empirically-derived lead time
rather than contemporaneously. VIX spike might lead Engine B entries by 2 days.
**IC test method:** Compute transfer entropy from each macro signal to each engine's
historical entry signal scores. Identify lead/lag structure. Test whether applying
the discovered lag improves IC of macro-conditioned engine signals.
**Build cost:** 2 days. `pyinform` or manual implementation via conditional entropy.
**References:** Schreiber (2000) *Measuring Information Transfer*

---

## TIER 3 — Exploratory (novel, higher complexity, validate Tier 1-2 first)

### Lévy Processes / Jump-Diffusion (Merton 1976; Kou 2002)
**What it does:** Corrects Kelly fraction for overnight gap risk (earnings, macro announcements).
Standard Kelly assumes diffusion returns — Kou's double-exponential jump model adds
jump intensity λ and asymmetric jump size distribution. Kelly becomes a function of
both diffusion variance AND jump variance. Shrinks sizing for high-jump-intensity stocks.
**Build cost:** 3 days. MLE calibration per stock from bar data. `scipy.optimize`.
**Note:** Build after Heston — both touch the return distribution assumption.
**References:** Merton (1976) *Option Pricing when Underlying Stock Returns are Discontinuous*;
Kou (2002) *A Jump-Diffusion Model for Option Pricing*

---

### Particle Filter / Sequential Monte Carlo (Gordon et al. 1993)
**What it does:** Nonlinear, non-Gaussian replacement for Kalman filter.
Estimates P(latent_composite_score | noisy_observations) exactly rather than
approximately. Handles fat-tailed noise and nonlinear dynamics that Kalman cannot.
**Note:** Kalman filter is not currently built in Ares — this would be built if/when
a latent state estimation layer is needed. Significant complexity overhead.
**Build cost:** 5+ days. ~60 sec runtime per scan. Medium-high maintenance.
**References:** Gordon, Salmond & Smith (1993) *Novel Approach to Nonlinear/Non-Gaussian
Bayesian State Estimation*

---

### LPPL / Log-Periodic Power Law — Crash Detection (Sornette 2003)
**What it does:** Renormalization group model of market bubbles and critical transitions.
Identifies when SPY/index price dynamics enter a critical state preceding phase transition.
For Ares: early warning layer in macro_context that precedes CRISIS regime classification.
**Note:** Not for individual stocks — macro regime signal only.
**Build cost:** 3 days. Non-trivial nonlinear curve fitting. Known to produce false positives.
**References:** Sornette (2003) *Why Stock Markets Crash*;
Johansen, Ledoit & Sornette (2000)

---

### Optimal Stopping Theory (Shiryaev 1978; Leung & Zhang 2019)
**What it does:** Formalizes the hold/exit decision as a free boundary problem.
The trail stop is a heuristic approximation of the true optimal stopping boundary.
Leung & Zhang (2019) apply this directly to mean-reverting positions (Engine B).
**Proposed use:** Derive Engine B's exit boundary from the OU optimal stopping solution
rather than from AVWAP proximity + time heuristic.
**Build cost:** 3-4 days. Requires solving a differential equation for the stopping boundary.
**References:** Leung & Zhang (2019) *Optimal Mean Reversion Trading*;
McKean (1965) *Appendix: A Free Boundary Problem for the Heat Equation*

---

### Empirical Mode Decomposition / Hilbert-Huang Transform (Huang 1998)
**What it does:** Fully adaptive signal decomposition — extracts intrinsic mode functions
from non-stationary price data without assuming a fixed basis (unlike wavelets).
Instantaneous frequency from the Hilbert transform of each IMF gives a real-time
regime signal that adapts to the data's own frequency structure.
**Note:** Computationally heavier than wavelets. Build only if wavelet preprocessing
(Tier 2) validates well and further frequency separation shows IC improvement.
**References:** Huang et al. (1998) *The Empirical Mode Decomposition and the Hilbert Spectrum*

---

### Topological Data Analysis — Persistent Homology (Carlsson 2009)
**What it does:** Extracts topological shape features from high-dimensional return space.
Loops (1-cycles) in persistence diagram indicate oscillatory/mean-reverting dynamics.
Bars indicate persistent trends. Completely orthogonal regime signal to all current factors.
**Note:** Computationally heavier. Niche tooling (`gudhi`, `ripser`). Build only after
Tier 1-2 math is validated. Would add to macro_context as orthogonal regime indicator.
**References:** Carlsson (2009) *Topology and Data*; Zomorodian & Carlsson (2005)

---

### Entropy Production / KL-Divergence Regime Detector (Jarzynski 1997)
**What it does:** Measures information entropy production rate by tracking KL-divergence
between today's and yesterday's return distribution across the universe.
Accelerating entropy production → regime transition is imminent (before it shows
in price-derived signals). Early warning layer complementing Hamilton Filter.
**IC test method:** Compute daily KL-divergence of return distribution. Test whether
spikes in KL-divergence lead regime transitions by N days in historical data.
**References:** Jarzynski (1997) *Nonequilibrium Equality for Free Energy Differences*;
Kullback & Leibler (1951)

---

### Maximum Entropy Portfolio Construction (Jaynes 1957)
**What it does:** Distributes position sizes to maximize portfolio entropy subject to
return/risk constraints. More diversified than mean-variance; doesn't concentrate in
eigenportfolios. Useful when covariance matrix is ill-conditioned (always true at 10
positions × 60-day window).
**Note:** Addresses the same problem as Ledoit-Wolf but at the portfolio level.
Build after Ledoit-Wolf validates — these are complementary, not competing.
**References:** Jaynes (1957) *Information Theory and Statistical Mechanics*

---

## EVALUATION PROTOCOL (for any item above)

### Step 1 — Offline IC Test (pre-implementation gate)
```python
import pandas as pd
from scipy.stats import spearmanr

# Load existing trade outcomes
outcomes = pd.read_json('sub_engine_outcomes.json')

# Compute proposed signal at entry date for each trade
signal_values = [compute_proposed_signal(row) for _, row in outcomes.iterrows()]

# Test against realized forward return (engine-specific horizon)
ic, pval = spearmanr(signal_values, outcomes['forward_return_Nd'])
icir = outcomes['rolling_ic'].mean() / outcomes['rolling_ic'].std()

# Gate: IC > 0.05 AND ICIR > 0.5
print(f"IC={ic:.4f}, p={pval:.4f}, ICIR={icir:.4f}")
print("PASS — promote to PROPOSED" if ic > 0.05 and icir > 0.5 else "FAIL — stays in research")
```

### Step 2 — Isolated Backtest
- Run baseline: Sharpe, CAGR, max drawdown, win rate
- Apply exactly one change
- Re-run same period
- Primary metric per change type listed in each item's description above

### Step 3 — Walk-Forward Stability
- Split backtest into 3 non-overlapping windows
- Improvement must appear in all 3, not concentrated in one subperiod

### Step 4 — Statistical Significance
```python
from scipy.stats import ttest_rel
t, p = ttest_rel(modified_returns, baseline_returns)
# Gate: p < 0.10 minimum; p < 0.05 preferred
```

### Step 5 — Complexity Cost Check
Before promoting, confirm:
- Implementation days (estimate realistic, not optimistic)
- Runtime cost acceptable for daily scan
- Maintenance burden acceptable long-term
- No introduction of external data dependencies not already in the system

---

## PROMOTION LOG

| Date | Item | IC Result | Decision |
|------|------|-----------|----------|
| 2026-05-29 | EVT/GPD | N/A — not a signal, calibration method | Promoted to Sprint 0 |
| 2026-05-29 | Hamilton Filter | N/A — macro architecture fix | Promoted to Sprint 1 |
| 2026-05-29 | Ledoit-Wolf | N/A — IC calibrator prerequisite | Locked to Sprint 5 |
| 2026-05-29 | RMT cleaning | N/A — IC calibrator prerequisite | Locked to Sprint 5 |
