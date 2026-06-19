# ARES_STARTUP.md — Session Startup

*Read first. Every session. No exceptions.
Last updated: 2026-06-19 | Version: 1.6*

---

## ⚠️  PATH CHANGE — PROJECT MOVED TO C:\Ares

As of 2026-06-19, Ares lives at `C:\Ares\` (not OneDrive).
This eliminates OneDrive sync race conditions on JSON state files.

**One-time migration steps (already done if you're reading this):**
```powershell
# 1. Copy files
robocopy "C:\Users\steve\OneDrive\Desktop\Ares" "C:\Ares" /E /XD __pycache__ .git

# 2. Re-clone git (preserves history, sets correct remote)
cd C:\
git clone https://github.com/stevefirwin-svg/Ares Ares
# Then copy your local-only files (_env, *.json state files, logs\) into C:\Ares\

# 3. Delete old Task Scheduler tasks and re-run setup
Get-ScheduledTask -TaskPath "\Ares\" | Unregister-ScheduledTask -Confirm:$false
powershell -ExecutionPolicy Bypass -File ares_task_scheduler_setup.ps1
```

**Exclude `C:\Ares\` from OneDrive sync** (if OneDrive tries to pick it up):
OneDrive Settings → Account → Choose folders → uncheck Ares if listed.

---

## ⚠️  RF-23 WARNING — RAPTOR FILES IN ARES DIRECTORY

The Ares folder contains Raptor system files (`main.py`, `exit_monitor.py`,
`hold_monitor.py`, `watchdog.py`, `backtest.py`, `signals.py`, `config.py`,
`data_feeds.py`, `agent_layer.py`, `market_agent.py`, etc.).

**NEVER schedule these Raptor files via Task Scheduler for Ares operations.**

Ares schedulable entry points ONLY:
```
hamilton_filter.py                  8:45 AM ET  (macro update)
engine_a.py --scan                  9:35 AM ET
engine_b.py --scan                  9:35 AM ET
engine_c.py --scan                  9:36 AM ET
engine_e.py --scan                  9:37 AM ET
engine_f.py --scan                  9:38 AM ET
ares_exit_monitor.py                9:50 AM ET + 3:55 PM ET
ares_hold_monitor.py                9:52 AM ET + 3:58 PM ET
outcome_tracker.py --all-forward    4:15 PM ET
ares_position_sync.py --void-ghosts 4:05 PM ET  ← NEW session 8
daily_recap.py                      4:20 PM ET
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
cd "C:\Ares"
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
python ares_reconcile.py --alpaca
```

`ares_diagnose.py` prints a full system health report:
- Macro regime and staleness
- Shadow classification counts per engine (gate = 30)
- Open positions and ownership
- Closed trade counts vs calibration gate
- EVT calibration status
- Engine modes (live vs shadow)
- Log file scan for ERRORs

`ares_reconcile.py --alpaca` is the new data integrity check (session 8):
- Ledger vs outcomes cross-check
- ic_valid flag integrity
- Ownership vs ledger consistency
- Alpaca live positions vs ledger (requires market hours for best results)

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

# 6. fetch_bars uses yfinance in engines AND exit monitor
grep -n "yf.download" engine_a.py engine_b.py ares_exit_monitor.py | head -5

# 7. All engines use MARKET orders (not limit) — RF-36 fix
grep -n '"type":' engine_a.py engine_b.py engine_c.py engine_e.py engine_f.py

# 8. forward_return_Nd anchored at entry — RF-1 fix verified
grep -n "post_entry\|entry_dt\|days - 1" outcome_tracker.py | head -5

# 9. TERMINAL_EXIT_PATHS includes trend_exhaustion and rs_lost — RF-38 fix
grep -n "trend_exhaustion\|rs_lost" outcome_tracker.py
```

If any check fails: diagnose before writing code.

---

## STEP 5 — CONTEXT CHECK

1. What is current macro regime? Check `macro_context.json`.
2. What symbols are currently owned? Check `symbol_ownership.json`.
3. What engines are live vs shadow? Check `ares_config.py` ENGINE_STATUS.
4. What shadow counts have accumulated? Check `shadow_classifications.json`.
5. Any open shorts in Alpaca? Run `python ares_position_sync.py` to check.
6. What is today's build target? See ARES_MASTER_PLAN.md open items.

