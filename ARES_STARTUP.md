# ARES\_STARTUP.md — Session Startup

*Read first. Every session. No exceptions.
Last updated: 2026-05-31 | Version: 1.2*

---

## ⚠️  RF-23 WARNING — RAPTOR FILES IN ARES DIRECTORY

The Ares folder contains Raptor system files (`main.py`, `exit_monitor.py`,
`hold_monitor.py`, `watchdog.py`, `backtest.py`, `signals.py`, `config.py`,
`data_feeds.py`, `agent_layer.py`, `market_agent.py`, etc.).

**NEVER schedule these Raptor files via Task Scheduler for Ares operations.**

Ares schedulable entry points ONLY:
```
engine_a.py --scan        9:35 AM ET
engine_b.py --scan        9:35 AM ET
engine_c.py --scan        9:35 AM ET
engine_e.py --scan        9:35 AM ET
engine_f.py --scan        9:35 AM ET
ares_exit_monitor.py      9:50 AM ET + 3:55 PM ET
ares_hold_monitor.py      9:52 AM ET + 3:58 PM ET
hamilton_filter.py        8:45 AM ET (macro update)
outcome_tracker.py        4:15 PM ET (--all-forward)
daily_recap.py            4:20 PM ET
```

If you accidentally run the Raptor `exit_monitor.py` or `watchdog.py` instead
of `ares_exit_monitor.py` or `ares_watchdog.py`, Raptor will attempt to manage
Ares positions with the wrong exit logic. All Raptor files now have a top-of-file
`⚠️ RAPTOR FILE — NOT ARES` warning.

TODO: When ready, move Raptor files to a `raptor/` subdirectory to eliminate
the naming confusion permanently.

---

## STEP 1 — COPY LATEST FILES INTO ARES FOLDER

Until git is initialized, copy all session output files directly into:
`C:\Users\steve\OneDrive\Desktop\Ares`

Git setup (run once when ready):
```powershell
cd "C:\Users\steve\OneDrive\Desktop\Ares"
git init
git add -A
git commit -m "Initial commit: Sprints 0-3 complete (2026-05-30)"
git branch -M main
git remote add origin https://github.com/stevefirwin-svg/Ares.git
git push -u origin main
```

Once git is live, replace Step 1 with:
```powershell
git -C "C:\Users\steve\OneDrive\Desktop\Ares" pull origin main
git -C "C:\Users\steve\OneDrive\Desktop\Ares" log --oneline -3
Remove-Item -Recurse -Force __pycache__ -ErrorAction SilentlyContinue
```

**Note the top commit hash.** Paste it to Claude at session start.
Claude uses it as `LAST_STEVE_COMMIT` when generating the end-of-session patch.

\---

## STEP 2 — READ FILES IN ORDER

1. **ARES\_STARTUP.md** — startup sequence and system state (this file)
2. **ARES\_MASTER\_PLAN.md** — what is done, what is open, build order
3. **ARES\_SKILL.md** — rules, engine architecture, what must never be violated

\---

## STEP 3 — HEALTH CHECK

Once engines are running, paste output of these before any code work:

```powershell
python engine_b.py --status
python engine_a.py --status
python engine_f.py --status
python ares_exit_monitor.py --status
```

**What to look for:**

`engine_*.py --status`:
- Shadow entries per engine — gate for live capital is 30 per engine
- Open positions (should be empty until shadow gate met)

`ares_exit_monitor.py --status`:
- Any open positions across all engines
- Confirm engine modes match expected (shadow vs live)

\---

## STEP 4 — VERIFY CRITICAL INVARIANTS

```bash
# 1. Symbol ownership lock present in ledger
grep -n "symbol_ownership\|engine_id" ledger.py | head -5

# 2. Engine F uses structural stop (not ATR-based)
grep -n "consol_low\|stop_price\|risk_budget" engine_f.py | head -5

# 3. No FRED data in macro_context.py
grep -n "FRED\|yield_curve\|FEDFUNDS\|T10Y2Y" macro_context.py

# 4. IC horizon immutable — declared in ares_config.py
grep -n "IC_HORIZON\|ic_horizon" ares_config.py | head -5

# 5. Capital reallocation is advisory only
grep -n "advisory\|approve\|manual" capital_allocator.py | head -5

# 6. No signals.py import in any engine file
grep -rn "from signals import\|import signals" engine_*.py

# 7. Atomic writes present
grep -n "os.replace" ledger.py outcome_tracker.py | head -5

# 8. forward_return_Nd anchored at entry — RF-1 fix verified
grep -n "entry_date_str\|entry_price\|fwd_price / entry_price" outcome_tracker.py | head -10
```

