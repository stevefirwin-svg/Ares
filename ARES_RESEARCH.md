# ARES_RESEARCH.md — Research Backlog

*Items here require offline IC validation against `sub_engine_outcomes.json`
before promotion to PROPOSED. Theoretical merit alone is not a build trigger.*

Last updated: 2026-05-29

---

## R-1 — Deflated Sharpe Ratio + False Discovery Rate Control

**Sprint target:** Sprint 5 (alongside IC calibration — requires 30 closed trades per engine)

**Problem:** With 5+ engines and multiple signals per engine, naive IC gates will
produce false positives from multiple testing alone. At 95% confidence, ~5% of
tested signals pass by noise. Current IC > 0.05 / ICIR > 0.5 gates are necessary
but not sufficient.

<!-- RF-3 MITIGATED 2026-05-31: The 30-trade gate is now explicitly documented as a
sign/sanity screen across ARES_MASTER_PLAN.md, ARES_SKILL.md, and statistical_validation.py.
R-1 (FDR) remains gated on data — correct behavior. The prerequisite framing below
reflects the updated understanding: R-1 is valuable but runs on top of a gate that
cannot detect IC=0.05 reliably; document that FDR/DSR correct multiple-testing noise,
not the underlying small-n power problem. -->

> **[RF-3 MITIGATED — 2026-05-31]** R-1's FDR correction is still the right approach, but the 30-trade gate it sits on is now explicitly a sign/sanity screen, not a power gate. See ARES_MASTER_PLAN.md §ARBITRARY CONSTANTS and ARES_SKILL.md §Signal Lifecycle for the updated language. Build R-1 as planned; label its output as "FDR-corrected but underpowered" until n >> 30.

**Fix:**
- Benjamini-Hochberg FDR correction applied at IC calibration across all signals
  per engine before any signal weight is promoted to LIVE
- Deflated Sharpe Ratio (Bailey & López de Prado) applied at engine-level
  evaluation to correct for the number of parameter combinations tested during shadow

**Reference:** Bailey & López de Prado (2014) — "The Deflated Sharpe Ratio: Correcting
for Selection Bias, Backtest Overfitting and Non-Normality"

**Implementation gate:** Sprint 5 — add to `statistical_validation.py` alongside
IC weighting. Do not build before 30 closed trades per engine exist.

---

## R-2 — Purged IC Computation with Embargo

**Sprint target:** Sprint 5

**Problem:** IC computed on overlapping forward return windows has label leakage.
If IC horizon is 10 days and windows overlap, the same return appears in multiple
IC observations, inflating ICIR and producing falsely confident signal weights.

**Fix:**
- When computing IC for signal promotion decisions, use non-overlapping windows
- Embargo period = IC horizon (10d for A/B/C/F, 20d for E) between train and
  test observations
- This does NOT change the IC horizon (still immutable once trades accumulate) —
  it changes the sampling method used during Sprint 5 calibration

**Reference:** López de Prado (2018) — "Advances in Financial Machine Learning",
Chapter 7 (Purged K-Fold CV)

**Implementation:** modify IC computation in `statistical_validation.py` to skip
overlapping return periods when computing ICIR across rolling windows.

---

## R-3 — Beta / Factor Decomposition Check

**Sprint target:** Post Sprint 5 (requires engine returns history)

**Problem:** Engine IC may be measuring beta exposure (market, momentum, size)
rather than genuine alpha. Engine A in particular may be harvesting the 12-1
momentum factor rather than edge. Without decomposition, IC confirmation is
ambiguous — it could mean edge or it could mean factor loading.

**Fix:**
- After 30+ closed trades per engine, regress engine returns against:
  - SPY beta (market factor)
  - 12-1 momentum factor (Jegadeesh-Titman)
  - Relevant sector factor (XL* ETF dominant in engine's holdings)
- Compute residual alpha IC (IC on factor-neutral returns)
- If residual IC < raw IC by > 30%, engine is harvesting factor, not alpha —
  flag for architecture review before capital expansion

**Not a blocker.** This is a diagnostic, not a gate. Engines proceed to live
capital on raw IC gates. Factor decomposition informs whether to continue or
redesign after sufficient history accumulates.

**Reference:** Fama & French (1993); Jegadeesh & Titman (1993)

---

## DEFERRED — Low Priority / Scale-Dependent

| Item | Reason Deferred | Revisit When |
|------|----------------|--------------|
| Hidden Semi-Markov Model (HSMM) | Hamilton Filter with probability blending already addresses cliff-switching. HSMM is an upgrade, not a current gap. | HMM shows instability after 6 months live |
| Meta-Labeling (P(profitable \| signal fired)) | Requires substantial trade history before meaningful training. Theoretical value is high. | 200+ closed trades across all engines |
| Tail Dependence Matrix (copula / Kendall tau across engines) | Cross-engine crisis correlation is a real risk, but not actionable at $50K. | Capital scale > $200K |
| Convex Portfolio Optimization (HRP / risk budgeting) | Equal-weight engine allocation is suboptimal long-term. Premature before engine performance history exists. | Post Sprint 6, 100+ closed trades per engine |

---

## PROMOTION RULES (from ARES_SKILL.md)

```
ARES_RESEARCH.md → PROPOSED:
  Trigger: IC > 0.05 computed offline against sub_engine_outcomes.json
  That offline test is the first gate — no implementation until it passes.
  Theoretical merit alone is not a build trigger.
```
