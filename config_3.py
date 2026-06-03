"""
Raptor v5.0 — Swing Trading Configuration
==========================================
2–10 day hold period  |  Daily bars  |  FRED macro overlay  |  Alpaca news sentiment

All tunable parameters in ONE file. No magic numbers buried in modules.
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ─────────────────────────────────────────────
# API CREDENTIALS
# ─────────────────────────────────────────────
@dataclass
class AlpacaConfig:
    api_key: str = os.getenv("ALPACA_API_KEY", "")
    secret_key: str = os.getenv("ALPACA_SECRET_KEY", "")
    base_url: str = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    data_url: str = "https://data.alpaca.markets"
    news_url: str = "https://data.alpaca.markets/v1beta1/news"
    feed: str = "iex"  # 'iex' for free, 'sip' for paid


@dataclass
class FREDConfig:
    api_key: str = os.getenv("FRED_API_KEY", "")
    base_url: str = "https://api.stlouisfed.org/fred/series/observations"
    # Series IDs for macro regime signals
    series: Dict[str, str] = field(default_factory=lambda: {
        "yield_curve_10y2y": "T10Y2Y",       # 10Y-2Y spread (recession indicator)
        "fed_funds_rate": "FEDFUNDS",          # Fed funds effective rate
        "initial_claims": "ICSA",              # Weekly initial jobless claims
        "vix": "VIXCLS",                       # VIX daily close
        "credit_spread": "BAMLC0A4CRPSYTW",   # ICE BofA US Corp BBB spread
        "cpi_yoy": "CPIAUCSL",                # CPI urban consumers (monthly)
        "ism_pmi": "ISM/MAN_PMI",             # ISM Manufacturing PMI
        "m2_money_supply": "M2SL",            # M2 money stock
        "retail_sales": "RSXFS",              # Advance retail sales
        "unemployment_rate": "UNRATE",         # Civilian unemployment rate
    })
    cache_hours: int = 6  # How long to cache FRED responses locally


# ─────────────────────────────────────────────
# UNIVERSE CONSTRUCTION
# ─────────────────────────────────────────────
@dataclass
class UniverseConfig:
    min_price: float = 8.0
    max_price: float = 500.0
    min_avg_volume: int = 500_000          # 20-day avg volume floor
    min_dollar_volume: float = 5_000_000   # Daily dollar volume floor
    min_market_cap: float = 1e9            # $1B minimum
    max_universe_size: int = 200           # Hard cap after scoring
    exclude_sectors: List[str] = field(default_factory=lambda: [
        "Utilities",  # Too slow for swing trading
    ])
    adr_min: float = 1.5  # Min avg daily range % (need volatility for swings)


# ─────────────────────────────────────────────
# SIGNAL ENGINE — SWING FACTORS
# ─────────────────────────────────────────────
@dataclass
class SignalConfig:
    """
    16-factor swing alpha engine.
    Weights must sum to 1.0 — enforced at startup.
    """
    # ── Momentum / Mean-Reversion (daily timeframe) ──
    factor_weights: Dict[str, float] = field(default_factory=lambda: {
        # --- Momentum cluster (45%) ---
        "roc_5d":               0.09,   # 5-day rate of change
        "roc_10d":              0.08,   # 10-day rate of change
        "macd_histogram":       0.09,   # MACD(12,26,9) histogram slope
        "adx_trend_strength":   0.06,   # ADX > 20 = trending
        "ema_crossover":        0.07,   # 8/21 EMA cross recency & magnitude
        "price_vs_vwap":        0.06,   # Daily close vs 5d rolling VWAP

        # --- Mean-reversion cluster (20%) ---
        "rsi_swing":            0.07,   # RSI(5) continuous mean-reversion
        "bollinger_reversion":  0.07,   # % distance from Bollinger(20,2) bands
        "stochastic_cross":     0.06,   # Stoch %K/%D cross in extreme zones

        # --- Volume / Microstructure (13%) ---
        "volume_surge":         0.05,   # Volume vs 20d avg (gradient)
        "obv_divergence":       0.04,   # OBV trend + divergence
        "vwap_reclaim":         0.04,   # VWAP reclaim on daily close

        # --- Macro / Regime (12%) ---
        "fred_regime_tilt":     0.05,   # FRED composite macro score
        "vix_regime":           0.03,   # VIX level (dampened)
        "sector_rotation":      0.04,   # Sector RS rank vs SPY

        # --- Sentiment (10%) ---
        "news_sentiment":       0.10,   # Alpaca news sentiment aggregate
    })

    # Factor-level params
    rsi_period: int = 5
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    bollinger_period: int = 20
    bollinger_std: float = 2.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    adx_period: int = 14
    adx_threshold: float = 20.0
    ema_fast: int = 8
    ema_slow: int = 21
    stoch_k_period: int = 14
    stoch_d_period: int = 3
    stoch_oversold: float = 20.0
    stoch_overbought: float = 80.0
    volume_surge_threshold: float = 1.8  # 1.8x 20d avg
    lookback_days: int = 60  # How much daily history to fetch for calcs

    # Composite scoring
    min_composite_score: float = 0.10    # Calibrated to actual score distribution
    min_factors_positive: int = 7        # At least 7 of 16 factors must be positive
    ic_decay_halflife: int = 10          # IC weighting halflife in days

    def validate(self):
        total = sum(self.factor_weights.values())
        assert abs(total - 1.0) < 0.001, f"Factor weights sum to {total}, must be 1.0"
        assert len(self.factor_weights) == 16, f"Expected 16 factors, got {len(self.factor_weights)}"


# ─────────────────────────────────────────────
# RISK MANAGEMENT
# ─────────────────────────────────────────────
@dataclass
class RiskConfig:
    # Position sizing
    kelly_fraction: float = 0.15          # Reduced Kelly (full Kelly = 1.0)
    kelly_bayesian_shrinkage: float = 0.5 # Shrink toward prior (conservative)
    max_position_pct: float = 0.08        # 8% max per position
    min_position_pct: float = 0.02        # 2% min per position
    max_positions: int = 15               # Portfolio breadth cap
    min_positions: int = 3                # Minimum diversification

    # Correlation & concentration
    max_sector_pct: float = 0.30          # 30% max in any sector
    max_pairwise_correlation: float = 0.70 # Block if corr > 0.7 with existing
    correlation_lookback: int = 60         # Days for correlation calc

    # Stop-loss / take-profit (swing-calibrated)
    initial_stop_atr_mult: float = 1.5    # Initial stop = 1.5x ATR(14)
    trailing_stop_atr_mult: float = 1.0   # Trail at 1.0x ATR(14) once in profit
    take_profit_atr_mult: float = 3.0     # TP target = 3x ATR(14) — 2:1 R:R min
    atr_period: int = 14
    time_stop_days: int = 10              # Force exit after 10 days (max hold)
    min_hold_days: int = 2                # Don't exit before 2 days (avoid noise)

    # Drawdown protection
    max_portfolio_drawdown: float = 0.12  # 12% max drawdown → reduce exposure
    drawdown_reduction_factor: float = 0.5 # Cut position sizes by 50% in DD
    drawdown_state_file: str = "drawdown_state.json"

    # Regime gating
    halt_in_crisis: bool = True           # Zero new entries in CRISIS regime
    reduce_in_bearish: float = 0.5        # 50% position size in BEARISH


# ─────────────────────────────────────────────
# BACKTEST ENGINE
# ─────────────────────────────────────────────
@dataclass
class BacktestConfig:
    start_date: str = "2020-01-01"
    end_date: str = "2025-12-31"
    initial_capital: float = 100_000.0
    commission_per_share: float = 0.0     # Alpaca = zero commission
    slippage_bps: float = 5.0             # 5 bps slippage estimate
    benchmark_symbol: str = "SPY"
    risk_free_rate: float = 0.05          # For Sharpe calc (current ~5%)
    min_trades_for_validity: int = 50     # Need 50+ trades to trust results
    walk_forward_windows: int = 5         # Number of WF-optimization windows
    walk_forward_train_pct: float = 0.70  # 70% train / 30% test per window


# ─────────────────────────────────────────────
# EXECUTION & SCHEDULING
# ─────────────────────────────────────────────
@dataclass
class ExecutionConfig:
    scan_time: str = "09:35"              # Run scan 5 min after open
    exit_scan_time: str = "15:45"         # Check exits 15 min before close
    order_type: str = "limit"             # 'market' or 'limit'
    limit_offset_bps: float = 10.0        # Limit price = mid ± 10 bps
    max_orders_per_scan: int = 5          # Don't flood with orders
    paper_trading: bool = True            # MUST be True until 30-day validation


# ─────────────────────────────────────────────
# LOGGING & PERSISTENCE
# ─────────────────────────────────────────────
@dataclass
class LogConfig:
    log_dir: str = "logs"
    trade_log_file: str = "trades.csv"
    signal_log_file: str = "signals.csv"
    portfolio_snapshot_file: str = "portfolio_snapshots.csv"
    backtest_results_dir: str = "backtest_results"
    log_level: str = "INFO"               # DEBUG for development


# ─────────────────────────────────────────────
# MASTER CONFIG — single import point
# ─────────────────────────────────────────────
@dataclass
class RaptorConfig:
    version: str = "5.0.0"
    alpaca: AlpacaConfig = field(default_factory=AlpacaConfig)
    fred: FREDConfig = field(default_factory=FREDConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    signals: SignalConfig = field(default_factory=SignalConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    log: LogConfig = field(default_factory=LogConfig)

    def validate_all(self):
        """Run all config validations at startup."""
        self.signals.validate()
        assert self.alpaca.api_key, "ALPACA_API_KEY not set"
        assert self.alpaca.secret_key, "ALPACA_SECRET_KEY not set"
        assert self.fred.api_key, "FRED_API_KEY not set"
        assert self.risk.max_position_pct * self.risk.max_positions <= 1.2, \
            "Position sizing allows > 120% exposure — reduce max_position_pct or max_positions"
        assert self.risk.take_profit_atr_mult > self.risk.initial_stop_atr_mult, \
            "Take profit must exceed initial stop for positive R:R"
        print(f"[CONFIG] Raptor v{self.version} config validated ✓")
        return self


# Singleton for easy import
CONFIG = RaptorConfig()
