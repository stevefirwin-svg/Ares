"""
ares_health_check.py — Ares Pre-Market Health Check
=====================================================
Run before market open (or any session) to validate system state.
Exits with code 1 if any CRITICAL check fails so Task Scheduler can alert.

Usage:
    python ares_health_check.py            # full check, print report
    python ares_health_check.py --json     # machine-readable output
    python ares_health_check.py --critical # only print failures

Schedule:
    7:55 AM ET via Task Scheduler — before all engine scans
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

# ares_config triggers load_dotenv — do this first
from ares_config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    ENGINE_STATUS, INITIAL_ENGINE_WEIGHTS,
)

ARES_DIR = os.path.dirname(os.path.abspath(__file__))

SEV_CRITICAL = "CRITICAL"
SEV_WARN     = "WARN"
SEV_OK       = "OK"
SEV_INFO     = "INFO"

results = []


def check(name: str, sev: str, passed: bool, detail: str):
    results.append({
        "name":   name,
        "sev":    sev if not passed else SEV_OK,
        "passed": passed,
        "detail": detail,
    })


# ── 1. Alpaca auth ────────────────────────────────────────────────────────────

def check_alpaca_auth():
    import requests
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    try:
        r = requests.get(f"{ALPACA_BASE_URL}/v2/account", headers=headers, timeout=10)
        if r.status_code == 200:
            acct   = r.json()
            equity = float(acct.get("equity", 0))
            bp     = float(acct.get("buying_power", 0))
            check("alpaca_auth", SEV_CRITICAL, True,
                  f"equity=${equity:,.0f}  buying_power=${bp:,.0f}")
        else:
            check("alpaca_auth", SEV_CRITICAL, False,
                  f"HTTP {r.status_code} — {r.text[:120]}")
    except Exception as e:
        check("alpaca_auth", SEV_CRITICAL, False, str(e))


# ── 2. Alpaca live positions ──────────────────────────────────────────────────

def check_alpaca_positions():
    import requests
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    try:
        r = requests.get(f"{ALPACA_BASE_URL}/v2/positions", headers=headers, timeout=10)
        if r.status_code == 200:
            pos = r.json()
            syms = [p.get("symbol") for p in pos]
            check("alpaca_positions", SEV_INFO, True,
                  f"{len(pos)} open: {', '.join(syms) if syms else 'none'}")
        else:
            check("alpaca_positions", SEV_WARN, False,
                  f"HTTP {r.status_code}")
    except Exception as e:
        check("alpaca_positions", SEV_WARN, False, str(e))


# ── 3. Critical file freshness ────────────────────────────────────────────────

def check_file_age(fname: str, max_hours: float, sev: str):
    path = os.path.join(ARES_DIR, fname)
    if not os.path.exists(path):
        check(f"file:{fname}", sev, False, "FILE MISSING")
        return
    age_hours = (datetime.now().timestamp() - os.path.getmtime(path)) / 3600
    passed    = age_hours <= max_hours
    check(f"file:{fname}", sev, passed,
          f"age={age_hours:.1f}h (max={max_hours:.0f}h)")


def check_file_freshness():
    # macro_context.json must be < 25h (refreshed daily at 8:00 AM)
    check_file_age("macro_context.json",       25.0, SEV_CRITICAL)
    # universe.json has 23h cache — warn if older than 26h
    check_file_age("universe.json",            26.0, SEV_WARN)
    # evt_calibration.json — warn if not built yet (but doesn't block)
    check_file_age("evt_calibration.json",     999.0, SEV_WARN)
    # sub_engine_params.json — must exist
    check_file_age("sub_engine_params.json",   999.0, SEV_WARN)


# ── 4. Macro regime ───────────────────────────────────────────────────────────

def check_macro_regime():
    path = os.path.join(ARES_DIR, "macro_context.json")
    if not os.path.exists(path):
        check("macro_regime", SEV_CRITICAL, False, "macro_context.json missing")
        return
    try:
        with open(path) as f:
            m = json.load(f)
        regime = m.get("macro_regime", "UNKNOWN")
        score  = m.get("macro_score", 0)
        ts     = m.get("timestamp", "unknown")[:16]
        # All good — just report state
        check("macro_regime", SEV_INFO, True,
              f"{regime}  score={score:+.3f}  as_of={ts}")
    except Exception as e:
        check("macro_regime", SEV_WARN, False, str(e))


# ── 5. Engine status ──────────────────────────────────────────────────────────

def check_engine_status():
    active = [e for e, s in ENGINE_STATUS.items() if s == "live"]
    shadow = [e for e, s in ENGINE_STATUS.items() if s == "shadow"]
    off    = [e for e, s in ENGINE_STATUS.items() if s == "off"]
    check("engine_status", SEV_INFO, True,
          f"live={active}  shadow={shadow}  off={off}")


# ── 6. Shadow pipeline health ─────────────────────────────────────────────────

def check_shadow_pipeline():
    path = os.path.join(ARES_DIR, "shadow_classifications.json")
    if not os.path.exists(path):
        check("shadow_pipeline", SEV_WARN, False, "shadow_classifications.json missing")
        return
    try:
        with open(path) as f:
            s = json.load(f)
        classes = s.get("classifications", [])
        from collections import Counter
        by_engine = Counter(c.get("engine_id") for c in classes)
        dates     = sorted(c.get("date","") for c in classes if c.get("date"))
        last_date = dates[-1] if dates else "none"
        check("shadow_pipeline", SEV_INFO, True,
              f"total={len(classes)}  by_engine={dict(by_engine)}  last={last_date}")
    except Exception as e:
        check("shadow_pipeline", SEV_WARN, False, str(e))


# ── 7. Outcomes pipeline ──────────────────────────────────────────────────────

def check_outcomes():
    path = os.path.join(ARES_DIR, "sub_engine_outcomes.json")
    if not os.path.exists(path):
        check("outcomes", SEV_WARN, False, "sub_engine_outcomes.json missing")
        return
    try:
        with open(path) as f:
            o = json.load(f)
        trades = o.get("trades", [])
        from collections import Counter
        by_engine = Counter(t.get("engine_id") for t in trades)
        ic_ready  = {e: sum(1 for t in trades
                            if t.get("engine_id") == e and t.get("ic_valid"))
                     for e in ["A","B","C","E","F"]}
        gate      = 30
        check("outcomes", SEV_INFO, True,
              f"closed={len(trades)}  ic_valid={ic_ready}  gate={gate}")
    except Exception as e:
        check("outcomes", SEV_WARN, False, str(e))


# ── 8. Ledger integrity ───────────────────────────────────────────────────────

def check_ledger():
    path = os.path.join(ARES_DIR, "ares_position_ledger.json")
    if not os.path.exists(path):
        check("ledger", SEV_WARN, False,
              "ares_position_ledger.json missing — expected if no trades yet")
        return
    try:
        with open(path) as f:
            l = json.load(f)
        open_pos = list(l.get("positions", {}).keys())
        closed   = len(l.get("closed", []))

        # Check ownership file is consistent
        own_path = os.path.join(ARES_DIR, "symbol_ownership.json")
        owned_syms = set()
        if os.path.exists(own_path):
            with open(own_path) as f:
                owned_syms = set(json.load(f).keys())

        ledger_syms = {v.get("symbol") for v in l.get("positions", {}).values()}
        orphan_owned = owned_syms - ledger_syms
        orphan_ledger = ledger_syms - owned_syms

        issues = []
        if orphan_owned:
            issues.append(f"owned_not_in_ledger={orphan_owned}")
        if orphan_ledger:
            issues.append(f"ledger_not_in_owned={orphan_ledger}")

        passed = not issues
        detail = f"open={len(open_pos)}  closed={closed}"
        if issues:
            detail += "  MISMATCH: " + "; ".join(issues)
        check("ledger_integrity", SEV_WARN if issues else SEV_INFO, passed, detail)

    except Exception as e:
        check("ledger_integrity", SEV_WARN, False, str(e))


# ── 9. API key not empty ──────────────────────────────────────────────────────

def check_keys_not_empty():
    key_ok = bool(ALPACA_API_KEY and len(ALPACA_API_KEY) > 10)
    sec_ok = bool(ALPACA_SECRET_KEY and len(ALPACA_SECRET_KEY) > 10)
    passed = key_ok and sec_ok
    check("api_keys_loaded", SEV_CRITICAL, passed,
          f"key={'ok' if key_ok else 'EMPTY'}  secret={'ok' if sec_ok else 'EMPTY'}"
          f"  url={ALPACA_BASE_URL}")


# ── 10. Universe symbol count ─────────────────────────────────────────────────

def check_universe():
    path = os.path.join(ARES_DIR, "universe.json")
    if not os.path.exists(path):
        check("universe", SEV_WARN, False, "universe.json missing")
        return
    try:
        with open(path) as f:
            u = json.load(f)
        count    = len(u.get("symbols", []))
        built_at = u.get("built_at", "unknown")[:10]
        passed   = count >= 50
        check("universe", SEV_WARN if not passed else SEV_INFO, passed,
              f"symbols={count}  built={built_at}")
    except Exception as e:
        check("universe", SEV_WARN, False, str(e))


# ── Run all checks ────────────────────────────────────────────────────────────

def run_all():
    check_keys_not_empty()
    check_alpaca_auth()
    check_alpaca_positions()
    check_file_freshness()
    check_macro_regime()
    check_engine_status()
    check_shadow_pipeline()
    check_outcomes()
    check_ledger()
    check_universe()


def print_report(critical_only: bool = False):
    run_all()

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*65}")
    print(f"  ARES HEALTH CHECK — {now}")
    print(f"{'='*65}")

    failures  = [r for r in results if not r["passed"]]
    criticals = [r for r in failures if r["sev"] == SEV_CRITICAL]
    warns     = [r for r in failures if r["sev"] == SEV_WARN]
    oks       = [r for r in results if r["passed"]]

    if criticals:
        print(f"\n  ❌ CRITICAL ({len(criticals)})")
        for r in criticals:
            print(f"     {r['name']:<30s}  {r['detail']}")

    if warns:
        print(f"\n  ⚠  WARN ({len(warns)})")
        for r in warns:
            print(f"     {r['name']:<30s}  {r['detail']}")

    if not critical_only:
        print(f"\n  ✓  OK / INFO ({len(oks)})")
        for r in oks:
            flag = "✓" if r["sev"] == SEV_OK else "·"
            print(f"     {flag} {r['name']:<28s}  {r['detail']}")

    print(f"\n{'='*65}")
    summary = f"  {len(criticals)} critical  {len(warns)} warn  {len(oks)} ok"
    if criticals:
        print(f"  STATUS: ⛔ NOT SAFE TO TRADE{summary}")
    elif warns:
        print(f"  STATUS: ⚠  DEGRADED — CHECK WARNS{summary}")
    else:
        print(f"  STATUS: ✅ HEALTHY{summary}")
    print(f"{'='*65}\n")

    return len(criticals)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ares pre-market health check")
    parser.add_argument("--json",     action="store_true", help="JSON output")
    parser.add_argument("--critical", action="store_true", help="Only print failures")
    args = parser.parse_args()

    if args.json:
        run_all()
        print(json.dumps({"timestamp": datetime.now().isoformat(), "checks": results}, indent=2))
        sys.exit(0)

    n_critical = print_report(critical_only=args.critical)
    sys.exit(1 if n_critical > 0 else 0)