---

## DAILY SCHEDULE — TASK SCHEDULER (13 tasks as of 2026-06-19)

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
| 4:05 PM | Ares_PositionSync_EOD | `ares_position_sync.py --void-ghosts` | `logs/position_sync_YYYYMMDD.log` |
| 4:15 PM | Ares_OutcomeTracker | `outcome_tracker.py --all-forward` | `logs/outcome_tracker_YYYYMMDD.log` |
| 4:20 PM | Ares_DailyRecap | `daily_recap.py` | `logs/daily_recap_YYYYMMDD.log` |

**All tasks:** `pythonw.exe` in `C:\Ares\`, logon type = Interactive.

**Task Scheduler setup for each task:**
- Program: `C:\Users\steve\AppData\Local\Programs\Python\Python313\pythonw.exe`
- Arguments: `engine_a.py --scan` (adjust per task)
- Start in: `C:\Ares`
- General tab → "Run only when user is logged on"
- Conditions tab → uncheck "Stop if computer switches to battery power"

**To verify tasks are configured:**
```powershell
Get-ScheduledTask -TaskPath "\Ares\" | Select TaskName, State
```

---

## RECONCILIATION TOOLS (new session 8)

### Daily data integrity audit
```powershell
python ares_reconcile.py           # 8 checks: ownership, ledger↔outcomes, P&L, ic_valid, forward returns
python ares_reconcile.py --fix     # auto-fix stale ownership + ic_valid errors
python ares_reconcile.py --alpaca  # include live Alpaca position comparison
```

### Alpaca vs ledger sync (use when --alpaca shows divergences)
```powershell
python ares_position_sync.py                # audit only — show divergences + action plan
python ares_position_sync.py --close-shorts # cover any accidental short positions immediately
python ares_position_sync.py --fix          # execute all corrective actions
python ares_position_sync.py --void-ghosts  # EOD: remove ghost ledger entries (runs via Task Scheduler)
```

---

## POST-MORTEM LOG REVIEW

After each trading day, review logs for issues:

```powershell
# Quick error scan across all today's logs
Get-Content logs\engine_a_$(Get-Date -f yyyyMMdd).log | Select-String "ERROR|WARNING"
Get-Content logs\engine_b_$(Get-Date -f yyyyMMdd).log | Select-String "ERROR|WARNING"
Get-Content logs\ares_exits_$(Get-Date -f yyyyMMdd).log | Select-String "ERROR|WARNING"
Get-Content logs\position_sync_$(Get-Date -f yyyyMMdd).log | Select-String "ERROR|WARNING|void"

# Full recap
Get-Content logs\daily_recap_$(Get-Date -f yyyyMMdd).log

# Reconciliation check
python ares_reconcile.py --alpaca

