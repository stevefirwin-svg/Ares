# ARES_STARTUP.md — Session Startup

*Read first. Every session. No exceptions.
Last updated: 2026-06-14 | Version: 1.4*

---

## ⚠️  RF-23 WARNING — RAPTOR FILES IN ARES DIRECTORY

The Ares folder contains Raptor system files (`main.py`, `exit_monitor.py`,
`hold_monitor.py`, `watchdog.py`, `backtest.py`, `signals.py`, `config.py`,
`data_feeds.py`, `agent_layer.py`, `market_agent.py`, etc.).

**NEVER schedule these Raptor files via Task Scheduler for Ares operations.**

Ares schedulable entry points ONLY:
```
hamilton_filter.py            8:45 AM ET  (macro update)
engine_a.py --scan            9:35 AM ET
engine_b.py --scan            9:35 AM ET
engine_c.py --scan            9:36 AM ET
engine_e.py --scan            9:37 AM ET
engine_f.py --scan            9:38 AM ET
ares_exit_monitor.py          9:50 AM ET + 3:55 PM ET
ares_hold_monitor.py          9:52 AM ET + 3:58 PM ET
outcome_tracker.py --all-forward  4:15 PM ET
daily_recap.py                4:20 PM ET
```

If you accidentally run the Raptor `exit_monitor.py` or `watchdog.py` instead
of `ares_exit_monitor.py` or `ares_watchdog.py`, Raptor will attempt to manage
Ares positions with the wrong exit logic. All Raptor files have a top-of-file
`⚠️ RAPTOR FILE — NOT ARES` warning.

TODO: When ready, move Raptor files to a `raptor/` subdirectory.

---

## ⚠️  SECURITY WARNING — PUBLIC REPOSITORY

