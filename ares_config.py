import os
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

# ares_config.py — Ares Trading System Configuration
# Last updated: 2026-05-29
#
# RULE: Every constant here must have a derivation comment or # TODO:DERIVE with method.
# Round numbers without justification are bugs.

import os

# ---------------------------------------------------------------------------
# ACCOUNT
# ---------------------------------------------------------------------------

ALPACA_API_KEY    = os.getenv("ARES_ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ARES_ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = os.getenv("ARES_ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# RF-22: Centralized feed selection.
# IEX ≈ 2-3% of consolidated tape — volume signals (b_volume_climax, f breakout vol)
# are biased on IEX. For live accounts use SIP (full consolidated tape).
# For paper/sandbox use IEX (free tier). Override via ARES_BAR_FEED env var.
_default_feed = "iex" if ("paper" in ALPACA_BASE_URL or "sandbox" in ALPACA_BASE_URL) else "sip"
BAR_FEED = os.getenv("ARES_BAR_FEED", _default_feed)

TOTAL_EQUITY      = 50_000.0   # Starting equity — update dynamically from broker

# ---------------------------------------------------------------------------
# ENGINE REGISTRY
# ---------------------------------------------------------------------------

# ENGINE_STATUS: "shadow" = classify but do not trade
#                "live"   = take real positions
#                "off"    = disabled
ENGINE_STATUS = {
    "A": "live",   # Momentum Continuation
    "B": "live",   # Mean Reversion — built first
    "C": "live",   # Squeeze Break
    "D": "off",    # Gap Reversion — deferred (needs intraday runtime)
    "E": "live",   # RS Rotation
    "F": "live",   # Structural Breakout
}

# ---------------------------------------------------------------------------
# CAPITAL ALLOCATION
# ---------------------------------------------------------------------------

# RF-8 fix: 5 active engines × 18% = 90%; 10% cash floor held permanently.
# Engine D is deferred — its allocation stays in the cash floor until intraday
# runtime is built. When D goes live: 6 engines × 15% = 90%, cash floor unchanged.
#
# IMPORTANT: These weights represent % of TOTAL_EQUITY, not 100% deployed.
# The cash floor (CASH_FLOOR_PCT below) is held back before weights are applied.
# Effective per-engine capital = TOTAL_EQUITY × (1 - CASH_FLOOR_PCT) × weight.
#
# Reallocation is ADVISORY only — never auto-shift
# TODO:DERIVE — shift toward winning engines once 15+ closed trades per engine
INITIAL_ENGINE_WEIGHTS = {
    "A": 0.18,
    "B": 0.18,
    "C": 0.18,
    "D": 0.00,   # Deferred — cash until intraday runtime built; slot stays in CASH_FLOOR
    "E": 0.18,
    "F": 0.18,
}
# 5 active engines × 0.18 = 0.90; remaining 0.10 is the CASH_FLOOR_PCT below.

# Hard cash floor — always held uninvested as a shock absorber (RF-8)
# Rationale: all-engine drawdowns can be correlated (see tail dependence notes in
# ARES_RESEARCH.md). 10% cash buffer prevents margin calls in correlated-loss scenarios.
# TODO:DERIVE — adjust floor based on max observed cross-engine drawdown correlation
# once 100+ total closed trades accumulate.
CASH_FLOOR_PCT = 0.10   # 10% of total equity always in cash

# Hard floor — no engine starved below this allocation (advisory gate)
ENGINE_CAPITAL_FLOOR = 0.10   # 10% of equity minimum per active engine

# Max positions per engine — prevents one engine from consuming all capital
# Derived from: engine_cap / typical_position_size ≈ $10K / $3-5K avg = 2-3
# TODO:DERIVE — set from empirical average position size after 30 trades per engine
MAX_POSITIONS_PER_ENGINE = {
    "A": 2,
    "B": 2,
    "C": 1,   # Binary outcome engine — concentrate capital
    "D": 1,   # Same-day only — one at a time
    "E": 2,
    "F": 2,
}

MAX_TOTAL_POSITIONS = 10   # Combined ceiling across all engines

# ---------------------------------------------------------------------------
# KELLY — PER ENGINE
# ---------------------------------------------------------------------------

# All engines start in shadow/bootstrap mode.
# Bootstrap base = 0.5 × composite fallback (conservative until own history exists)
# TODO:DERIVE — replace with engine-specific empirical Kelly after 30 closed trades per engine
#
# RF-8 WARNING: Kelly sizing is mathematically correct only for a known positive
# expectancy. Until RF-1/RF-3 establish edge (IC > 0.05 with statistical power),
# all sizing parameters below are CONSERVATIVE PLACEHOLDERS, not optimal fractions.
# Half-Kelly is only "safe" when the sign of edge is already confirmed.

# ---------------------------------------------------------------------------
# KELLY — PER ENGINE
# ---------------------------------------------------------------------------

# All engines start in shadow/bootstrap mode.
# TODO:DERIVE — replace with engine-specific empirical Kelly after 30 closed trades per engine
#
# RF-8 WARNING: Kelly sizing is mathematically correct only for a known positive
# expectancy. Until RF-1/RF-3 establish edge (IC > 0.05 with statistical power),
# all sizing parameters below are CONSERVATIVE PLACEHOLDERS, not optimal fractions.
# Half-Kelly is only "safe" when the sign of edge is already confirmed.
#
# RF-13 FIX: Bootstrap effective fraction = 0.025 × 0.5 = 0.0125.
# This is BELOW the KELLY_ENGINE_CLIPS min_pct (0.02), which would floor all positions
# at 2% regardless of conviction or macro scaling — making kelly_scale and macro_mult inert.
# FIX: KELLY_APPLY_FLOOR_IN_BOOTSTRAP = False disables the min_pct floor during bootstrap.
# Each engine applies: position = max(0, min(equity*max_pct, equity*fraction*scale*macro_mult))
# Floor re-engages post-calibration as a no-starve protection.

KELLY_BOOTSTRAP_FRACTION = 0.025   # 2.5% base until engine has own history
                                    # TODO:DERIVE from EVT tail on first 30 trades per engine

KELLY_SHADOW_MULTIPLIER = 0.50    # All engines run at 50% of bootstrap Kelly at launch
                                   # Advance to full Kelly after 30 clean closed trades

KELLY_APPLY_FLOOR_IN_BOOTSTRAP = False  # RF-13: disable min_pct floor during bootstrap phase

KELLY_ENGINE_CLIPS = {
    # (min_fraction, max_fraction) of equity per position
    # min_fraction only applies post-calibration (KELLY_APPLY_FLOOR_IN_BOOTSTRAP=False above)
    # TODO:DERIVE from EVT tail on closed trade returns per engine
    "A": (0.01, 0.10),   # RF-13: min lowered to 0.01 (below bootstrap 0.0125) for when floor re-engages
    "B": (0.01, 0.08),
    "C": (0.01, 0.06),
    "D": (0.01, 0.08),   # Deferred
    "E": (0.01, 0.10),
    "F": (0.01, 0.10),   # Structural — sizing derived from risk_per_share not fraction
}

# Assertion: bootstrap effective size must not exceed max_pct for any engine
_boot_effective = KELLY_BOOTSTRAP_FRACTION * KELLY_SHADOW_MULTIPLIER  # 0.0125
assert all(
    _boot_effective <= clips[1] for clips in KELLY_ENGINE_CLIPS.values()
), f"Bootstrap Kelly {_boot_effective:.4f} exceeds max_pct ceiling — reduce KELLY_BOOTSTRAP_FRACTION"

# ---------------------------------------------------------------------------
# IC CALIBRATION HORIZONS — IMMUTABLE ONCE TRADES ACCUMULATE
# ---------------------------------------------------------------------------
# Derived from academic holding period research:
#   A/B/C/F = 10d: Jegadeesh & Titman (1993) short-horizon window
#   E = 20d:       Moskowitz & Grinblatt (1999) industry momentum 1-month horizon
#   D = 1d:        Wibowo & Naimoli (2019) same-day gap fill
#
# WARNING: Do NOT change these values once any engine has accumulated closed trades.
# Changing N invalidates the IC history and makes all prior calibration meaningless.

IC_HORIZON_DAYS = {
    "A": 10,
    "B": 10,
    "C": 10,
    "D": 1,
    "E": 20,
    "F": 10,
}

# Minimum closed trades before IC weights replace bootstrap equal weights
IC_CALIBRATION_GATE = 30   # per engine

# ---------------------------------------------------------------------------
# STOP AND TRAIL PARAMETERS — ENGINE SPECIFIC
# ---------------------------------------------------------------------------
# All marked TODO:DERIVE — current values from blueprint research, not empirical

ENGINE_STOP_PARAMS = {
    "A": {
        "stop_mult_low_vol":  3.5,   # TODO:DERIVE — EVT on Engine A drawdowns
        "stop_mult_high_vol": 2.8,   # TODO:DERIVE — wider trend needs room to breathe
        # Linear interpolation between these based on atr_pct_0to1
    },
    "B": {
        "stop_mult_low_vol":  2.0,   # TODO:DERIVE — tight for MR (if not reverting, exit)
        "stop_mult_high_vol": 2.5,   # TODO:DERIVE
    },
    "C": {
        "stop_mult_low_vol":  1.5,   # TODO:DERIVE — binary thesis, tight stop
        "stop_mult_high_vol": 1.8,   # TODO:DERIVE
    },
    "D": {
        "stop_mult": 1.0,            # TODO:DERIVE — tight for same-day gap trade
    },
    "E": {
        "stop_mult": 3.0,            # TODO:DERIVE — rotation needs room for sector volatility
    },
    "F": {
        # Engine F uses structural stop (base low), not ATR-based
        # stop_price = consol_low - (stop_buffer_atr * ATR)
        "stop_buffer_atr": 0.10,     # TODO:DERIVE — small buffer below consolidation base
    },
}

# ---------------------------------------------------------------------------
# CLASSIFICATION THRESHOLDS — TODO:DERIVE FROM SHADOW MODE DATA
# ---------------------------------------------------------------------------
# WARNING: These are bootstrap defaults. All must be replaced with empirically
# derived values from the first 30 trading days of shadow mode.
# See ARES_MASTER_PLAN.md ARBITRARY CONSTANTS section.

CLASSIFICATION_THRESHOLDS = {
    "A": {
        "mom_gate":        0.05,   # TODO:DERIVE — 12-1 momentum min (5% excess return)
        "hi52_gate":       0.85,   # TODO:DERIVE — price within 15% of 52-week high
        "adx_gate":        25.0,   # Wilder (1978) — established trend threshold
        "engine_score_min": 0.60,  # TODO:DERIVE — composite score gate
    },
    "B": {
        "mr_score_pctile":    0.75,  # TODO:DERIVE
        "avwap_gate_atr":    -1.0,   # TODO:DERIVE — must be < -1.0 ATR below AVWAP
        "engine_score_min":   0.60,  # TODO:DERIVE
    },
    "C": {
        "squeeze_min_bars":   5,     # TODO:DERIVE — minimum squeeze duration before entry
        "hv_ratio_max":       0.80,  # TODO:DERIVE — HV/30d-HV ratio ceiling (compressed)
        "engine_score_min":   0.55,  # TODO:DERIVE — lower threshold, binary setup
    },
    "E": {
        "rs_stock_pctile":    0.70,  # TODO:DERIVE
        "rs_sector_pctile":   0.60,  # TODO:DERIVE
        "engine_score_min":   0.60,  # TODO:DERIVE
    },
    "F": {
        "breakout_vol_ratio": 1.80,  # TODO:DERIVE — 70th pctile of breakout volume distribution
        "close_position_min": 0.75,  # TODO:DERIVE — close must be in top 25% of bar
        "overhead_hvn_atr":   2.0,   # TODO:DERIVE — no HVN within this distance above
        "consol_bars_min":    15,    # TODO:DERIVE — minimum consolidation bars
        "engine_score_min":   0.60,  # TODO:DERIVE
    },
}

# ---------------------------------------------------------------------------
# MACRO GATE — ENGINE BEHAVIOR BY REGIME
# ---------------------------------------------------------------------------
# Each engine defines which regimes it fires in.
# RISK_ON=full, NEUTRAL=reduced, RISK_OFF=restricted, CRISIS=off

ENGINE_REGIME_GATE = {
    "A": {"RISK_ON": 1.0, "NEUTRAL": 0.75, "RISK_OFF": 0.0,  "CRISIS": 0.0},
    "B": {"RISK_ON": 0.5, "NEUTRAL": 1.0,  "RISK_OFF": 1.0,  "CRISIS": 0.0},
    # B fires hardest in NEUTRAL/RISK_OFF — MR edge strongest in fear-driven selloffs
    "C": {"RISK_ON": 1.0, "NEUTRAL": 1.0,  "RISK_OFF": 0.5,  "CRISIS": 0.0},
    "D": {"RISK_ON": 1.0, "NEUTRAL": 1.0,  "RISK_OFF": 0.0,  "CRISIS": 0.0},
    "E": {"RISK_ON": 1.0, "NEUTRAL": 0.75, "RISK_OFF": 0.0,  "CRISIS": 0.0},
    "F": {"RISK_ON": 1.0, "NEUTRAL": 0.75, "RISK_OFF": 0.0,  "CRISIS": 0.0},
    # F requires RISK_ON or NEUTRAL — breakouts in RISK_OFF fail at high rate
}
# Multiplier applied to kelly_scale — 0.0 = engine does not fire in that regime

# ---------------------------------------------------------------------------
# UNIVERSE
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# UNIVERSE FILTER CONSTANTS — single source of truth (RF-21)
# Previously these lived as hardcodes inside ares_universe.py (3.0/500/0.008)
# while ares_config.py had different values (5.0/1000/0.01). Now one place only.
# ---------------------------------------------------------------------------
UNIVERSE_PRICE_MIN      = 3.0           # Lower than Raptor — Engine B needs mid-cap MR candidates
UNIVERSE_PRICE_MAX      = 500.0
UNIVERSE_MIN_DOLLAR_VOL = 20_000_000    # $20M avg daily dollar volume (20d)
UNIVERSE_MIN_SHARE_VOL  = 500_000       # 500K avg daily share volume (20d)
UNIVERSE_MIN_ATR_PCT    = 0.008         # 0.8% ATR/price — minimum swing amplitude for MR

# ---------------------------------------------------------------------------
# DATA
# ---------------------------------------------------------------------------

BARS_LOOKBACK         = 120        # Minimum bars required for signal computation
ENGINE_E_LOOKBACK     = 80         # Sector ETF bars (Engine E RS signals)
SECTOR_ETFS = [
    "XLK", "XLF", "XLV", "XLE", "XLY",
    "XLI", "XLB", "XLC", "XLU", "XLRE", "XLP"
]

# ---------------------------------------------------------------------------
# SHADOW MODE
# ---------------------------------------------------------------------------

SHADOW_MIN_ENTRIES_FOR_LIVE = 30   # Per engine — must see 30 shadow setups before live capital
SHADOW_TARGET_CLASSIFICATION_RATE = (0.02, 0.08)  # 2-8% of universe per scan

# ---------------------------------------------------------------------------
# PORTFOLIO PROTECTION
# ---------------------------------------------------------------------------

MAX_PORTFOLIO_DRAWDOWN = 0.12      # TODO:DERIVE from EVT tail on portfolio returns
                                    # Triggers advisory alert — not auto-exit
HEAT_ALERT_THRESHOLD   = 0.08      # Advisory alert when any engine is down > 8% from peak

# ---------------------------------------------------------------------------
# CAPITAL ALLOCATOR
# ---------------------------------------------------------------------------

REALLOCATION_GATE_TRADES = 15      # Minimum closed trades per engine before advisory fires
REALLOCATION_LOOKBACK    = 20      # Trade window for rolling Sharpe in allocator
REALLOCATION_MIN_WEIGHT  = 0.10    # No engine below 10% while active
