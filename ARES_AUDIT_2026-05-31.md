# Ares — Full System Audit (Code + Math + Wiring)
*Auditor pass: 2026-05-31 | Scope: all 60 files, ~20.4k LOC | Standard applied: the project's own "every number must be derivable" + senior-dev wiring scrutiny + PhD-quant math review*

**Session 6 update (2026-06-03):** All RF-1..27 fixes verified in code. All engines promoted from shadow to live. Task Scheduler fully configured. System now accumulating live paper-trade data for IC calibration.

---

## TL;DR — Grade: **B−**

A genuinely strong skeleton with disciplined I/O and an honest self-critique culture, but **several load-bearing wires are disconnected** and **two numeric defects silently override the math they sit on top of.** None bite *today* because the system is in shadow with 0 closed trades — which is precisely why this is the cheapest possible moment to fix them, before any history is written against a biased horizon.

The headline finding for your specific asks:

- **No more building "besides what is gated"? — False.** At least four items need building that are **not** gated on trade count: the calibrated-weight consumption path (RF-14), the shadow forward-return backfill (RF-15), the sizing-floor reconciliation (RF-13), and the forward-return horizon fix (RF-12). These are wiring/math gaps, not data gaps.
- **No ungated numeric fallbacks? — One real violation.** All five engines fabricate `equity=$50k / buying_power=$25k` on an account-fetch failure and keep going (RF-19). Every other fallback I found (EVT round stops, equal weights, bootstrap Kelly) **is** correctly gated on `n_trades=0`.

What's genuinely done well: atomic `tmp+os.replace` writes everywhere; the symbol-ownership lock is correct (entry claims, full exit releases, trim retains); **RF-1 is genuinely closed** (forward return is entry-anchored); EVT is honestly gated to fallbacks at `n_obs=0`; logging is handler-based to `logs/`; the entry→exit→outcome journaling path writes `signals_at_entry` and `macro_regime` correctly; all 60 files compile clean; engines correctly avoid Raptor's `signals.py`.

| Layer | Sub-grade | One-line |
|-------|-----------|----------|
| Plumbing / I/O / ledger / ownership | A− | Atomic, correct, well-instrumented |
| Data integrity discipline | B | Gated fallbacks honest; one ungated violation (RF-19); IEX feed bias (RF-22) |
| Math (signals, OU, EVT, GPD, BH-FDR) | B | Formulas correct where implemented; horizon (RF-12) and Kelly floor (RF-13) are wrong |
| End-to-end calibration wiring | **D** | IC weights unconsumed (RF-14); shadow IC never computed (RF-15); LW unused + RMT absent (RF-17) |
| Macro regime integration | C+ | HMM sound but near one-hot (RF-4); 3/5 engines on cliff, 2/5 on blend (RF-16) |
| Self-documentation / honesty | A | RED-FLAG register is exemplary; this audit just extends it |

**Bottom line:** the 30-trade gates are necessary but **not** the real blockers to go-live. Even with 30 clean trades per engine tomorrow, the system would calibrate weights nothing reads, against a shadow loop that produces no IC, while sizing every position at a flat 2%. Fix the wiring first.

---

## How to read this

New findings follow your existing `RED-FLAG` convention (RF-12 … RF-23) so they merge into `ARES_MASTER_PLAN.md` and stay greppable. Every finding cites `file:line` evidence and gives a concrete fix. Severity uses your scale; CAT uses MATH / ARCH / HYGIENE.

---

## CRITICAL & HIGH FINDINGS (new)

