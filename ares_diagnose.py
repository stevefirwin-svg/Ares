"""
ares_diagnose.py — Ares System Health & Activity Diagnostic
============================================================
Run from the Ares directory:
  python ares_diagnose.py

Reads local JSON state files — no API calls, no network.
Prints a structured report covering:
  1. Macro regime
  2. Shadow classification counts (trades would fire)
  3. Live positions / ownership
  4. Closed trade count (vs 30-trade calibration gate)
  5. Engine stop distance sanity check
  6. Key flags that suppress activity
  7. Task Scheduler log scan (if accessible)
"""

import json
import os
import sys
import glob
from datetime import datetime, date, timedelta
from collections import defaultdict

# ── File paths (run from Ares directory) ─────────────────────────────────────
FILES = {
    "macro":      "macro_context.json",
    "shadow":     "shadow_classifications.json",
    "ownership":  "symbol_ownership.json",
    "outcomes":   "sub_engine_outcomes.json",
    "evt":        "evt_calibration.json",
    "config":     "ares_config.py",
}

ENGINES = ["A", "B", "C", "E", "F"]
LIVE_GATE = 30   # shadow entries needed before live capital

SEP  = "─" * 65
SEP2 = "═" * 65

def load(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)

def section(title):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)

def ok(msg):   print(f"  ✅  {msg}")
def warn(msg): print(f"  ⚠️   {msg}")
def bad(msg):  print(f"  ❌  {msg}")
def info(msg): print(f"      {msg}")

# ─────────────────────────────────────────────────────────────────────────────
# 1. MACRO REGIME
# ─────────────────────────────────────────────────────────────────────────────
def check_macro():
    section("1 · MACRO REGIME")
    m = load(FILES["macro"])
    if not m:
        bad("macro_context.json not found — macro_context.py has not run")
        return

    regime = m.get("macro_regime", "UNKNOWN")
    score  = m.get("macro_score", 0)
    ts     = m.get("timestamp", "unknown")
    probs  = m.get("regime_probs", {})
    sigs   = m.get("signals", {})

    # Age check
    try:
        ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        age_h = (datetime.now().astimezone() - ts_dt).total_seconds() / 3600
        age_str = f"{age_h:.1f}h ago"
        if age_h > 25:
            warn(f"macro_context.json is STALE ({age_str}) — macro_context.py may not be running")
        else:
            ok(f"Macro file age: {age_str}")
    except Exception:
        warn(f"Cannot parse timestamp: {ts}")
        age_h = 0

    color = {"RISK_ON": "✅", "NEUTRAL": "⚠️ ", "RISK_OFF": "❌", "CRISIS": "❌"}.get(regime, "❓")
    print(f"\n  {color}  Regime: {regime}  (score={score:+.3f})")

    if probs:
        for k, v in probs.items():
            bar = "█" * int(v * 20)
            print(f"        {k:<10s} {v:.3f}  {bar}")

    print(f"\n      VIX:     {sigs.get('vix_level', 'n/a')}")
    print(f"      SPY 20d: {sigs.get('spy_ret_20d_pct', 'n/a')}%")
    print(f"      Credit:  {sigs.get('credit_norm', 'n/a')}")
    print(f"      Breadth: {sigs.get('breadth_z', 'n/a')}")

    # Regime impact per engine
    GATES = {
        "A": {"RISK_ON": 1.0, "NEUTRAL": 0.75, "RISK_OFF": 0.0, "CRISIS": 0.0},
        "B": {"RISK_ON": 0.5, "NEUTRAL": 1.0,  "RISK_OFF": 1.0, "CRISIS": 0.0},
        "C": {"RISK_ON": 1.0, "NEUTRAL": 0.75, "RISK_OFF": 0.0, "CRISIS": 0.0},
        "E": {"RISK_ON": 1.0, "NEUTRAL": 0.75, "RISK_OFF": 0.0, "CRISIS": 0.0},
        "F": {"RISK_ON": 1.0, "NEUTRAL": 0.75, "RISK_OFF": 0.0, "CRISIS": 0.0},
    }
    print(f"\n  Macro multipliers at current regime ({regime}):")
    for eng, gates in GATES.items():
        mult = gates.get(regime, 0.0)
        sym  = "✅" if mult >= 1.0 else ("⚠️ " if mult > 0 else "🚫")
        print(f"    Engine {eng}: {mult:.2f}  {sym}")

