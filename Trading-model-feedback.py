"""
Raptor v6.0 — Configuration
Jim Simons principle: risk budget follows conviction, not fixed rules
"""
import os
from dataclasses import dataclass, field
from typing import Dict

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

@dataclass
class AlpacaConfig:
    api_key: str = os.getenv("ALPACA_API_KEY", "")
    secret_key: str = os.getenv("ALPACA_SECRET_KEY", "")
    base_url: str = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    data_url: str = "https://data.alpaca.markets"
    news_url: str = "https://data.alpaca.markets/v1beta1/news"
    feed: str = "iex"

@dataclass
class SignalConfig:
    lookback_days: int = 200 # Need 200 for stable factor stats

@dataclass
class RiskConfig:
    kelly_fraction: float = 0.25 # Was 0.15 — size winners
    max_position_pct: float = 0.12 # Was 0.08 — allow concentration
    min_position_pct: float = 0.02
    max_positions: int = 20 # Was 15
    max_sector_pct: float = 0.40 # Was 0.30
    max_pairwise_correlation: float = 0.85 # Was 0.70 — stop fighting trends
    initial_stop_atr_mult: float = 4.0 # WAS 3.0 — THIS WAS KILLING YOU
    # Bertsimas & Lo optimal exit: widen early, tighten late
    trail_early_atr: float = 3.0 # Days 1-3
    trail_mid_atr: float = 2.5 # Days 4-10
    trail_late_atr: float = 2.0 # Days 11-20
    trail_final_atr: float = 1.5 # Days 21+
    trail_early_days: int = 3
    trail_mid_days: int = 10
    trail_late_days: int = 20
    atr_period: int = 14
    time_stop_days: int = 21 # 3 weeks max, not 90 minutes
    min_hold_days: int = 2
    max_portfolio_drawdown: float = 0.15

@dataclass
class BacktestConfig:
    start_date: str = "2020-01-01"
    end_date: str = "2025-12-31"
    initial_capital: float = 100_000.0
    slippage_bps: float = 5.0
    benchmark_symbol: str = "SPY"
    risk_free_rate: float = 0.05

@dataclass
class ExecutionConfig:
    scan_time: str = "09:35"
    exit_scan_time: str = "15:45"
    order_type: str = "limit"
    limit_offset_bps: float = 10.0
    max_orders_per_scan: int = 5
    paper_trading: bool = True

@dataclass
class LogConfig:
    log_dir: str = "logs"
    trade_log_file: str = "trades.csv"
    signal_log_file: str = "signals.csv"
    backtest_results_dir: str = "backtest_results"
    log_level: str = "INFO"

@dataclass
class RaptorConfig:
    version: str = "6.0.0"
    alpaca: AlpacaConfig = field(default_factory=AlpacaConfig)
    signals: SignalConfig = field(default_factory=SignalConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    log: LogConfig = field(default_factory=LogConfig)

    def validate_all(self):
        assert self.alpaca.api_key, "ALPACA_API_KEY not set"
        assert self.alpaca.secret_key, "ALPACA_SECRET_KEY not set"
        print(f"[CONFIG] Raptor v{self.version} — Simons mode: adaptive weights, wide stops")
        return self

CONFIG = RaptorConfig()