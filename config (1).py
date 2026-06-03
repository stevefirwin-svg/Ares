"""
=============================================================================
ELITE TRADING BOT - CONFIGURATION v3.2
=============================================================================
Edit this file to customize your trading bot settings.
ALL user-configurable parameters are here.
=============================================================================
"""

# ─────────────────────────────────────────────────────────────────────────────
# BROKER API CREDENTIALS
# ─────────────────────────────────────────────────────────────────────────────
API_KEY    = "YOUR_ALPACA_API_KEY_HERE"
API_SECRET = "YOUR_ALPACA_SECRET_KEY_HERE"
PAPER_TRADING = True

# ─────────────────────────────────────────────────────────────────────────────
# PORTFOLIO SETTINGS
# ─────────────────────────────────────────────────────────────────────────────
PORTFOLIO_SIZE        = 2500.00
MAX_POSITION_SIZE_PCT = 0.15
MIN_POSITION_SIZE_PCT = 0.03
MAX_OPEN_POSITIONS    = 8         # Increased from 6 to handle larger watchlist
MAX_DAILY_LOSS_PCT    = 0.03
MAX_DRAWDOWN_PCT      = 0.10

# ─────────────────────────────────────────────────────────────────────────────
# RISK MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_STOP_LOSS_PCT   = 0.02
DEFAULT_TAKE_PROFIT_PCT = 0.04
USE_TRAILING_STOP       = True
TRAILING_STOP_PCT       = 0.015

# ─────────────────────────────────────────────────────────────────────────────
# SECTOR CLASSIFICATION
# Used by the strategy engine to apply sector-specific signal tuning.
# AI/Tech stocks get higher momentum weighting.
# Energy stocks get higher trend-following weighting.
# ─────────────────────────────────────────────────────────────────────────────
SECTOR_MAP = {
    # AI & Semiconductors
    "NVDA":  "AI_SEMI",  "AMD":   "AI_SEMI",  "AVGO":  "AI_SEMI",
    "QCOM":  "AI_SEMI",  "ARM":   "AI_SEMI",  "SMCI":  "AI_SEMI",
    "MRVL":  "AI_SEMI",  "ALAB":  "AI_SEMI",  "TSM":   "AI_SEMI",
    "INTC":  "AI_SEMI",  "TXN":   "AI_SEMI",  "LRCX":  "AI_SEMI",
    "AMAT":  "AI_SEMI",  "KLAC":  "AI_SEMI",  "ASML":  "AI_SEMI",

    # AI Software & Cloud
    "MSFT":  "AI_SOFT",  "GOOGL": "AI_SOFT",  "META":  "AI_SOFT",
    "AMZN":  "AI_SOFT",  "CRM":   "AI_SOFT",  "NOW":   "AI_SOFT",
    "PLTR":  "AI_SOFT",  "AI":    "AI_SOFT",  "BBAI":  "AI_SOFT",
    "SOUN":  "AI_SOFT",  "PATH":  "AI_SOFT",  "GTLB":  "AI_SOFT",

    # Energy - Oil & Gas
    "XOM":   "ENERGY",   "CVX":   "ENERGY",   "COP":   "ENERGY",
    "OXY":   "ENERGY",   "SLB":   "ENERGY",   "HAL":   "ENERGY",
    "MPC":   "ENERGY",   "VLO":   "ENERGY",   "PSX":   "ENERGY",
    "XLE":   "ENERGY",   "DVN":   "ENERGY",   "FANG":  "ENERGY",

    # Clean Energy & Solar
    "ENPH":  "CLEAN",    "SEDG":  "CLEAN",    "FSLR":  "CLEAN",
    "NEE":   "CLEAN",    "ICLN":  "CLEAN",    "PLUG":  "CLEAN",
    "BE":    "CLEAN",    "RUN":   "CLEAN",    "ARRY":  "CLEAN",

    # Biotech & Healthcare AI
    "MRNA":  "BIOTECH",  "BNTX":  "BIOTECH",  "RXRX":  "BIOTECH",
    "EXAI":  "BIOTECH",  "RARE":  "BIOTECH",  "BEAM":  "BIOTECH",
    "CRSP":  "BIOTECH",  "NTLA":  "BIOTECH",  "EDIT":  "BIOTECH",
    "INVA":  "BIOTECH",  "ABBV":  "BIOTECH",  "AMGN":  "BIOTECH",
}