# Or run the diagnostic
python ares_diagnose.py
```

---

## SYSTEM STATE (2026-06-19 — Session 8)

### Built and running (LIVE paper trading)

| File | Status | Notes |
|------|--------|-------|
| ares_config.py | ✓ Done | All Ares constants |
| ledger.py | ✓ Done | engine_id keyed + update_metadata |
| hamilton_filter.py | ✓ Done | HMM live, file logging |
| macro_context.py | ✓ Done | FRED stripped |
| margin_guard.py | ✓ Done | Per-engine budget |
| outcome_tracker.py | ✓ Done | TERMINAL_EXIT_PATHS fixed session 8 |
| capital_allocator.py | ✓ Done | Advisory only |
| daily_recap.py | ✓ Done | Macro cards fixed session 8 |
| evt_calibrator.py | ✓ Done | Fallback active — RF-7 still open |
| ares_universe.py | ✓ Done | 141 symbols, 23h cache |
| engine_a.py | ✓ LIVE | Market orders session 8 |
| exit_monitor_a.py | ✓ Done | 6 exit conditions |
| hold_monitor_a.py | ✓ Done | 5 layers + ADX slope penalty |
| engine_b.py | ✓ LIVE | Market orders session 8 |
| exit_monitor_b.py | ✓ Done | 5 exit conditions |
| hold_monitor_b.py | ✓ Done | 5-layer OU health |
| engine_c.py | ✓ LIVE | Market orders session 8 |
| exit_monitor_c.py | ✓ Done | 4 exit conditions |
| hold_monitor_c.py | ✓ Done | 4-layer squeeze health |
| engine_e.py | ✓ LIVE | Market orders session 8 |
| exit_monitor_e.py | ✓ Done | RS exit conditions |
| hold_monitor_e.py | ✓ Done | RS health scoring |
| engine_f.py | ✓ LIVE | Market orders session 8 |
| exit_monitor_f.py | ✓ Done | 5 exits + partial trim |
| hold_monitor_f.py | ✓ Done | 5-layer breakout health |
| ares_exit_monitor.py | ✓ Done | yfinance bars fixed session 8 |
| ares_hold_monitor.py | ✓ Done | A, B, C, E, F wired |
| ares_watchdog.py | ✓ Done | Intraday hard stop + circuit breaker |
| ares_diagnose.py | ✓ Done | System health diagnostic |
| ares_reconcile.py | ✓ Done | NEW session 8 — daily data integrity |
| ares_position_sync.py | ✓ Done | NEW session 8 — Alpaca vs ledger sync |
| statistical_validation.py | ✓ Done | IC + FDR + Ledoit-Wolf |
| ares_signal_audit.py | ✓ Done | Signal distribution audit |
| ares_threshold_deriver.py | ✓ Done | Derives TODO:DERIVE constants |
| sub_engine_params.json | ✓ Done | Bootstrap equal weights |
| sub_engine_outcomes.json | ✓ Done | ic_valid backfilled session 8 |

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
          outcome_tracker.py capital_allocator.py daily_recap.py hamilton_filter.py \
          ares_reconcile.py ares_position_sync.py; do
    python3 -c "import ast; ast.parse(open('$f').read()); print('OK: $f')" 2>/dev/null
done

# 2. Verify no signals.py imports
grep -rn "from signals import\|import signals" engine_*.py 2>/dev/null && echo "VIOLATION" || echo "OK"

# 3. Verify yfinance in all fetch_bars (engines + exit monitor)
grep -n "yf.download" engine_*.py ares_exit_monitor.py

# 4. Verify MARKET orders in all engines
grep -n '"type":' engine_*.py

# 5. Present all changed files for download
# 6. Update all three ARES markdown files and present for download
```

### Steve's side

```powershell
# Copy all downloaded files into C:\Ares, overwriting existing
# Then:
git add -A
git status --short   # CONFIRM no _env, .env, *.log, __pycache__ appear
git commit -m "Session 8: reconciliation pipeline, market order fix, ic_valid fix, recap macro fix (2026-06-19)"
git push origin main
Remove-Item -Recurse -Force __pycache__ -ErrorAction SilentlyContinue
```

---

## RULES CLAUDE MUST FOLLOW

1. Read all three MD files before any technical work.
2. Run `python ares_diagnose.py` and `python ares_reconcile.py --alpaca` before writing code each session.
3. Verify invariants (Step 4). If any fail, diagnose before proceeding.
4. Real data or skip. Never invent a default. Math based always.
5. Symbol ownership check is the first gate in every engine.
6. Engine F stop is structural — never apply ATR Kelly without converting to risk-per-share.
7. Capital reallocation is advisory. Never shift automatically.
8. IC horizon is immutable once trades accumulate.
9. No signals.py import in any engine file — ever.
10. No FRED data in macro_context.py.
11. Bar data fetch must use yfinance — never Alpaca IEX endpoint. This applies to engines AND ares_exit_monitor.py.
12. All entry orders must use type=market — never DAY LIMIT (RF-36: limit orders caused ghost ledger entries and accidental shorts).
13. TERMINAL_EXIT_PATHS in outcome_tracker.py must include all engine-specific exit labels. If a new exit reason is added to any engine, add it to TERMINAL_EXIT_PATHS in the same session.
14. Run `ares_reconcile.py --alpaca` after any session that touches entry/exit logic.
15. Syntax check before presenting any file for download.
16. Every session that touches code ends with updated markdown files presented for download.
17. Update ARES_MASTER_PLAN.md same session as architecture changes.
18. Shadow mode before live capital — 30 shadow entries per engine minimum.
