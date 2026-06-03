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
ALPACA_BASE_URL   = "https://paper-api.alpaca.markets"  # paper account

TOTAL_EQUITY      = 50_000.0   # Starting equity — update dynamically from broker

# ---------------------------------------------------------------------------
# ENGINE REGISTRY
# ---------------------------------------------------------------------------

# ENGINE_STATUS: "shadow" = classify but do not trade
#                "live"   = take real positions
#                "off"    = disabled
ENGINE_STATUS = {
    "A": "shadow",   # Momentum Continuation
    "B": "shadow",   # Mean Reversion — built first
    "C": "shadow",   # Squeeze Break
    "D": "off",      # Gap Reversion — deferred (needs intraday runtime)
    "E": "shadow",   # RS Rotation
    "F": "shadow",   # Structural Breakout
}

# ---------------------------------------------------------------------------
# CAPITAL ALLOCATION
# ---------------------------------------------------------------------------

# Initial equal allocation across 5 active engines (D deferred = capital held in cash)
# Reallocation is ADVISORY only — never auto-shift
# TODO:DERIVE — shift toward winning engines once 15+ closed trades per engine
INITIAL_ENGINE_WEIGHTS = {
    "A": 0.20,
    "B": 0.20,
    "C": 0.20,
    "D": 0.00,   # Deferred — cash until intraday runtime built
    "E": 0.20,
    "F": 0.20,
}

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

KELLY_BOOTSTRAP_FRACTION = 0.025   # 2.5% base until engine has own history
                                    # TODO:DERIVE from EVT tail on first 30 trades per engine

KELLY_SHADOW_MULTIPLIER = 0.50    # All engines run at 50% of bootstrap Kelly at launch
                                   # Advance to full Kelly after 30 clean closed trades

KELLY_ENGINE_CLIPS = {
    # (min_fraction, max_fraction) of equity per position
    # TODO:DERIVE from EVT tail on closed trade returns per engine
    "A": (0.02, 0.10),
    "B": (0.02, 0.08),   # Tighter — MR trades have shorter thesis window
    "C": (0.02, 0.06),   # Tightest — binary outcome, high uncertainty
    "D": (0.02, 0.08),   # Deferred
    "E": (0.02, 0.10),   # Longer hold, wider — rotation trades need room
    "F": (0.02, 0.10),   # Structural — sizing derived from risk_per_share not fraction
}

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
        "trend_score_pctile": 0.70,  # TODO:DERIVE — 70th pctile of TREND scores in universe
        "vol_score_pctile":   0.60,  # TODO:DERIVE
        "mr_ceiling":         0.50,  # TODO:DERIVE — max MR score before routing to B or composite
        "engine_score_min":   0.60,  # TODO:DERIVE — logistic score threshold
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

UNIVERSE_PRICE_MIN    = 5.0        # Same as Raptor
UNIVERSE_PRICE_MAX    = 1000.0
UNIVERSE_VOLUME_MIN   = 500_000
UNIVERSE_DOLLAR_VOL   = 20_000_000
UNIVERSE_DAILY_RANGE  = 0.01       # 1% minimum daily range

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