# Sector-specific strategy weight overrides
# Each sector gets tuned weights based on how it behaves
SECTOR_WEIGHTS = {
    "AI_SEMI": {
        "trend_following": 0.35,  # Semis trend strongly
        "momentum":        0.30,  # High momentum moves
        "mean_reversion":  0.10,  # Less mean-reverting
        "statistical_arb": 0.15,
        "volatility":      0.10,
    },
    "AI_SOFT": {
        "trend_following": 0.30,
        "momentum":        0.30,  # Software has strong momentum
        "mean_reversion":  0.15,
        "statistical_arb": 0.15,
        "volatility":      0.10,
    },
    "ENERGY": {
        "trend_following": 0.40,  # Energy follows macro trends
        "momentum":        0.20,
        "mean_reversion":  0.20,  # Also mean-reverts with oil price
        "statistical_arb": 0.10,
        "volatility":      0.10,
    },
    "CLEAN": {
        "trend_following": 0.25,
        "momentum":        0.35,  # Clean energy runs on momentum/news
        "mean_reversion":  0.15,
        "statistical_arb": 0.15,
        "volatility":      0.10,
    },
    "BIOTECH": {
        "trend_following": 0.20,
        "momentum":        0.35,  # Biotech is momentum/catalyst driven
        "mean_reversion":  0.20,
        "statistical_arb": 0.15,
        "volatility":      0.10,
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# EXPANDED WATCHLIST — 60 stocks across all sectors
# ─────────────────────────────────────────────────────────────────────────────
WATCHLIST = [
    # ── AI & SEMICONDUCTORS (core AI infrastructure) ──────────────────────
    "NVDA",   # AI chip leader — #1 AI play
    "AMD",    # GPU/CPU competitor to NVDA
    "AVGO",   # Networking chips for AI data centers
    "QCOM",   # Mobile + edge AI chips
    "ARM",    # CPU architecture for AI devices
    "SMCI",   # AI server manufacturer
    "MRVL",   # Data center networking chips
    "TSM",    # Manufactures all major AI chips
    "LRCX",   # Chip manufacturing equipment
    "AMAT",   # Semiconductor equipment

    # ── AI SOFTWARE & CLOUD ────────────────────────────────────────────────
    "MSFT",   # Azure AI + OpenAI partnership
    "GOOGL",  # Gemini AI + TPU chips
    "META",   # LLaMA AI + massive compute
    "AMZN",   # AWS AI services
    "CRM",    # AI-powered enterprise software
    "NOW",    # ServiceNow — enterprise AI
    "PLTR",   # AI data analytics platform
    "AI",     # C3.ai — pure AI software play

    # ── BIG TECH (proven winners from backtest) ────────────────────────────
    "AAPL",   # Strong trend performer
    "NFLX",   # Consistent momentum
    "MA",     # Steady trend performer
    "V",      # Financial + AI payments
    "JPM",    # AI in banking
    "GS",     # AI in finance

    # ── BROAD MARKET ETFs ──────────────────────────────────────────────────
    "QQQ",    # Nasdaq 100 — tech heavy
    "SPY",    # S&P 500 broad market
    "SOXX",   # Semiconductor ETF
    "AIQ",    # AI & Tech ETF
    "BOTZ",   # Robotics & AI ETF

    # ── ENERGY — OIL & GAS ────────────────────────────────────────────────
    "XOM",    # Largest US oil company
    "CVX",    # Major integrated oil
    "COP",    # Large independent E&P
    "OXY",    # Berkshire-backed oil play
    "SLB",    # Oilfield services leader
    "HAL",    # Oilfield services
    "MPC",    # Refining + distribution
    "XLE",    # Energy sector ETF
    "DVN",    # Devon Energy — shale
    "FANG",   # Diamondback Energy

    # ── CLEAN ENERGY & SOLAR ──────────────────────────────────────────────
    "NEE",    # NextEra — largest clean energy
    "ENPH",   # Solar microinverters
    "FSLR",   # First Solar — panels
    "ICLN",   # Clean energy ETF
    "PLUG",   # Hydrogen fuel cells
    "BE",     # Bloom Energy — fuel cells
    "RUN",    # Sunrun — residential solar

    # ── BIOTECH & HEALTHCARE AI ────────────────────────────────────────────
    "MRNA",   # mRNA technology + AI drug discovery
    "ABBV",   # Large cap biotech
    "AMGN",   # Biotech + AI therapeutics
    "RXRX",   # AI drug discovery platform
    "BEAM",   # Gene editing
    "CRSP",   # CRISPR gene editing
    "EXAI",   # Exscientia — AI pharma
]

# Minimum average daily volume
MIN_AVG_VOLUME = 500_000  # Lowered slightly for smaller biotech/clean energy stocks

# ─────────────────────────────────────────────────────────────────────────────
# DEFAULT STRATEGY WEIGHTS (used when sector not found)
# ─────────────────────────────────────────────────────────────────────────────
STRATEGY_WEIGHTS = {
    "trend_following": 0.30,
    "mean_reversion":  0.20,
    "momentum":        0.25,
    "statistical_arb": 0.15,
    "volatility":      0.10,
}

# ─────────────────────────────────────────────────────────────────────────────
# SECTOR POSITION LIMITS
# Prevent over-concentration in one sector
# ─────────────────────────────────────────────────────────────────────────────
MAX_POSITIONS_PER_SECTOR = 2   # Never hold more than 2 from same sector at once

# ─────────────────────────────────────────────────────────────────────────────
# TECHNICAL INDICATOR PARAMETERS
# ─────────────────────────────────────────────────────────────────────────────
EMA_FAST          = 8
EMA_MEDIUM        = 21
EMA_SLOW          = 50
EMA_TREND         = 200
RSI_PERIOD        = 14
RSI_OVERSOLD      = 30
RSI_OVERBOUGHT    = 70
MACD_FAST         = 12
MACD_SLOW         = 26
MACD_SIGNAL       = 9
BB_PERIOD         = 20
BB_STD            = 2.0
ATR_PERIOD        = 14
ADX_PERIOD        = 14
ADX_THRESHOLD     = 25
VOLUME_SMA_PERIOD = 20
VOLUME_SURGE_MULT = 1.5

# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL THRESHOLDS
# For live trading use 0.55. For backtesting use 0.30.
# ─────────────────────────────────────────────────────────────────────────────
MIN_SIGNAL_SCORE    = 0.55   # Live trading threshold
MIN_WIN_PROBABILITY = 0.55
SIGNAL_LOOKBACK     = 100

# ─────────────────────────────────────────────────────────────────────────────
# EXECUTION SETTINGS
# ─────────────────────────────────────────────────────────────────────────────
BAR_TIMEFRAME      = "5Min"
SCAN_INTERVAL_SEC  = 60
USE_LIMIT_ORDERS   = True
LIMIT_SLIPPAGE_PCT = 0.001
MARKET_OPEN_HOUR   = 9
MARKET_OPEN_MIN    = 35
MARKET_CLOSE_HOUR  = 15
MARKET_CLOSE_MIN   = 45

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
LOG_LEVEL      = "INFO"
LOG_FILE       = "logs/trading_bot.log"
TRADE_LOG_FILE = "logs/trades.csv"