If any check fails: diagnose before writing code.

\---

## STEP 5 — CONTEXT CHECK

1. What is current macro regime? Check `macro_context.json`.
2. What symbols are currently owned? Check `symbol_ownership.json`.
3. What engines are live vs shadow? Check `ares_config.py` ENGINE\_STATUS.
4. What is the capital allocation? Check `capital_allocator.py --status`.
5. What shadow counts have accumulated? Check `shadow_classifications.json`.
6. What is today's build target? See ARES\_MASTER\_PLAN.md open items.

\---

## DAILY SCHEDULE (target — not yet fully operational)

|Time ET|Script|What it does|
|-|-|-|
|8:00 AM|hamilton\_filter.py|SPY + VIX + credit + breadth → regime|
|9:30 AM|engine\_b.py --scan|MR scan, shadow|
|9:35 AM|engine\_a.py --scan|Momentum scan, shadow|
|9:35 AM|engine\_f.py --scan|Breakout scan, shadow|
|9:35 AM|engine\_e.py --scan|RS rotation scan, shadow|
|9:35 AM|engine\_c.py --scan|Squeeze scan, shadow|
|9:45 AM|ares\_watchdog.py|Intraday hard stop + SPY circuit breaker (every 15 min)|
|9:52 AM|ares\_hold\_monitor.py|Morning health scores, all engines|
|9:55 AM|ares\_exit\_monitor.py|Engine-aware exits, all engines|
|4:00 PM|ares\_hold\_monitor.py|Afternoon re-score|
|4:15 PM|daily\_recap.py|Per-engine P&L summary|
|After close|outcome\_tracker.py|Tag new closed trades|
|After close|capital\_allocator.py --report|Advisory reallocation check|

**✅ Task Scheduler fully configured (session 6 — 2026-06-03):**

| Task | Time | Status |
|------|------|--------|
| Ares\_Hamilton | 8:00 AM | ✅ Running |
| Ares\_UniverseRebuild | 8:30 AM | ✅ Running |
| EngineB\_Scan | 9:35 AM | ✅ Running |
| EngineA\_Scan | 9:36 AM | ✅ Running |
| EngineC\_Scan | 9:37 AM | ✅ Running |
| EngineE\_Scan | 9:38 AM | ✅ Running |
| EngineF\_Scan | 9:38 AM | ✅ Running |
| HoldMonitor\_AM | 9:52 AM | ✅ Running |
| HoldMonitor\_PM | 4:00 PM | ✅ Running |
| Ares\_DailyRecap | 4:15 PM | ✅ Running |
| Ares\_OutcomeTracker\_AllForward | 4:15 PM | ✅ Running |

All tasks set to "Run only when user is logged on".

Engine D (when built — separate intraday runtime):
| 9:05 AM | engine\_d.py --premarket | Gap detection scan |
| 9:30–11:30 AM | engine\_d.py --monitor | 5-minute intraday loop |

\---

## SYSTEM STATE (2026-06-03 — Session 6 end)

### Built and running