# ─────────────────────────────────────────────────────────────────────────────
# 2. SHADOW CLASSIFICATION COUNTS
# ─────────────────────────────────────────────────────────────────────────────
def check_shadow():
    section("2 · SHADOW CLASSIFICATION COUNTS  (gate = 30 per engine)")
    s = load(FILES["shadow"])
    if not s:
        bad("shadow_classifications.json not found")
        return

    clsf = s.get("classifications", [])
    if not clsf:
        bad("Zero shadow classifications — engines have never fired")
        return

    # Count by engine
    by_engine   = defaultdict(int)
    would_trade = defaultdict(int)
    skip_counts = defaultdict(lambda: defaultdict(int))
    dates_seen  = defaultdict(set)

    for c in clsf:
        eng = c.get("engine_id", "?")
        by_engine[eng] += 1
        dates_seen[eng].add(c.get("date", ""))
        if c.get("would_have_traded"):
            would_trade[eng] += 1
        else:
            reason = c.get("skip_reason") or "unknown"
            skip_counts[eng][reason] += 1

    # Today / recent
    today = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    recent = [c for c in clsf if c.get("date","") >= week_ago]
    recent_by_eng = defaultdict(int)
    for c in recent:
        recent_by_eng[c.get("engine_id","?")] += 1

    print(f"\n  Total classifications: {len(clsf)}")
    print(f"  Last 7 days:          {len(recent)}")
    print()
    print(f"  {'Eng':<5} {'Total':>6} {'WouldTrade':>11} {'Last7d':>7} {'ActiveDays':>11}  {'Status'}")
    print(f"  {'───':<5} {'─────':>6} {'──────────':>11} {'──────':>7} {'──────────':>11}  {'──────'}")
    for eng in ENGINES:
        total  = by_engine[eng]
        wt     = would_trade[eng]
        l7     = recent_by_eng[eng]
        days   = len(dates_seen[eng])
        pct    = f"{wt/total*100:.0f}%" if total else "n/a"
        gate   = "✅ GATE MET" if total >= LIVE_GATE else f"⏳ {total}/{LIVE_GATE}"
        print(f"  {eng:<5} {total:>6} {wt:>9} ({pct}) {l7:>7}   {days:>10}  {gate}")

    # Skip reason breakdown
    all_skips = defaultdict(int)
    for eng_skips in skip_counts.values():
        for r, n in eng_skips.items():
            all_skips[r] += n
    if all_skips:
        print(f"\n  Skip reason breakdown (all engines):")
        for reason, count in sorted(all_skips.items(), key=lambda x: -x[1]):
            print(f"    {count:>4}×  {reason}")

    # Most recent entry per engine
    print(f"\n  Most recent classification per engine:")
    last = {}
    for c in clsf:
        eng = c.get("engine_id","?")
        if eng not in last or c.get("date","") > last[eng].get("date",""):
            last[eng] = c
    for eng in ENGINES:
        if eng in last:
            c = last[eng]
            print(f"    {eng}: {c['date']}  {c['symbol']:<6}  score={c.get('engine_score',0):.3f}  "
                  f"would_trade={c.get('would_have_traded')}")
        else:
            print(f"    {eng}: no classifications")

# ─────────────────────────────────────────────────────────────────────────────
# 3. LIVE POSITIONS / OWNERSHIP
# ─────────────────────────────────────────────────────────────────────────────
def check_positions():
    section("3 · LIVE POSITIONS (symbol_ownership.json)")
    o = load(FILES["ownership"])
    if o is None:
        bad("symbol_ownership.json not found")
        return

    if not o:
        info("No open positions (symbol_ownership is empty)")
        info("→ This means any qualifying signal should be able to enter")
        return

    print(f"\n  Open positions: {len(o)}")
    for sym, eng in o.items():
        print(f"    {sym:<8}  owned by Engine {eng}")

