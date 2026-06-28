# Ares — Audit Snapshot 2026-06-28 (Session 9)

*Point-in-time audit log. Not a living document — see `ARES_MASTER_PLAN.md` for current
state and the full RED-FLAG REGISTER. This file records what was found and fixed in this
session only.*

*Prior snapshot: `ARES_AUDIT_2026-06-19.md` (Session 8).*

---

## Scope

Continued code audit across files not yet reviewed in prior sessions
(`ares_universe.py`, `ares_weights.py`, `evt_calibrator.py`, `ares_diagnose.py`), plus
closing out the git hygiene/sync work that surfaced a second leaked credential.

---

## Findings (RF-42 through RF-48)

| ID | Sev | Cat | Finding | Resolution |
|----|-----|-----|---------|------------|
| RF-42 | HIGH | ARCH | `ares_watchdog.py` called `ledger.record_exit()` with wrong positional argument order at two call sites (hard-stop exit and circuit-breaker exit). Silent ledger/broker drift — the ledger would record incorrect data on every watchdog-triggered exit. | Fixed both call sites to `record_exit(engine_id, symbol, price, date, reason)`. |
| RF-43 | MED | HYGIENE | `ares_watchdog.py` `print_status()` had an f-string ternary operator precedence bug, causing PnL/stop/flag fields to render incorrectly in status output. | Fixed operator precedence. |
| RF-44 | HIGH | ARCH | `ares_universe.py` `screen_symbols()` fetched bars via the Alpaca `/v2/stocks/bars` endpoint with `feed: "iex"`. On a paper account, IEX covers only ~2-3% of the consolidated tape — the same failure mode previously found and fixed in `ares_exit_monitor.py` (RF-37). Empirically confirmed: `universe.json` (built 2026-06-19) contained only 144 symbols passing filters that should pass many more given the configured thresholds (price $3-$500, $20M+ dollar volume, 500K+ share volume, 0.8%+ ATR). | Migrated `screen_symbols()` to the shared `bars_fetch.py` module (yfinance), same fix pattern as RF-37/RF-35/RF-22. True post-fix symbol count is unknown until the next scheduled `ares_universe.py` run. |
| RF-45 | MED | HYGIENE | `ares_diagnose.py` `check_flags()` hardcoded `frac = 0.025` and `mult = 0.50` for the "effective position size" diagnostic display, instead of reading the actual `KELLY_BOOTSTRAP_FRACTION`/`KELLY_SHADOW_MULTIPLIER` values it had already parsed out of `ares_config.py`'s source text earlier in the same function. Currently harmless (values match), but would silently show a stale number if those constants were ever changed without updating this hardcode. | Now parses `frac`/`mult` as floats directly from the matched config lines, with a `warn()` + fallback to `None` if parsing fails. |
| RF-46 | MED | HYGIENE | `hamilton_filter.py` called `logging.basicConfig()` before `os.makedirs("logs", exist_ok=True)` — would crash with `FileNotFoundError` on a fresh clone with no pre-existing `logs/` directory. | Reordered: directory creation now precedes `basicConfig()`. |
| RF-47 | HIGH | HYGIENE | `daily_recap.py` had a Gmail App Password hardcoded directly in source, committed to the public GitHub repo. | Moved to `.env` / `os.getenv()` with an empty-password guard. **Steve explicitly declined to rotate the password** — it remains valid and present in pre-fix git history. |
| RF-48 | CRITICAL | HYGIENE | `env.txt` — a plaintext duplicate of `.env` containing the live Alpaca API key and secret — had been committed to the public repo (`https://github.com/stevefirwin-svg/Ares`) since the very first commit (`0179fa6`, 2026-06-03). Confirmed via `git show <commit>:env.txt` on both `HEAD` and `origin/main` at the time of discovery — fully exposed for the entire life of the public repo. | `git rm --cached env.txt`, added `env.txt` to `.gitignore`. **Steve explicitly declined to rotate the Alpaca API key/secret** — "Stop tracking, keep key as-is." The real credentials the live system reads continue to live in `.env` (gitignored, never committed). |

Audited with no fix needed (confirmed clean):
- `ares_weights.py` — `load_signal_weights()`, `weighted_score()`, `is_calibrated()` all consistent with `sub_engine_params.json` schema.
- `evt_calibrator.py` — POT/GPD fitting logic, quantile inversion, and stop-multiplier derivation all sound; the fixed mega-cap/ETF bootstrap symbol list achieves ~96% data coverage (18,240/~19,040 observations in `evt_calibration.json`), so it does not suffer the IEX-coverage problem found in `ares_universe.py` and (previously) `ares_exit_monitor.py`.

---

## Git / repository hygiene

- Confirmed `.git/index.lock` removal and subsequent `git rm --cached env.txt`, `git add -A`,
  `git commit`, `git push origin main` all executed successfully directly on Windows
  (sandbox could not delete files inside `.git/`, most likely due to Windows-side security
  software restricting deletions in that directory specifically).
- Commit `ce5a9de` pushed to `origin/main` (fast-forward from `4d7a175`, 26 files changed).
- Verified via `git fetch origin` that local `main` and `origin/main` both point to `ce5a9de`
  — no divergence, no conflicts.
- `.gitignore` now excludes: `__pycache__/`, `*.pyc`, `*.pyo`, `.env`, `_env`, `env.txt`,
  `*.patch`, `*.log`.
- Per Steve's explicit instruction, live runtime/state JSON files (ledger, universe, macro
  context, shadow classifications, symbol ownership, outcomes, etc.) remain tracked and
  committed — this was a deliberate choice, not an oversight.

---

## Outstanding from this session

- `universe.json` still reflects the pre-fix 144-symbol IEX screen until `ares_universe.py`
  runs again on its normal schedule.
- RF-7 (EVT fallback stops, all engines on ATR fallback pending `evt_calibrator.py` re-run)
  remains open — unchanged this session.
- Two credential leaks (Gmail App Password, Alpaca API key/secret) have been code-fixed /
  untracked but **not rotated**, per Steve's explicit, repeated decisions. Both secrets remain
  live and present in git history prior to their respective fix commits. This is a standing
  residual risk Steve has accepted, not an unresolved bug.