### RF-14 — CRITICAL / ARCH — Calibrated signal weights are written but never consumed
**Evidence:** `grep sub_engine_params engine_*.py` → no match. Only `statistical_validation.py` touches the file (it writes it). Each engine's `compute_engine_score` hardcodes equal weights — e.g. `engine_b.py:386` `score = (avwap_score + stoch_score + vol_score) / 3.0`; same pattern in A/C/E/F.
**Why it matters:** The entire Sprint-5 value proposition — "IC-weighted signal weights per engine" — terminates in a write-only JSON. When you hit 30 trades and run the calibrator, the weights it produces influence nothing. This is **not** data-gated; the consumption wire was never run.
**Fix:** Each engine's `compute_engine_score` must load `sub_engine_params.json[engine_id]["signal_weights"]` and apply them, falling back to equal weights only when `calibrated == false`. Add a single `load_signal_weights(engine_id)` helper (shared, in `ares_config.py` or a small `ares_weights.py`) so all five engines consume identically. Verify by calibrating on synthetic trades and asserting a score change.

### RF-15 — HIGH / ARCH — Shadow forward returns are never backfilled → shadow IC is uncomputable → the SHADOW→LIVE gate is circular
**Evidence:** Engines only *append* to `shadow_classifications.json` with `forward_return_Nd: null`. `outcome_tracker.py` operates exclusively on `sub_engine_outcomes.json` (ledger closed trades). `ares_threshold_deriver.py:133` reads shadow entries "where forward_return_Nd has been filled in" — but nothing fills them.
**Why it matters:** Rule 19 requires `IC>0.05 AND ICIR>0.5` *before* a signal goes live. That IC can only come from shadow (you're not live yet). But shadow forward returns are never computed, so the only IC the system can ever produce comes from *live* closed trades — which requires already being live. The validate-before-risking-capital loop is structurally open. (This also makes the live-capital shadow gate a pure count of classifications with no quality check.)
**Fix:** Add a `--shadow-forward` pass to `outcome_tracker.py` that, for each shadow classification past its horizon, calls the (fixed, see RF-12) `fetch_price_after(symbol, date, IC_HORIZON_DAYS[engine])` and writes `forward_return_Nd` + `outcome_tagged=true`. Then `ares_signal_audit.py` / `statistical_validation.py` can compute *shadow* IC and the Rule-19 gate becomes real. This is the highest-leverage fix in the system — it's the only thing that makes "shadow validation" mean anything.

### RF-12 — HIGH / MATH — Forward-return horizon is biased ~+40% (samples ~14 trading days for a declared 10-day horizon)
**Evidence:** `outcome_tracker.py:68-97`. `start_dt = entry + (days-2)` **calendar** days; window end `entry + int(days*1.45)+5` calendar; then `idx = min(days-1, len(bars)-1)` indexes by **trading-day** count into that window. Simulated for a Monday entry, 10-day horizon: window starts at trading-day ~6, has ~9 bars, `idx=8` → returns **trading day ~14**. Engine E's 20d → ~28d.
**Why it matters:** The IC horizon is declared *immutable* (Rule 8) and IC is the central calibration quantity. Measuring a 14-day return and labeling it 10-day systematically contaminates every IC, ICIR, FDR, and weight downstream. It is dormant only because there are 0 closed trades — fix it now and zero history is lost.
**Fix:** Replace the windowing with: fetch ascending daily closes starting at `entry_date` (inclusive of buffer for holidays), drop the entry bar, and select index `N-1` of the *trading-day* list (the Nth trading bar strictly after entry). Pseudocode:
```python
bars = fetch_daily_closes(symbol, start=entry_date, end=entry_date + int(N*1.6)+7)
closes_after_entry = [b for b in bars if b.date > entry_date]   # ascending
if len(closes_after_entry) < N: return None
fwd_price = closes_after_entry[N-1]["c"]
```
Apply the identical helper to the RF-15 shadow backfill so live and shadow horizons match exactly.

### RF-13 — HIGH / MATH — The Kelly clip *floor* silently overrides all sizing during the entire bootstrap phase
**Evidence:** `KELLY_BOOTSTRAP_FRACTION=0.025 × KELLY_SHADOW_MULTIPLIER=0.5 = 0.0125` (`ares_config.py:76,79`). Every engine's `KELLY_ENGINE_CLIPS` has `min_pct=0.02` (`ares_config.py:85-90`). Sizing does `kelly_dollar = max(equity*min_pct, min(equity*max_pct, equity*0.0125*scale))` (`engine_a.py:588-589`, `engine_b.py:428-431`, C/E inline, `engine_f.py:617`). Since `0.0125*scale ≤ 0.0125 < 0.02` always, the `max(equity*0.02, …)` floor wins every time.
**Why it matters:** Every bootstrap position is a flat **2% of equity ($1,000)** regardless of conviction. `kelly_scale` (engine-score conviction), the half-Kelly `KELLY_SHADOW_MULTIPLIER`, and — for C/E — the `macro_mult` blend are **all inert**, because they only ever push sizing *below* the floor. "Go live at 50% of engine Kelly" (Rule 6 / Capital Model) is mathematically vacuous: 50% of 2.5% (=1.25%) is below the 2% floor, so you are actually live at a fixed 2%. The bootstrap phase is the *only* phase that exists until 30 trades, so this governs 100% of near-term sizing.
**Fix (pick one, document why):**
- (a) Lower `min_pct` below the effective bootstrap fraction (e.g. `0.01`) so conviction/macro scaling lives inside the band; or
- (b) Make the floor *not apply* in bootstrap (only clip `max_pct`), letting size = `equity*0.0125*scale*macro_mult`; or
- (c) Raise `KELLY_BOOTSTRAP_FRACTION×SHADOW_MULTIPLIER` above `min_pct` and re-derive.
Whichever you choose, add an assertion: `assert KELLY_BOOTSTRAP_FRACTION*KELLY_SHADOW_MULTIPLIER >= min_pct or floor_disabled_in_bootstrap`. Note the comment at `ares_config.py:73` ("0.5 × composite fallback") references a *composite* Ares deliberately does not have — strike it.

### RF-19 — HIGH / ARCH — Ungated fabricated account state on broker-fetch failure (violates Rule 3)
**Evidence:** All five engines: `engine_a.py:695-697`, `engine_b.py:529-531`, `engine_c.py:430-432`, `engine_e.py:432-434`, `engine_f.py:728-730`:
```python
except Exception:
    equity = TOTAL_EQUITY          # 50_000 invented
    buying_power = equity * 0.5    # 25_000 invented
```
Then the scan continues to sizing and (in live) order submission.
**Why it matters:** This is exactly the fabricated fallback Rule 3 forbids ("Missing data → skip with warning. Never substitute invented values"). In live mode, if Alpaca is down/rate-limited/401, an engine sizes against a fake $50k/$25k and can submit orders divorced from the real account. It is **not** gated by lack of trades.
**Fix:** On account-fetch failure, in `status == "live"` **abort the scan** (`return []` with a logged error). In shadow it's harmless (no orders), but the cleanest rule is: live entries require a real account snapshot — no snapshot, no entries.

---

## MEDIUM FINDINGS (new)

### RF-16 — MED / ARCH — Macro gating is inconsistent: A/B/F use the argmax label (cliff); C/E use the probability blend
**Evidence:** `engine_a.py:606-607` and `engine_b.py:448-449` and `engine_f.py` `get_macro_multiplier`: `regime = macro.get("macro_regime"); multiplier = GATE[id].get(regime, 0.0)` — pure discrete cliff. `engine_c.py:350-368` and `engine_e.py` build `multiplier = Σ probs[r]*gates[r]` — the intended blend.
**Why it matters:** Directly contradicts Rule 17 and SKILL §Hamilton ("no cliff-switching… fires on a blend"). Compounds RF-4: even after you temperature-smooth the posterior, A/B/F will still snap on the argmax. Identical infrastructure should gate identically.
**Fix:** Promote the C/E blend logic into the shared `get_macro_multiplier` (one implementation, imported by all five). Pairs with the RF-4 fix (stickier transition prior / `covariance_type='diag'` / posterior temperature) so the blend is actually non-degenerate.

### RF-17 — MED / MATH+ARCH — Ledoit-Wolf is computed-but-unused; RMT/Marchenko-Pastur is entirely absent (Rule 18 unmet in substance)
**Evidence:** `statistical_validation.py:451` computes `shrunk_cov`, but its only consumers are `:457` (a logged "off-diag reduction" cosmetic) — it never enters weight derivation (`:472-510`, weights = normalized `max(0,ICIR)`). `grep -niE "marchenko|pastur|rmt|eigenvalue|random matrix" *.py` → **no match anywhere.** Also the LW formula at `:302-309` is an ad-hoc expression (includes a `mu**2*p` term with no basis in Ledoit-Wolf 2004), not the LW analytical shrinkage intensity.
**Why it matters:** Rule 18 ("Ledoit-Wolf + RMT are Sprint-5 co-requirements; IC calibration must not be built without shrinkage") and the master-plan claim "Ledoit-Wolf + RMT covariance cleaning" are *documented but functionally false*. With only 3–5 signals/engine the covariance is tiny, so the practical impact today is small — but the doc claims a rigor the code doesn't deliver.
**Fix:** Either (a) actually use shrinkage — e.g. weights ∝ `shrunk_cov⁻¹ · IC` (a shrunk mean-variance / Grinold-Kahn optimal allocation) instead of raw ICIR; replace the hand-rolled formula with `sklearn.covariance.LedoitWolf`; and add Marchenko-Pastur eigenvalue clipping for the cross-engine covariance (R-1/ARCH-5 scale); **or** (b) honestly downgrade Rule 18 and the master-plan line to "ICIR-weighted; LW/RMT deferred to ARCH-5 (100+ trades)" so spec = impl. Given 3–5 signals, (b) is defensible now and (a) belongs at the cross-engine layer.

### RF-18 — MED / HYGIENE — Engine E signal-name mismatch + score uses 4 of 5 declared signals
**Evidence:** `engine_e.py` emits `e_rs_new_high` (signals dict + `shadow_classifications.json` E-QCOM). But `statistical_validation.py:73` `ENGINE_SIGNALS["E"]` and `sub_engine_params.json` use `e_new_rs_high`. And `engine_e.py:324` `compute_engine_score(rs20, rs60, sec_rs, rs_accel)` takes only 4 args — `e_rs_new_high` is logged but never scored.
**Why it matters:** E's 5th "signal" is dead three ways: misnamed (IC always "insufficient_data"), unscored (no effect on entries), and its 0.20 bootstrap weight maps to nothing. The engine advertises 5 signals and runs on 4 at 0.25 effective weight each.
**Fix:** Pick the canonical name (`e_rs_new_high`), fix `ENGINE_SIGNALS["E"]` and `sub_engine_params.json` to match, and either wire the new-RS-high boolean into `compute_engine_score` (e.g. as a small additive bonus) or drop it from the registry/params so the count is honest.

### RF-20 — MED / ARCH — Universe contains leveraged & inverse ETFs that violate multiple engines' modeling assumptions
**Evidence:** `universe.json` includes TQQQ, SQQQ, SOXL, SOXS, TZA, SPDN, TSLL, NVD, PLTD (and crypto-proxy BITO/IBIT). `ares_universe.py:54` `HARD_EXCLUSIONS = {"BRK.A","BRK.B"}` — only structural names excluded; the `len(symbol) <= 5` filter does not catch these.
**Why it matters:** Daily-rebalanced leveraged/inverse products exhibit volatility decay and path dependence — they do **not** mean-revert (breaks Engine B's OU thesis), their ATR-based stops are mis-scaled (2–3× vol), and their "momentum" is a leverage artifact, not institutional accumulation (pollutes Engine A/E/F). Engine B firing on SOXS or TZA is a structurally wrong trade.
**Fix:** Add a leveraged/inverse exclusion to `HARD_EXCLUSIONS` (maintain a denylist, or filter on Alpaca asset attributes / known 2x-3x/inverse families). Revisit whether crypto-proxy ETFs belong in a swing-equity universe.

---

## LOW FINDINGS (new)

| ID | Cat | Finding | Evidence | Fix |
|----|-----|---------|----------|-----|
| RF-21 | HYGIENE | Universe filter config drift — two disagreeing sources of truth | `ares_config.py:208-212` (5.0/1000/0.01) vs `ares_universe.py:48-52` (3.0/500/0.008); builder never imports the config constants | Make `ares_universe.py` import `UNIVERSE_*` from `ares_config.py`; delete the dead config values or the hardcodes |
| RF-22 | HYGIENE | IEX feed used for screening, signals, exits, **and** forward returns | `feed:"iex"` in `ares_universe.py:113`, `ares_exit_monitor.py:75`, `outcome_tracker.py:86`, engine fetchers | IEX ≈ 2–3% of consolidated tape → dollar/share-volume filters and volume signals (`b_volume_climax`, `f` breakout-vol) are computed on a biased sample. For live, switch to SIP; document the paper/live feed split |
| RF-23 | HYGIENE | Raptor files coexist in the Ares directory and share the `ledger` import | `main.py:4-7` imports Raptor `config`/`signals`/`data_feeds`; 15+ Raptor files present (`exit_monitor.py`, `hold_monitor.py`, `signals.py`, `backtest.py`, `factor_lab.py`, `watchdog.py`, `agent_layer.py`, `market_agent.py`, …) | Move Raptor files out of the Ares repo (or into a `raptor/` subdir excluded from the scheduler). Operational footgun: a scheduled task or operator can run the wrong `watchdog.py`/`exit_monitor.py` |
| RF-24 | HYGIENE/MATH | Ledger multi-trim `pnl` reflects only the final tranche | `ledger.py:182-183` (record_trim → shares_after==0 path) reprices remaining shares from entry, ignoring earlier realized trims | For Engine F (40% trim then exit), set closed `pnl` = Σ(trim pnls) + final-tranche pnl |
| RF-25 | MATH | BH-FDR is applied per-engine (3–5 tests), not across all ~19 system signals | `statistical_validation.py:422` calls BH within `calibrate_engine` | R-1 intends system-wide FDR; collect all engines' p-values and run one BH pass, or document the per-engine choice |
| RF-26 | MATH | Deflated Sharpe is inert (`n_trials=1` → `e_max_sr=0`) | `statistical_validation.py:558,594-595` | Pass the real number of shadow threshold-sweep combinations once `ares_threshold_deriver` runs; until then it's just `P(SR>0)` |
| RF-27 | HYGIENE | Dead/confusing branch in GPD quantile | `evt_calibrator.py` `gpd_quantile` `... if False else u + sigma*3` | Remove the `if False` artifact; keep the `u + sigma*3` clamp |

---

## Status of your pre-existing RED FLAGS (verified against code this pass)

| ID | Verdict | Note |
|----|---------|------|
| RF-1 | **Genuinely CLOSED** | `outcome_tracker.run_forward_fill` anchors on `entry_price` + `entry_date+N`. (But see RF-12: the *N* it samples is biased.) |
| RF-3 | CONFIRMED open | n=30 IC gate is underpowered; correctly demoted to "sign screen" in docs but the live-weight path (RF-14) doesn't exist anyway |
| RF-4 | CONFIRMED open | `macro_context.json` posterior NEUTRAL=0.9976; `covariance_type="full"`, diag 0.92. Compounds RF-16 |
| RF-5 | CONFIRMED open | `fetch_features` builds 3 columns; sector breadth never enters the HMM |
| RF-6 | CONFIRMED open | `_state_order_to_regime` sorts on `means_[:,1]` (VIX) alone |
| RF-7 | CONFIRMED open | `evt_calibration.json` all `fallback_insufficient_data`, `n_obs=0`. Correctly gated |
| RF-8 | CONFIRMED open (partial) | No cash floor: `INITIAL_ENGINE_WEIGHTS` A,B,C,E,F = 0.20 each = 1.00 deployed, D=0.00. D double-spec is *resolved* (D=0, sum=1.00 = "5×20% D excluded"). Cash-buffer point stands |
| RF-9, RF-10, RF-11 | CONFIRMED open | Selection-bias / regime-coverage / horizon<hold concerns all valid; RF-15 makes them moot until shadow IC exists |

---

## Alpha — do you need more signals? (you asked)

**Not yet, and adding signals now would make things worse.** Reasoning, in priority order:

1. **You cannot measure the alpha of the signals you already have.** With RF-15 open, no signal has a computable IC. Adding signals multiplies the multiple-testing problem (R-1) on top of a measurement pipeline that produces nothing. **Fix RF-12 + RF-15 first; then let IC tell you which of the existing ~19 signals actually carry edge.** Several may be redundant (e.g. `a_mom_12_1` vs `a_roc_accel`; `e_rs_20d` vs `e_rs_60d` vs `e_rs_acceleration` are near-collinear by construction).
2. **The single highest-value *new* analytic isn't a signal, it's R-3 (factor decomposition).** Engine A's `a_mom_12_1` is almost certainly harvesting the Jegadeesh-Titman momentum *factor*, not idiosyncratic alpha. Until you regress engine returns on SPY-beta + 12-1 momentum + sector, a positive IC is ambiguous. This is already in `ARES_RESEARCH.md` R-3 — keep it there, don't pre-empt it with more signals.

When the pipeline works and you *do* want incremental alpha, the gaps in current coverage that are most defensible to test offline (per Rule 20 / R→PROPOSED) are:
- **Engine B:** an explicit **regime/credit-stress conditioner** on the MR thesis — capitulation MR works far better when credit isn't blowing out. You already fetch HYG/LQD for the HMM; a `b_credit_calm` gate is cheap.
- **Engine A/E:** a **liquidity/short-interest or earnings-proximity exclusion** (avoid momentum entries into a binary event) — a risk filter that should *raise* IC by removing event noise, not a new alpha source.
- **Engine F:** **relative-volume on the breakout bar vs the consolidation's own volume** (not just `f_breakout_strength` vs 20d) — distinguishes genuine accumulation breakouts from index-drag.
- **Cross-engine:** the **tail-dependence matrix** (R-1 deferred) becomes worth it only at >$200k; skip at $50k.

Each must pass the offline-IC gate against a *working* `sub_engine_outcomes.json` before promotion. No new signal should be added until RF-12/14/15 are closed.

---

## Recommended fix sequence (dependency-ordered)

```
NOW (not data-gated — do before any engine goes live, costs zero history):
  1. RF-12  Fix forward-return horizon sampling (outcome_tracker)         [MATH, ~1h]
  2. RF-15  Add shadow forward-return backfill (reuse RF-12 helper)       [ARCH, ~2h]  ← unlocks shadow IC
  3. RF-14  Wire engines to read sub_engine_params.json weights           [ARCH, ~2h]  ← unlocks calibration
  4. RF-13  Reconcile Kelly floor vs bootstrap fraction + assertion       [MATH, ~30m]
  5. RF-19  Abort live scan on account-fetch failure (no fabricated $)    [ARCH, ~30m]
  6. RF-16  Unify macro gating to the C/E blend across all engines        [ARCH, ~1h]
  7. RF-18  Fix Engine E signal name + score arity                        [HYGIENE, ~30m]
  8. RF-20  Exclude leveraged/inverse ETFs from the universe              [ARCH, ~1h]

NEXT (cleanup, lower risk):
  9. RF-21/22/23/24/25/26/27 — config drift, feed, repo hygiene, accounting

THEN (your existing register, in order):
  RF-4/5/6 (HMM), RF-8 (cash floor), RF-17 (LW/RMT decision), RF-3/9/10 (gate redesign)

GATED ON DATA (legitimately wait for ≥30 trades/engine):
  EVT rebuild (RF-7), IC calibration run, capital_allocator go-live, R-3 factor decomp
```

**Go-live readiness:** Even after the 30-trade gates are met, do not promote any engine to `live` until items 1–5 above are verified (grep/test output pasted, per your Audit-Integrity Corollary). Until then, "shadow validation" and "IC-weighted sizing" are labels, not behaviors.