|File|Status|Notes|
|-|-|-|
|ares\_config.py|✅ LIVE|All engines set to `live`; bootstrap Kelly active|
|ledger.py|✓ Done|engine\_id keyed + update\_metadata added|
|hamilton\_filter.py|✓ Done|HMM live, writes macro\_context.json|
|macro\_context.py|✓ Done|FRED stripped|
|margin\_guard.py|✓ Done|Per-engine budget|
|outcome\_tracker.py|✓ Done|RF-1/12/15 fixed; --all-forward scheduled at 4:15 PM|
|capital\_allocator.py|✓ Done|Advisory — activates at 15+ closed trades/engine|
|daily\_recap.py|✅ Scheduled|4:15 PM ET via Task Scheduler (session 6)|
|evt\_calibrator.py|✓ Done|Fallback active — re-run after 30+ closed trades (RF-7)|
|ares\_universe.py|✓ Done|150 symbols, 23h cache|
|engine\_b.py|✅ LIVE|Accumulating closed trades|
|exit\_monitor\_b.py|✓ Done|5 exit conditions|
|hold\_monitor\_b.py|✓ Done|5-layer OU health|
|engine\_a.py|✅ LIVE|Accumulating closed trades|
|exit\_monitor\_a.py|✓ Done|6 exit conditions|
|hold\_monitor\_a.py|✓ Done|5 layers + ADX slope penalty|
|engine\_f.py|✅ LIVE|Accumulating closed trades|
|exit\_monitor\_f.py|✓ Done|5 exits + partial trim|
|hold\_monitor\_f.py|✓ Done|5-layer breakout health|
|engine\_c.py|✅ LIVE|Accumulating closed trades|
|exit\_monitor\_c.py|✓ Done|4 exit conditions|
|hold\_monitor\_c.py|✓ Done|4-layer squeeze health|
|engine\_e.py|✅ LIVE|Accumulating closed trades|
|exit\_monitor\_e.py|✓ Done|RS exit conditions|
|hold\_monitor\_e.py|✓ Done|RS health scoring|
|ares\_exit\_monitor.py|✓ Done|A, B, C, E, F wired|
|ares\_hold\_monitor.py|✓ Done|A, B, C, E, F wired|
|statistical\_validation.py|✓ Done|IC calibration + FDR + Ledoit-Wolf|
|ares\_watchdog.py|✓ Done|Intraday hard stop + circuit breaker|
|ares\_signal\_audit.py|✓ Done|Signal distribution + shadow audit|
|ares\_threshold\_deriver.py|✓ Done|Derives TODO:DERIVE constants|
|sub\_engine\_params.json|✓ Done|Bootstrap equal weights pre-loaded|

### Deferred

|Item|Reason|
|-|-|
|engine\_d.py|Needs intraday runtime (minute bars, 9:30–11:30 loop)|

\---

## END OF SESSION CHECKLIST

### Claude side

```bash
# 1. Syntax check all changed files
for f in ares_config.py engine_*.py ares_exit_monitor.py ares_hold_monitor.py \
          exit_monitor_*.py hold_monitor_*.py ledger.py \
          outcome_tracker.py capital_allocator.py daily_recap.py; do
    [ -f "$f" ] && python3 -c "import ast; ast.parse(open('$f').read()); print('OK: $f')" 2>/dev/null
done

# 2. Verify no signals.py imports in engine files
grep -rn "from signals import\|import signals" engine_*.py 2>/dev/null && echo "VIOLATION" || echo "OK"

# 3. Present all changed files for download
# 4. Update all three ARES markdown files
# 5. Present markdown files for download
```

### Steve's side

```powershell
# Copy all downloaded files into Ares folder
# Overwrite existing files when prompted
# Once git is initialized:
# git add -A
# git commit -m "Session description (2026-MM-DD)"
# git push origin main
Remove-Item -Recurse -Force __pycache__ -ErrorAction SilentlyContinue
```

\---

## RULES CLAUDE MUST FOLLOW

1. Read all three MD files before any technical work.
2. Run health check before writing code (once operational).
3. Verify invariants (Step 4). If any fail, diagnose before proceeding.
4. Real data or skip. Never invent a default. Math based always.
5. Symbol ownership check is the first gate in every engine.
6. Engine F stop is structural — never apply ATR Kelly without converting to risk-per-share.
7. Capital reallocation is advisory. Never shift automatically.
8. IC horizon is immutable once trades accumulate.
9. No signals.py import in any engine file — ever.
10. No FRED data in macro\_context.py.
11. Syntax check before presenting any file for download.
12. Every session that touches code ends with updated markdown files presented for download.
13. Update ARES\_MASTER\_PLAN.md same session as architecture changes.
14. Shadow mode before live capital — 30 shadow entries per engine minimum.