The Ares GitHub repo (https://github.com/stevefirwin-svg/Ares) is **public**.

**NEVER commit the following files:**
- `_env` / `.env` — contains Alpaca API keys
- `*.log` — runtime logs
- `__pycache__/` — bytecode

These are excluded in `.gitignore`. Before every `git push`, run:
```powershell
git status --short
```
Confirm none of the above appear. If they do, run `git rm --cached <filename>` before pushing.

**If API keys are ever accidentally pushed: immediately rotate them in the Alpaca dashboard.**

---

## STEP 1 — PULL LATEST FROM GITHUB

```powershell
cd "C:\Users\steve\OneDrive\Desktop\Ares"
git pull origin main
git log --oneline -3
Remove-Item -Recurse -Force __pycache__ -ErrorAction SilentlyContinue
```

**Note the top commit hash** and paste it to Claude at session start.

---

## STEP 2 — READ FILES IN ORDER

1. **ARES_STARTUP.md** — startup sequence and system state (this file)
2. **ARES_MASTER_PLAN.md** — what is done, what is open, build order
3. **ARES_SKILL.md** — rules, engine architecture, what must never be violated

---

## STEP 3 — RUN DIAGNOSTIC

```powershell
python ares_diagnose.py
```

This prints a full system health report in ~5 seconds:
- Macro regime and staleness
- Shadow classification counts per engine (gate = 30)
- Open positions and ownership
- Closed trade counts vs calibration gate
- EVT calibration status
- Engine modes (live vs shadow)
- Log file scan for ERRORs

Then run individual engine status checks if needed:
```powershell
python engine_b.py --status
python engine_a.py --status
python engine_e.py --status
python ares_exit_monitor.py --status
```

---

## STEP 4 — VERIFY CRITICAL INVARIANTS

```bash
# 1. Symbol ownership lock present in ledger
grep -n "symbol_ownership\|engine_id" ledger.py | head -5

# 2. Engine F uses structural stop (not ATR-based)
grep -n "consol_low\|stop_price\|risk_budget" engine_f.py | head -5

# 3. No FRED data in macro_context.py
grep -n "FRED\|yield_curve\|FEDFUNDS\|T10Y2Y" macro_context.py

# 4. No signals.py import in any engine file
grep -rn "from signals import\|import signals" engine_*.py

# 5. Atomic writes present
grep -n "os.replace" ledger.py outcome_tracker.py | head -5

# 6. fetch_bars uses yfinance (not Alpaca IEX)
grep -n "yf.download\|alpaca.markets/v2/stocks/bars" engine_a.py engine_b.py | head -5

# 7. forward_return_Nd anchored at entry — RF-1 fix verified
grep -n "post_entry\|entry_dt\|days - 1" outcome_tracker.py | head -5
```

If any check fails: diagnose before writing code.

---

## STEP 5 — CONTEXT CHECK

1. What is current macro regime? Check `macro_context.json`.
2. What symbols are currently owned? Check `symbol_ownership.json`.
3. What engines are live vs shadow? Check `ares_config.py` ENGINE_STATUS.
4. What shadow counts have accumulated? Check `shadow_classifications.json`.
5. What is today's build target? See ARES_MASTER_PLAN.md open items.

---

## DAILY SCHEDULE — TASK SCHEDULER (fully operational as of 2026-06-14)

| Time ET | Task Name | Script | Log file |
|---------|-----------|--------|----------|
| 8:45 AM | Ares_Hamilton | `hamilton_filter.py` | `logs/hamilton_YYYYMMDD.log` |
| 9:35 AM | Ares_EngineA_Scan | `engine_a.py --scan` | `logs/engine_a_YYYYMMDD.log` |
| 9:35 AM | Ares_EngineB_Scan | `engine_b.py --scan` | `logs/engine_b_YYYYMMDD.log` |
| 9:36 AM | Ares_EngineC_Scan | `engine_c.py --scan` | `logs/engine_c_YYYYMMDD.log` |
| 9:37 AM | Ares_EngineE_Scan | `engine_e.py --scan` | `logs/engine_e_YYYYMMDD.log` |
| 9:38 AM | Ares_EngineF_Scan | `engine_f.py --scan` | `logs/engine_f_YYYYMMDD.log` |
| 9:50 AM | Ares_ExitMonitor_AM | `ares_exit_monitor.py` | `logs/ares_exits_YYYYMMDD.log` |
| 9:52 AM | Ares_HoldMonitor_AM | `ares_hold_monitor.py` | `logs/ares_hold_YYYYMMDD.log` |
| 3:55 PM | Ares_ExitMonitor_PM | `ares_exit_monitor.py` | `logs/ares_exits_YYYYMMDD.log` |
| 3:58 PM | Ares_HoldMonitor_PM | `ares_hold_monitor.py` | `logs/ares_hold_YYYYMMDD.log` |
| 4:15 PM | Ares_OutcomeTracker | `outcome_tracker.py --all-forward` | `logs/outcome_tracker_YYYYMMDD.log` |
| 4:20 PM | Ares_DailyRecap | `daily_recap.py` | `logs/daily_recap_YYYYMMDD.log` |

**All tasks:** `pythonw.exe` in `C:\Users\steve\OneDrive\Desktop\Ares\`, logon type = Interactive.

**Task Scheduler setup for each task:**
- Program: `C:\Users\steve\AppData\Local\Programs\Python\Python313\pythonw.exe`
- Arguments: `engine_a.py --scan` (adjust per task)
- Start in: `C:\Users\steve\OneDrive\Desktop\Ares`
- General tab → "Run only when user is logged on"
- Conditions tab → uncheck "Stop if computer switches to battery power"

**To verify tasks are configured:**
```powershell
Get-ScheduledTask -TaskPath "\Ares\" | Select TaskName, State
```

---

## POST-MORTEM LOG REVIEW

After each trading day, review logs for issues:

```powershell
# Quick error scan across all today's logs
Get-Content logs\engine_a_$(Get-Date -f yyyyMMdd).log | Select-String "ERROR|WARNING"
Get-Content logs\engine_b_$(Get-Date -f yyyyMMdd).log | Select-String "ERROR|WARNING"
Get-Content logs\ares_exits_$(Get-Date -f yyyyMMdd).log | Select-String "ERROR|WARNING"

# Full recap
Get-Content logs\daily_recap_$(Get-Date -f yyyyMMdd).log

# Or run the diagnostic to see current state
python ares_diagnose.py
```

---

## SYSTEM STATE (2026-06-14 — Session 7)

### Built and running (LIVE paper trading)

| File | Status | Notes |
|------|--------|-------|
| ares_config.py | ✓ Done | All Ares constants |
| ledger.py | ✓ Done | engine_id keyed + update_metadata |
| hamilton_filter.py | ✓ Done | HMM live, file logging added |
| macro_context.py | ✓ Done | FRED stripped |
| margin_guard.py | ✓ Done | Per-engine budget |
| outcome_tracker.py | ✓ Done | yfinance forward returns, file logging added |
| capital_allocator.py | ✓ Done | Advisory only |
| daily_recap.py | ✓ Done | INFO-level file logging fixed |
| evt_calibrator.py | ✓ Done | Fallback active — RF-7 still open |
| ares_universe.py | ✓ Done | 141 symbols, 23h cache |
| engine_a.py | ✓ LIVE | yfinance bars, ADX fixed, 400-bar lookback |
| exit_monitor_a.py | ✓ Done | 6 exit conditions |
| hold_monitor_a.py | ✓ Done | 5 layers + ADX slope penalty |
| engine_b.py | ✓ LIVE | yfinance bars, limit_price fix |
| exit_monitor_b.py | ✓ Done | 5 exit conditions |
| hold_monitor_b.py | ✓ Done | 5-layer OU health |
| engine_c.py | ✓ LIVE | yfinance bars |
| exit_monitor_c.py | ✓ Done | 4 exit conditions |
| hold_monitor_c.py | ✓ Done | 4-layer squeeze health |
| engine_e.py | ✓ LIVE | yfinance bars |
| exit_monitor_e.py | ✓ Done | RS exit conditions |
| hold_monitor_e.py | ✓ Done | RS health scoring |
| engine_f.py | ✓ LIVE | yfinance bars, consolidation gate fixed |
| exit_monitor_f.py | ✓ Done | 5 exits + partial trim |
| hold_monitor_f.py | ✓ Done | 5-layer breakout health |
| ares_exit_monitor.py | ✓ Done | A, B, C, E, F wired |
| ares_hold_monitor.py | ✓ Done | A, B, C, E, F wired |
| ares_watchdog.py | ✓ Done | Intraday hard stop + circuit breaker |
| ares_diagnose.py | ✓ Done | System health diagnostic |
| statistical_validation.py | ✓ Done | IC + FDR + Ledoit-Wolf |
| ares_signal_audit.py | ✓ Done | Signal distribution audit |
| ares_threshold_deriver.py | ✓ Done | Derives TODO:DERIVE constants |
| sub_engine_params.json | ✓ Done | Bootstrap equal weights |

### Open positions (as of 2026-06-14)

| Symbol | Engine | Entry | Entry Price | Stop | Hold Target |
|--------|--------|-------|-------------|------|-------------|
| CSCO | E | 2026-06-03 | $125.97 | $114.56 | 20d |
| IONQ | E | 2026-06-03 | $70.05 | $55.38 | 20d |
| ASTS | B | 2026-06-14 | $82.43 | $55.02 | 8d |

### Shadow counts (as of 2026-06-14 end of session)

| Engine | Shadow count | Gate |
|--------|-------------|------|
| A | 6/30 | ⏳ |
| B | 3/30 | ⏳ |
| C | 1/30 | ⏳ |
| E | 27/30 | ⏳ — 3 away |
| F | 0/30 | ⏳ (consolidation gate fixed today) |

### Deferred

| Item | Reason |
|------|--------|
| engine_d.py | Needs intraday runtime (minute bars, 9:30–11:30 loop) |

---

## END OF SESSION CHECKLIST

### Claude side

```bash
# 1. Syntax check all changed files
for f in ares_config.py engine_*.py ares_exit_monitor.py ares_hold_monitor.py \
          exit_monitor_*.py hold_monitor_*.py ledger.py \
          outcome_tracker.py capital_allocator.py daily_recap.py hamilton_filter.py; do
    python3 -c "import ast; ast.parse(open('$f').read()); print('OK: $f')" 2>/dev/null
done

# 2. Verify no signals.py imports
grep -rn "from signals import\|import signals" engine_*.py 2>/dev/null && echo "VIOLATION" || echo "OK"

# 3. Verify yfinance in all fetch_bars
grep -n "yf.download" engine_*.py

# 4. Present all changed files for download
# 5. Update all three ARES markdown files and present for download
```

### Steve's side

```powershell
# Copy all downloaded files into Ares folder, overwriting existing
# Then:
git add -A
git status --short   # CONFIRM no _env, .env, *.log, __pycache__ appear
git commit -m "Session 7: yfinance bar fetch, ADX fix, Engine F consolidation gate, logging pipeline (2026-06-14)"
git push origin main
Remove-Item -Recurse -Force __pycache__ -ErrorAction SilentlyContinue
```

---

## RULES CLAUDE MUST FOLLOW

1. Read all three MD files before any technical work.
2. Run `python ares_diagnose.py` before writing code each session.
3. Verify invariants (Step 4). If any fail, diagnose before proceeding.
4. Real data or skip. Never invent a default. Math based always.
5. Symbol ownership check is the first gate in every engine.
6. Engine F stop is structural — never apply ATR Kelly without converting to risk-per-share.
7. Capital reallocation is advisory. Never shift automatically.
8. IC horizon is immutable once trades accumulate.
9. No signals.py import in any engine file — ever.
10. No FRED data in macro_context.py.
11. Bar data fetch must use yfinance — never Alpaca IEX endpoint.
12. Syntax check before presenting any file for download.
13. Every session that touches code ends with updated markdown files presented for download.
14. Update ARES_MASTER_PLAN.md same session as architecture changes.
15. Shadow mode before live capital — 30 shadow entries per engine minimum.