# ─────────────────────────────────────────────────────────────────────────────
# 4. CLOSED TRADE COUNT
# ─────────────────────────────────────────────────────────────────────────────
def check_outcomes():
    section("4 · CLOSED TRADES (gate for Sprint 5 calibration = 30 per engine)")
    out = load(FILES["outcomes"])
    if not out:
        bad("sub_engine_outcomes.json not found")
        return

    trades = out.get("trades", [])
    if not trades:
        warn("Zero closed trades recorded — system has not completed any round-trips")
        info("Sprint 5 (IC calibration, Kelly unlock) requires 30 per engine")
        return

    by_engine = defaultdict(int)
    for t in trades:
        by_engine[t.get("engine_id","?")] += 1

    print(f"\n  Total closed trades: {len(trades)}")
    for eng in ENGINES:
        n   = by_engine[eng]
        bar = "█" * n
        pct = f"{n}/{LIVE_GATE}"
        sym = "✅" if n >= LIVE_GATE else ("⚠️ " if n >= 15 else "⏳")
        print(f"  Engine {eng}: {n:>3}  {sym}  {pct}")

# ─────────────────────────────────────────────────────────────────────────────
# 5. EVT CALIBRATION STATUS
# ─────────────────────────────────────────────────────────────────────────────
def check_evt():
    section("5 · EVT CALIBRATION (RF-7)")
    evt = load(FILES["evt"])
    if not evt:
        bad("evt_calibration.json not found")
        return

    fallback_count = 0
    for eng in ENGINES:
        data = evt.get(f"engine_{eng.lower()}", evt.get(eng, {}))
        method = data.get("method", "unknown")
        n_obs  = data.get("n_observations", data.get("n_obs", 0))
        mult   = data.get("stop_mult", data.get("stop_multiplier", "?"))
        is_fb  = "fallback" in str(method)
        if is_fb:
            fallback_count += 1
        sym = "❌" if is_fb else "✅"
        print(f"  Engine {eng}: {sym}  method={method:<30}  n_obs={n_obs}  stop_mult={mult}")

    if fallback_count == len(ENGINES):
        warn("ALL engines on fallback stops — evt_calibrator.py has never run with live data")
        info("Fix: run `python evt_calibrator.py` from the Ares directory")
    elif fallback_count > 0:
        warn(f"{fallback_count} engine(s) still on fallback")

# ─────────────────────────────────────────────────────────────────────────────
# 6. ENGINE E STOP DISTANCE CHECK
# ─────────────────────────────────────────────────────────────────────────────
def check_stop_distances():
    section("6 · ENGINE E STOP DISTANCE CHECK (shadow data)")
    s = load(FILES["shadow"])
    if not s:
        return

    e_clsf = [c for c in s.get("classifications", []) if c.get("engine_id") == "E"]
    if not e_clsf:
        info("No Engine E classifications to check")
        return

    print(f"\n  {'Symbol':<7} {'Price':>8} {'Stop':>8} {'ATR':>8} {'Stop%':>7} {'ATRmult':>9}")
    print(f"  {'──────':<7} {'─────':>8} {'────':>8} {'───':>8} {'─────':>7} {'───────':>9}")
    flagged = 0
    for c in e_clsf[-10:]:
        sigs  = c.get("signals", {})
        price = sigs.get("price", c.get("entry_price", 0))
        stop  = sigs.get("e_stop_price", 0)
        atr   = sigs.get("atr", 0)
        if price and stop and atr:
            stop_pct  = (price - stop) / price * 100
            atr_mult  = (price - stop) / atr if atr else 0
            flag = " ⚠️  wide" if stop_pct > 25 else ""
            if stop_pct > 25:
                flagged += 1
            print(f"  {c['symbol']:<7} {price:>8.2f} {stop:>8.2f} {atr:>8.4f} {stop_pct:>6.1f}% {atr_mult:>8.2f}×{flag}")

    if flagged:
        warn(f"{flagged} Engine E stops are >25% below price — stops are very wide (3.0× ATR)")
        info("This is by design per engine_e.py but means a larger loss to the stop")

