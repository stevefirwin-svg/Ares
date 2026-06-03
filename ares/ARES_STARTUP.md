# ARES_STARTUP.md — Session Startup
*Read first. Every session. No exceptions.*
*Last updated: 2026-05-29 | Version: 1.0*

---

## STEP 1 — PULL FRESH FROM GITHUB

```bash
git clone https://github.com/stevefirwin-svg/Ares /home/claude/ares
cd /home/claude/ares
git log --oneline -5
```

Steve's machine before any session:
```powershell
git -C "C:\Users\steve\OneDrive\Desktop\Ares" pull origin main
git -C "C:\Users\steve\OneDrive\Desktop\Ares" log --oneline -3
Remove-Item -Recurse -Force __pycache__ -ErrorAction SilentlyContinue
```

**Note the top commit hash.** Paste it to Claude at session start.
Claude uses it as `LAST_STEVE_COMMIT` when generating the end-of-session patch.

---

## STEP 2 — READ FILES IN ORDER

1. **ARES_STARTUP.md** — startup sequence and system state (this file)
2. **ARES_MASTER_PLAN.md** — what is done, what is open, build order
3. **ARES_SKILL.md** — rules, engine architecture, what must never be violated

---

## STEP 3 — HEALTH CHECK

Once engines are running, paste output of these before any code work:

```powershell
python outcome_tracker.py --summary
python capital_allocator.py --status
python shadow_classifier.py --summary
```

**What to look for (once operational):**

`outcome_tracker.py --summary`:
- Closed trades per engine — gate for IC calibration is 30 per engine
- IC-valid count per engine (terminal exits only, not partial trims)
- entry_decision populated — confirm P0 sidecar is working

`capital_allocator.py --status`:
- Current allocation per engine ($ and %)
- Advisory recommendation (if any) — requires 15 closed trades per engine
- Last approved reallocation date

`shadow_classifier.py --summary`:
- Shadow entries per engine — gate for live capital is 30 per engine
- Classification frequency per engine (target 2–8% of universe per scan)
- Signal distribution check: std > 0, NaN < 20% per signal