# ─────────────────────────────────────────────────────────────────────────────
# 7. KEY SUPPRESSION FLAGS
# ─────────────────────────────────────────────────────────────────────────────
def check_flags():
    section("7 · KEY ACTIVITY SUPPRESSION FLAGS")

    items = []

    # Shadow vs live mode — read ares_config.py directly (crude grep)
    cfg_path = FILES["config"]
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            cfg_text = f.read()
        # Find ENGINE_STATUS lines
        in_block = False
        statuses = {}
        for line in cfg_text.splitlines():
            if "ENGINE_STATUS" in line and "=" in line:
                in_block = True
            if in_block:
                for eng in ENGINES:
                    if f'"{eng}"' in line or f"'{eng}'" in line:
                        if '"live"' in line or "'live'" in line:
                            statuses[eng] = "live"
                        elif '"shadow"' in line or "'shadow'" in line:
                            statuses[eng] = "shadow"
                        elif '"off"' in line or "'off'" in line:
                            statuses[eng] = "off"
                if "}" in line and in_block:
                    in_block = False

        print(f"\n  Engine modes (from ares_config.py):")
        for eng in ENGINES:
            mode = statuses.get(eng, "unknown")
            sym  = "✅" if mode == "live" else ("⚠️ " if mode == "shadow" else "🚫")
            print(f"    Engine {eng}: {sym}  {mode}")

        # Bootstrap fraction — parse actual values rather than hardcoding,
        # so this stays correct after ares_config.py changes (e.g. post-calibration
        # when KELLY_SHADOW_MULTIPLIER advances toward 1.0 per its own comment).
        frac = None
        mult = None
        for line in cfg_text.splitlines():
            if "KELLY_BOOTSTRAP_FRACTION" in line and "=" in line and "#" not in line.split("=")[0]:
                print(f"\n  {line.strip()}")
                try:
                    frac = float(line.split("=")[1].split("#")[0].strip())
                except Exception:
                    pass
            if "KELLY_SHADOW_MULTIPLIER" in line and "=" in line and "#" not in line.split("=")[0]:
                print(f"  {line.strip()}")
                try:
                    mult = float(line.split("=")[1].split("#")[0].strip())
                except Exception:
                    pass

        if frac is not None and mult is not None:
            effective_pct = frac * mult * 100
            print(f"\n  Effective position size: {frac} × {mult} = {effective_pct:.2f}% of equity")
            print(f"  At $50K: ${50000 * frac * mult:,.0f} per position")
        else:
            warn("Could not parse KELLY_BOOTSTRAP_FRACTION/KELLY_SHADOW_MULTIPLIER from ares_config.py")

    # Leveraged/inverse ETF issue (RF-20)
    universe_path = "universe.json"
    if os.path.exists(universe_path):
        with open(universe_path) as f:
            try:
                uni = json.load(f)
                symbols = uni if isinstance(uni, list) else uni.get("symbols", [])
                if isinstance(symbols[0], dict):
                    symbols = [s.get("symbol","") for s in symbols]
                BAD_ETF = {"TQQQ","SQQQ","SOXL","SOXS","TZA","SPDN","TSLL","NVD","PLTD","BITO","IBIT","UVXY","SVXY"}
                found_bad = [s for s in symbols if s in BAD_ETF]
                if found_bad:
                    warn(f"RF-20: Leveraged/inverse ETFs in universe: {found_bad}")
                    info("These violate OU/momentum assumptions — should be in HARD_EXCLUSIONS")
                else:
                    ok("No known leveraged/inverse ETFs found in universe")
                print(f"\n  Universe size: {len(symbols)} symbols")
            except Exception as e:
                info(f"Could not parse universe.json: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# 8. TASK SCHEDULER LOG SCAN
# ─────────────────────────────────────────────────────────────────────────────
def check_scheduler():
    section("8 · TASK SCHEDULER LOGS")

    log_dirs = [
        r"C:\Windows\System32\winevt\Logs",
        "C:\\Ares\\logs",
        "C:\\Ares",
        "logs",
    ]

    log_files = []
    for d in log_dirs:
        if os.path.isdir(d):
            for pattern in ["*.log", "engine_*.log", "ares_*.log"]:
                log_files.extend(glob.glob(os.path.join(d, pattern)))

    if not log_files:
        warn("No log files found — check that engines are writing logs")
        info("Expected locations: Ares/logs/*.log")
        info("If Task Scheduler tasks are failing silently, add stdout redirect:")
        info("  In task action: pythonw.exe engine_a.py >> C:\\Ares\\logs\\engine_a.log 2>&1")
        return

    # Show recent log tails
    log_files.sort(key=os.path.getmtime, reverse=True)
    print(f"\n  Found {len(log_files)} log file(s):")
    for lf in log_files[:8]:
        mtime = datetime.fromtimestamp(os.path.getmtime(lf)).strftime("%Y-%m-%d %H:%M")
        size  = os.path.getsize(lf)
        print(f"    {os.path.basename(lf):<35}  {mtime}  {size:>8,} bytes")

    # Check for errors in recent logs
    print(f"\n  Scanning for ERRORs/Tracebacks in recent logs:")
    errors_found = False
    for lf in log_files[:5]:
        try:
            with open(lf, errors="replace") as f:
                lines = f.readlines()
            error_lines = [l.rstrip() for l in lines if
                           any(kw in l for kw in ["ERROR", "Traceback", "Exception", "CRITICAL", "failed"])]
            if error_lines:
                errors_found = True
                print(f"\n  [{os.path.basename(lf)}]")
                for l in error_lines[-5:]:
                    print(f"    {l}")
        except Exception as e:
            info(f"Could not read {lf}: {e}")

    if not errors_found:
        ok("No ERRORs found in recent log files")

# ─────────────────────────────────────────────────────────────────────────────
# 9. SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
def print_summary(shadow_data, ownership_data, outcomes_data):
    section("SUMMARY & RECOMMENDED NEXT STEPS")

    clsf    = shadow_data.get("classifications", []) if shadow_data else []
    trades  = outcomes_data.get("trades", []) if outcomes_data else []
    owned   = ownership_data or {}

    by_engine = defaultdict(int)
    for c in clsf:
        by_engine[c.get("engine_id","?")] += 1

    total_shadow = sum(by_engine[e] for e in ENGINES)
    total_trades = len(trades)
    open_pos     = len(owned)

    print(f"""
  ACTIVITY SNAPSHOT
  ─────────────────
  Shadow classifications:  {total_shadow}  (across {len(ENGINES)} engines)
  Closed trades:           {total_trades}  (need 30/engine for calibration)
  Open positions:          {open_pos}
""")

    steps = []

    if total_shadow == 0:
        steps.append("CRITICAL: Zero shadow classifications — engines are not running or scanning no symbols")
        steps.append("→ Check Task Scheduler: are engine_a.py through engine_f.py running at 9:35 AM?")
        steps.append("→ Run `python engine_b.py` manually and check for errors")

    elif total_shadow < 10:
        steps.append("Very few classifications — signal thresholds may be too tight OR engines are crashing")
        steps.append("→ Run `python engine_b.py --dry-run` and watch the console output")

    if not any(by_engine.get(e,0) > 0 for e in ["A","B","C"]):
        steps.append("Engines A, B, C have ZERO classifications — likely not running (task scheduler issue)")

    if total_trades == 0 and total_shadow > 0:
        steps.append("Shadows exist but ZERO trades executed — engines may still be in shadow mode OR Alpaca order submission failing")
        steps.append("→ Confirm ENGINE_STATUS in ares_config.py shows 'live' for all engines")

    if open_pos == 0 and total_shadow > 5:
        steps.append("No open positions despite shadows — live entry code may have an order submission bug")
        steps.append("→ Check Alpaca paper account dashboard for any rejected/failed orders")

    evt = load(FILES["evt"])
    if evt:
        all_fallback = all(
            "fallback" in str(evt.get(f"engine_{e.lower()}", evt.get(e, {})).get("method","fallback"))
            for e in ENGINES
        )
        if all_fallback:
            steps.append("RF-7: Run `python evt_calibrator.py` — all engines on fallback stops")

    if not steps:
        steps.append("System looks structurally healthy — activity is limited by signal selectivity (by design)")
        steps.append("→ Keep running; accumulate shadow + live trade data toward 30-trade calibration gate")

    for i, s in enumerate(steps, 1):
        print(f"  {i}. {s}")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n{SEP2}")
    print(f"  ARES SYSTEM DIAGNOSTIC  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(SEP2)

    check_macro()
    check_shadow()

    shadow_data   = load(FILES["shadow"])
    ownership_data = load(FILES["ownership"])
    outcomes_data  = load(FILES["outcomes"])

    check_positions()
    check_outcomes()
    check_evt()
    check_stop_distances()
    check_flags()
    check_scheduler()
    print_summary(shadow_data, ownership_data, outcomes_data)

    print(f"\n{SEP2}\n")