---

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
```

If any check fails: diagnose before writing code.

---

## STEP 5 — CONTEXT CHECK

1. What is current macro regime? Check `macro_context.json`.
2. What symbols are currently owned? Check `symbol_ownership.json`.
3. What engines are live vs shadow? Check `ares_config.py` ENGINE_STATUS.
4. What is the capital allocation? Check `capital_allocator.py --status`.
5. What is today's build target? See ARES_MASTER_PLAN.md open items.

---

## DAILY SCHEDULE (target — not yet operational)

| Time ET | Script | What it does |
|---------|--------|--------------|
| 8:00 AM | macro_context.py | SPY + VIX + credit + breadth → regime |
| 9:30 AM | engine_b.py --scan | MR scan, shadow or live |
| 9:35 AM | engine_a.py --scan | Momentum scan |
| 9:35 AM | engine_e.py --scan | RS rotation scan |
| 9:35 AM | engine_f.py --scan | Breakout scan |
| 9:35 AM | engine_c.py --scan | Squeeze scan |
| 9:52 AM | hold_monitor.py | Morning health scores, all engines |
| 9:55 AM | exit_monitor.py | Engine-aware exits, all engines |
| 4:00 PM | hold_monitor.py | Afternoon re-score |
| 4:15 PM | daily_recap.py | Per-engine P&L summary |
| After close | outcome_tracker.py | Tag new closed trades |
| After close | capital_allocator.py --report | Advisory reallocation check |
| End of day | git push | Via patch handoff |

Engine D (when built — separate intraday runtime):
| 9:05 AM | engine_d.py --premarket | Gap detection scan |
| 9:30–11:30 AM | engine_d.py --monitor | 5-minute intraday loop |

---

## SYSTEM STATE (2026-05-29)

### Infrastructure (inherited from Raptor, needs Ares-specific modification)

| File | Status | Sprint |
|------|--------|--------|
| data_feeds.py | Available — needs sector ETF bars for Engine E | S4 |
| universe_builder.py | Available — unchanged | Ready |
| macro_context.py | Needs FRED strip | S0-e |
| ledger.py | Needs engine_id keying + Ares account | S0-d |
| margin_guard.py | Needs per-engine budget | S0-f |
| kelly_engine.py | Available — needs per-engine wiring | S2+ |
| outcome_tracker.py | Needs engine_id field | S1-a |
| daily_recap.py | Needs per-engine tables | S1-c |
| exit_monitor.py | Needs engine branches (currently Raptor logic) | S2+ |
| hold_monitor.py | Needs engine branches (currently Raptor logic) | S2+ |

### Not started

| File | Sprint |
|------|--------|
| ares_config.py | S0-a |
| symbol_ownership.json | S0-b |
| sub_engine_outcomes.json | S0-g |
| shadow_classifications.json | S0-h |
| capital_allocator.py | S1-b |
| engine_a.py through engine_f.py | S2–S4 |

### Deferred

| Item | Reason |
|------|--------|
| engine_d.py | Needs intraday runtime (minute bars, 9:30–11:30 loop) |

---

## KEY FILES (target state — post Sprint 0)

```
ARES_STARTUP.md           This file — read first
ARES_MASTER_PLAN.md       Priority queue + verified status
ARES_SKILL.md             Rules + engine architecture + what to never do
ares_config.py            All parameters — Ares Alpaca keys, IC horizons, caps
engine_a.py               Momentum Continuation — independent scanner
engine_b.py               Mean Reversion — independent scanner (built first)
engine_c.py               Squeeze Break — independent scanner
engine_e.py               RS Rotation — independent scanner
engine_f.py               Structural Breakout — independent scanner
exit_monitor.py           Engine-branched exit logic
hold_monitor.py           Engine-branched health scoring
outcome_tracker.py        Trade labeling — engine_id mandatory
capital_allocator.py      Advisory weight rebalancer
ledger.py                 Entry/exit records — engine_id keyed
symbol_ownership.json     Cross-engine symbol lock
sub_engine_outcomes.json  Per-engine trade history
shadow_classifications.json Would-be entries in shadow mode
macro_context.json        Shared regime label (read-only for engines)
```

---

## END OF SESSION CHECKLIST

### Claude side

```bash
# 1. Syntax check all changed files
for f in ares_config.py engine_*.py exit_monitor.py hold_monitor.py ledger.py \
          outcome_tracker.py capital_allocator.py daily_recap.py; do
    [ -f "$f" ] && python3 -c "import ast; ast.parse(open('$f').read()); print('OK: $f')" 2>/dev/null
done

# 2. Verify no signals.py imports in engine files
grep -rn "from signals import\|import signals" engine_*.py 2>/dev/null && echo "VIOLATION" || echo "OK"

# 3. Commit locally
git add -A
git commit -m "Description: what changed and why (2026-MM-DD)"
git log --oneline -3

# 4. Generate patch
git diff LAST_STEVE_COMMIT HEAD > /tmp/ares_session.patch
cp /tmp/ares_session.patch /mnt/user-data/outputs/ares_session.patch
```

### Steve's side

```powershell
cd "C:\Users\steve\OneDrive\Desktop\Ares"
git apply ares_session.patch
git diff --stat
git add -A
git commit -m "same message as Claude commit"
git push origin main
git log --oneline -4
Remove-Item ares_session.patch
Remove-Item -Recurse -Force __pycache__ -ErrorAction SilentlyContinue
```

---

## RULES CLAUDE MUST FOLLOW

1. Clone fresh from GitHub first. Never work from project knowledge alone.
2. Read all three MD files before any technical work.
3. Run health check before writing code (once operational).
4. Verify invariants (Step 4). If any fail, diagnose before proceeding.
5. Real data or skip. Never invent a default.
6. Symbol ownership check is the first gate in every engine.
7. Engine F stop is structural — never apply ATR Kelly without converting to risk-per-share.
8. Capital reallocation is advisory. Never shift automatically.
9. IC horizon is immutable once trades accumulate.
10. No signals.py import in any engine file — ever.
11. No FRED data in macro_context.py.
12. Syntax check before committing.
13. Every session that touches code ends with a patch file. No exceptions.
14. Update ARES_MASTER_PLAN.md same session as architecture changes.
15. Shadow mode before live capital — 30 shadow entries per engine minimum.
