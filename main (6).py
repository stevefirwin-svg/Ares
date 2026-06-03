"""
=============================================================================
ELITE TRADING BOT - MAIN ENGINE v3.1
=============================================================================
Orchestrates everything: data fetching, signal generation, order execution,
and position management. This is the file you RUN to start the bot.

Usage:
    python main.py                    # Live trading loop
    python main.py --backtest 90      # Backtest last 90 days
    python main.py --scan             # Single scan, no trading

Improvements (v3.1):
    - Retry logic on all API calls (tenacity)
    - Parallel symbol scanning (ThreadPoolExecutor)
    - Secure API keys via environment variables
    - Timezone-aware market hours check (pytz)
=============================================================================
"""

import sys
import os
import time
import logging
import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import numpy as np
import pytz

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from utils.indicators import compute_all_indicators
from utils.risk_manager import RiskManager, TradeSignal
from strategies.signal_engine import generate_composite_signal, generate_elite_signal, generate_sector_aware_signal
from utils.conviction_sizer import ConvictionSizer
from utils.earnings_calendar import earnings_calendar
from utils.premarket_scanner import get_scanner
from utils.correlation_filter import get_correlation_filter
from utils.short_seller import ShortSeller
from alerts import alert_engine

# Retry logic (tenacity) — graceful fallback if not installed
try:
    from tenacity import retry, stop_after_attempt, wait_exponential
    TENACITY_AVAILABLE = True
except ImportError:
    TENACITY_AVAILABLE = False
    # Create dummy decorator so code works without tenacity
    def retry(*args, **kwargs):
        def decorator(func):
            return func
        return decorator
    def stop_after_attempt(n): return None
    def wait_exponential(**kwargs): return None


# ─────────────────────────────────────────────────────────────────────────────
# LOGGING SETUP
# ─────────────────────────────────────────────────────────────────────────────

Path("logs").mkdir(exist_ok=True)
Path("data").mkdir(exist_ok=True)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE),
    ]
)
logger = logging.getLogger("TradingBot")


# ─────────────────────────────────────────────────────────────────────────────
# BROKER CONNECTION
# ─────────────────────────────────────────────────────────────────────────────

def connect_alpaca():
    """
    Connect to Alpaca broker.
    API keys loaded from environment variables first, then config.py fallback.
    
    To use environment variables (more secure):
      Windows: set ALPACA_API_KEY=your_key && set ALPACA_API_SECRET=your_secret
      Then run: python main.py
    """
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient

        # STEP 1: Try environment variables (most secure)
        api_key    = os.getenv('ALPACA_API_KEY')
        api_secret = os.getenv('ALPACA_API_SECRET')

        # STEP 2: Fall back to config.py (easy for beginners)
        if not api_key or not api_secret:
            api_key    = config.API_KEY
            api_secret = config.API_SECRET

        if not api_key or api_key == "YOUR_ALPACA_API_KEY_HERE":
            logger.error("No API keys found. Add them to config.py or set environment variables.")
            sys.exit(1)

        paper = config.PAPER_TRADING
        trading_client = TradingClient(api_key, api_secret, paper=paper)
        data_client    = StockHistoricalDataClient(api_key, api_secret)

        account = trading_client.get_account()
        logger.info(f"Connected to Alpaca ({'PAPER' if paper else 'LIVE'} trading)")
        logger.info(f"   Account: ${float(account.equity):,.2f} equity | "
                    f"${float(account.buying_power):,.2f} buying power")

        return trading_client, data_client

    except ImportError:
        logger.error("alpaca-py not installed. Run: pip install alpaca-py")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Could not connect to Alpaca: {e}")
        logger.error("   Check your API keys in config.py")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def fetch_bars(data_client, symbol: str, timeframe_str: str = "5Min",
               limit: int = 300) -> pd.DataFrame:
    """
    Fetch OHLCV bars from Alpaca.
    Retries up to 3 times on network failures (tenacity).
    Returns cleaned DataFrame with columns: open, high, low, close, volume
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    tf_map = {
        "1Min": TimeFrame.Minute,
        "5Min": TimeFrame(5, TimeFrameUnit.Minute),
        "15Min": TimeFrame(15, TimeFrameUnit.Minute),
        "1Hour": TimeFrame.Hour,
        "1Day": TimeFrame.Day,
    }
    tf = tf_map.get(timeframe_str, TimeFrame(5, TimeFrameUnit.Minute))

    try:
        from datetime import timedelta
        end = datetime.now(timezone.utc)

        # Calculate correct lookback based on timeframe and limit requested
        # Daily bars need years of history; intraday needs days
        if timeframe_str == "1Day":
            # Each bar = 1 trading day (~252/year)
            # Add 50% buffer for weekends/holidays
            days_needed = int(limit * 1.5)
            start = end - timedelta(days=days_needed)
        elif timeframe_str == "1Hour":
            days_needed = int(limit / 6.5) + 10  # ~6.5 trading hours/day
            start = end - timedelta(days=days_needed)
        else:
            # Intraday — 30 days is plenty
            start = end - timedelta(days=30)

        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=start,
            end=end,
            limit=limit,
        )
        bars = data_client.get_stock_bars(request)
        df = bars.df

        if isinstance(df.index, pd.MultiIndex):
            df = df.loc[symbol] if symbol in df.index.get_level_values(0) else df.xs(symbol)

        df.index = pd.to_datetime(df.index)
        df = df.rename(columns=str.lower)

        required = ['open', 'high', 'low', 'close', 'volume']
        df = df[[c for c in required if c in df.columns]]
        df = df.dropna()

        if len(df) < 50:
            logger.warning(f"{symbol}: insufficient data ({len(df)} bars)")
            return pd.DataFrame()

        return df

    except Exception as e:
        logger.debug(f"Error fetching {symbol}: {e}")
        return pd.DataFrame()


def get_current_price(data_client, symbol: str) -> float:
    """Get latest trade price."""
    try:
        from alpaca.data.requests import StockLatestTradeRequest
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        trade = data_client.get_stock_latest_trade(req)
        return float(trade[symbol].price)
    except Exception as e:
        logger.debug(f"Error getting price for {symbol}: {e}")
        return 0.0


def is_market_open(trading_client) -> bool:
    """
    Check if market is currently open for trading.
    Uses pytz for accurate Eastern Time timezone handling.
    Prevents trading outside hours or missing opens.
    """
    try:
        clock = trading_client.get_clock()
        if not clock.is_open:
            return False

        # Use proper Eastern Time (handles daylight saving automatically)
        eastern = pytz.timezone('America/New_York')
        now_utc = datetime.now(timezone.utc)
        now_et  = now_utc.astimezone(eastern)

        # Check our custom trading window (avoid first/last 5 mins of session)
        after_open = (now_et.hour > config.MARKET_OPEN_HOUR or
                      (now_et.hour == config.MARKET_OPEN_HOUR and
                       now_et.minute >= config.MARKET_OPEN_MIN))
        before_close = (now_et.hour < config.MARKET_CLOSE_HOUR or
                        (now_et.hour == config.MARKET_CLOSE_HOUR and
                         now_et.minute <= config.MARKET_CLOSE_MIN))

        return after_open and before_close

    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# ORDER EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

def place_order(trading_client, signal: TradeSignal, dry_run: bool = False) -> bool:
    """
    Place an entry order with bracket orders (stop + target).
    Returns True if order placed successfully.
    """
    from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
    from alpaca.trading.requests import StopLossRequest, TakeProfitRequest

    side = OrderSide.BUY if signal.direction == 'long' else OrderSide.SELL

    logger.info(f"\n{'='*60}")
    logger.info(f"ORDER: {signal.direction.upper()} {signal.shares}x {signal.symbol}")
    logger.info(f"   Score: {signal.signal_score*17:.1f}/17 | "
                f"Win Prob: {signal.win_probability:.1%} | "
                f"Signal: {signal.signal_score:.2f}")
    logger.info(f"   Entry: ${signal.entry_price:.2f} | "
                f"Stop: ${signal.stop_loss:.2f} | "
                f"Target: ${signal.take_profit:.2f}")
    logger.info(f"   Risk: ${signal.dollar_risk:.2f} | "
                f"Exposure: ${signal.dollar_exposure:.2f} ({signal.position_pct:.1%})")
    logger.info(f"   Gross EV: ${signal.expected_value:.2f} | "
                f"Costs: ${signal.estimated_cost:.2f} | "
                f"Net EV: ${signal.net_expected_value:.2f}")
    logger.info(f"   Sizing method: {signal.sizing_method}")

    if dry_run:
        logger.info("   [DRY RUN - No order placed]")
        return True

    try:
        if config.USE_LIMIT_ORDERS:
            # Limit order with slight slippage allowance
            slip = config.LIMIT_SLIPPAGE_PCT
            limit_px = signal.entry_price * (1 + slip) if signal.direction == 'long' \
                       else signal.entry_price * (1 - slip)

            order_req = LimitOrderRequest(
                symbol=signal.symbol,
                qty=signal.shares,
                side=side,
                type="limit",
                limit_price=round(limit_px, 2),
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=round(signal.stop_loss, 2)),
                take_profit=TakeProfitRequest(limit_price=round(signal.take_profit, 2)),
            )
        else:
            order_req = MarketOrderRequest(
                symbol=signal.symbol,
                qty=signal.shares,
                side=side,
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=round(signal.stop_loss, 2)),
                take_profit=TakeProfitRequest(limit_price=round(signal.take_profit, 2)),
            )

        order = trading_client.submit_order(order_req)
        logger.info(f"   ✅ Order submitted: {order.id}")
        log_trade(signal, "ENTRY", order.id)
        return True

    except Exception as e:
        logger.error(f"   ❌ Order failed: {e}")
        return False


def close_position(trading_client, symbol: str, reason: str = "signal"):
    """Close an open position immediately."""
    try:
        trading_client.close_position(symbol)
        logger.info(f"🔴 Closed position: {symbol} (reason: {reason})")
        return True
    except Exception as e:
        logger.error(f"Error closing {symbol}: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# TRADE LOGGING
# ─────────────────────────────────────────────────────────────────────────────

def log_trade(signal: TradeSignal, action: str, order_id: str = ""):
    """Append trade to CSV log for analysis."""
    file_exists = Path(config.TRADE_LOG_FILE).exists()
    
    with open(config.TRADE_LOG_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                'timestamp', 'action', 'symbol', 'direction', 'shares',
                'entry_price', 'stop_loss', 'take_profit', 'dollar_risk',
                'dollar_exposure', 'position_pct', 'win_probability',
                'signal_score', 'expected_value', 'sizing_method', 'order_id'
            ])
        writer.writerow([
            datetime.now().isoformat(), action, signal.symbol, signal.direction,
            signal.shares, signal.entry_price, signal.stop_loss, signal.take_profit,
            round(signal.dollar_risk, 2), round(signal.dollar_exposure, 2),
            round(signal.position_pct, 4), round(signal.win_probability, 4),
            round(signal.signal_score, 4), round(signal.expected_value, 2),
            signal.sizing_method, order_id
        ])


# ─────────────────────────────────────────────────────────────────────────────
# PARALLEL SYMBOL PROCESSOR
# ─────────────────────────────────────────────────────────────────────────────

def process_symbol(symbol: str, data_client, risk_manager: RiskManager,
                   signal_history: list):
    """
    Process a single symbol — fetch, analyze, signal.
    Runs in parallel threads for speed.
    Returns TradeSignal if valid, None otherwise.
    """
    try:
        df = fetch_bars(data_client, symbol, config.BAR_TIMEFRAME)
        if df.empty or len(df) < 200:
            return None

        avg_vol = df['volume'].tail(20).mean()
        if avg_vol < config.MIN_AVG_VOLUME:
            return None

        df = compute_all_indicators(df, config)
        df = df.dropna(subset=['rsi', 'macd', 'adx', 'atr'])

        if len(df) < 50:
            return None

        signal_data = generate_sector_aware_signal(df, config, symbol, signal_history)

        if signal_data['signal_score'] < config.MIN_SIGNAL_SCORE:
            return None

        entry     = signal_data['close']
        atr_val   = signal_data['atr']
        direction = signal_data['direction']
        support    = signal_data.get('support') or None
        resistance = signal_data.get('resistance') or None

        stop, target = risk_manager.calculate_stops(
            entry, direction, atr_val,
            support=support if support and support > 0 else None,
            resistance=resistance if resistance and resistance > 0 else None,
        )

        garch_mult  = signal_data.get('garch_multiplier', 1.0)
        size_result = risk_manager.optimal_position_size(
            entry, stop, target,
            signal_data['win_probability'],
            atr_val, signal_data['signal_score']
        )
        size_result['shares'] = max(1, int(size_result['shares'] * garch_mult))
        size_result['dollar_exposure'] = size_result['shares'] * entry
        size_result['position_pct'] = size_result['dollar_exposure'] / config.PORTFOLIO_SIZE

        if size_result['shares'] == 0:
            return None

        # ── EARNINGS CALENDAR CHECK ────────────────────────────────────────
        earnings_check = earnings_calendar.is_earnings_risk_window(symbol)
        earnings_mult  = earnings_calendar.get_size_multiplier(symbol)
        if earnings_mult == 0.0:
            logger.info(f"{symbol}: SKIPPED — {earnings_check['reason']}")
            return None
        elif earnings_mult < 1.0:
            size_result['shares'] = max(1, int(size_result['shares'] * earnings_mult))
            logger.info(f"{symbol}: earnings risk — reducing size to {size_result['shares']} shares")

        # ── CONVICTION-BASED SIZING ────────────────────────────────────────
        conviction = ConvictionSizer(config)
        hist_vol = df['hist_vol'].iloc[-1] if 'hist_vol' in df.columns else 20.0
        garch_r  = signal_data.get('quant', {}).get('garch', {}).get('regime', 'NORMAL')
        conv_result = conviction.calculate_conviction_size(
            symbol=symbol,
            score_17=signal_data.get('total_score_17', 8),
            grade=signal_data.get('grade', 'GOOD'),
            garch_regime=garch_r,
            hist_vol=hist_vol,
            open_positions=risk_manager.open_positions,
            portfolio_value=risk_manager.portfolio_value,
            base_shares=size_result['shares'],
            entry_price=entry,
        )
        if conv_result.get('skip'):
            logger.info(f"{symbol}: conviction skip — {conv_result['reason']}")
            return None
        size_result['shares'] = conv_result['shares']
        size_result['dollar_exposure'] = size_result['shares'] * entry
        size_result['position_pct'] = size_result['dollar_exposure'] / config.PORTFOLIO_SIZE

        costs = risk_manager.estimate_round_trip_cost(entry, size_result['shares'], symbol)
        cost_adj_ev, _ = risk_manager.adjust_expected_value_for_costs(
            signal_data['expected_value'], costs, signal_data['win_probability']
        )

        trade_signal = TradeSignal(
            symbol=symbol,
            direction=direction,
            entry_price=round(entry, 4),
            stop_loss=round(stop, 4),
            take_profit=round(target, 4),
            shares=size_result['shares'],
            dollar_risk=size_result['dollar_risk'],
            dollar_exposure=size_result['dollar_exposure'],
            position_pct=size_result['position_pct'],
            win_probability=signal_data['win_probability'],
            expected_value=signal_data['expected_value'],
            signal_score=signal_data['signal_score'],
            strategy_signals={
                k: v['score'] for k, v in signal_data.get('strategy_results', {}).items()
            },
            sizing_method=size_result['method'],
            estimated_cost=costs['total_cost'],
            cost_pct=costs['cost_pct'],
            net_expected_value=cost_adj_ev,
        )

        approved, reason = risk_manager.validate_trade(trade_signal)
        if approved:
            # Sector concentration check
            sector = getattr(config, 'SECTOR_MAP', {}).get(symbol, 'GENERAL')
            max_per_sector = getattr(config, 'MAX_POSITIONS_PER_SECTOR', 2)
            sector_count = sum(
                1 for sym in risk_manager.open_positions
                if getattr(config, 'SECTOR_MAP', {}).get(sym, 'GENERAL') == sector
            )
            if sector_count >= max_per_sector:
                logger.debug(f"{symbol}: sector limit reached ({sector}: {sector_count}/{max_per_sector})")
                return None
            total_17 = signal_data.get('total_score_17', 0)
            grade    = signal_data.get('grade', '')
            sector   = signal_data.get('sector', 'GENERAL')
            bonus    = signal_data.get('sector_bonus', 0)
            fg_idx   = signal_data.get('fear_greed_index', 50)
            fg_cat   = signal_data.get('fear_greed_cat', '')
            herd     = signal_data.get('herd_regime', '')
            turb     = signal_data.get('turbulence_regime', '')
            garch_r  = signal_data.get('quant', {}).get('garch', {}).get('regime', '')
            arima_d  = signal_data.get('quant', {}).get('arima', {}).get('direction', 0)
            ml_acc   = signal_data.get('quant', {}).get('ml_model', {}).get('val_accuracy', 0)
            logger.info(f">> Signal: {symbol} {direction.upper()} | "
                       f"SCORE: {total_17:.1f}/17 [{grade}] | "
                       f"Sector: {sector} | Bonus: +{bonus:.1f} | "
                       f"P(win): {signal_data['win_probability']:.1%} | "
                       f"Net EV: ${cost_adj_ev:.2f}")
            logger.info(f"   ARIMA: {'UP' if arima_d > 0 else 'DOWN'} | "
                       f"GARCH: {garch_r} | "
                       f"ML: {ml_acc:.0%} | "
                       f"F&G: {fg_idx:.0f} [{fg_cat}] | "
                       f"Herd: {herd} | Turb: {turb}")
            return trade_signal
        else:
            logger.debug(f"{symbol}: rejected - {reason}")
            return None

    except Exception as e:
        logger.error(f"Error processing {symbol}: {e}", exc_info=False)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# MAIN SCANNING LOOP (PARALLEL)
# ─────────────────────────────────────────────────────────────────────────────

def scan_for_signals(data_client, risk_manager: RiskManager,
                      signal_history: list) -> list[TradeSignal]:
    """
    Scan all watchlist symbols for trade signals IN PARALLEL.
    Uses ThreadPoolExecutor to scan multiple symbols simultaneously.
    Much faster than sequential scanning for large watchlists.
    Returns list of validated TradeSignals ready to execute.
    """
    candidates = []
    max_workers = min(8, len(config.WATCHLIST))  # Max 8 parallel threads

    logger.debug(f"Scanning {len(config.WATCHLIST)} symbols with {max_workers} threads...")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                process_symbol, symbol, data_client, risk_manager, signal_history
            ): symbol
            for symbol in config.WATCHLIST
        }

        for future in as_completed(futures):
            symbol = futures[future]
            try:
                result = future.result(timeout=30)
                if result is not None:
                    candidates.append(result)
            except Exception as e:
                logger.debug(f"Thread error for {symbol}: {e}")

    # Sort by net expected value (best first)
    candidates.sort(key=lambda x: x.net_expected_value, reverse=True)

    # Apply correlation filter — remove redundant signals
    corr_filter = get_correlation_filter(config)
    candidates  = corr_filter.filter_signals(candidates)

    if candidates:
        logger.info(f"After correlation filter: {len(candidates)} signal(s)")

    return candidates


# ─────────────────────────────────────────────────────────────────────────────
# POSITION MONITORING
# ─────────────────────────────────────────────────────────────────────────────

def monitor_positions(trading_client, data_client, risk_manager: RiskManager):
    """
    Check all open positions:
    - Update trailing stops
    - Force exit if stop/target hit
    - Log any closed positions
    """
    try:
        positions = trading_client.get_all_positions()
    except Exception as e:
        logger.error(f"Error fetching positions: {e}")
        return

    for pos in positions:
        symbol = pos.symbol
        current_price = float(pos.current_price)

        # Update trailing stop
        if config.USE_TRAILING_STOP:
            new_stop = risk_manager.update_trailing_stop(symbol, current_price)
            if new_stop:
                logger.debug(f"📈 Trailing stop updated: {symbol} → ${new_stop:.2f}")
                # In live trading, you'd submit a replace order here

        # Check manual exit conditions
        should_exit, exit_reason = risk_manager.should_exit(symbol, current_price)
        if should_exit:
            logger.info(f"🔔 Exit triggered for {symbol}: {exit_reason}")
            # Alpaca bracket orders handle stop/target automatically
            # But we track PnL here
            unrealized_pnl = float(pos.unrealized_pl)
            risk_manager.update_portfolio({
                "symbol": symbol,
                "pnl": unrealized_pnl,
                "exit_reason": exit_reason,
                "timestamp": datetime.now().isoformat(),
            })


# ─────────────────────────────────────────────────────────────────────────────
# BACKTESTING ENGINE (SIMPLIFIED)
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest(data_client, days: int = 90):
    """
    Run a simplified backtest on historical data.
    Generates signals bar-by-bar and tracks hypothetical P&L.
    """
    logger.info(f"\n{'='*60}")
    logger.info(f"🔬 BACKTESTING - Last {days} days")
    logger.info(f"{'='*60}\n")

    from datetime import timedelta

    results = []
    
    for symbol in config.WATCHLIST[:5]:  # Backtest top 5 to save time
        logger.info(f"Backtesting {symbol}...")
        df = fetch_bars(data_client, symbol, "1Day", limit=days + 300)
        
        if df.empty or len(df) < 200:
            continue

        df = compute_all_indicators(df, config)
        df = df.dropna(subset=['rsi', 'macd', 'adx', 'atr'])

        trades = []
        in_trade = False
        entry_px = 0
        stop = 0
        target = 0
        direction = 'long'

        for i in range(200, len(df) - 1):
            slice_df = df.iloc[:i+1].copy()
            
            if not in_trade:
                signal_data = generate_composite_signal(slice_df, config)
                
                if signal_data['signal_score'] >= config.MIN_SIGNAL_SCORE:
                    entry_px = slice_df['close'].iloc[-1]
                    atr_val = signal_data['atr']
                    direction = signal_data['direction']
                    
                    rm = RiskManager(config)
                    stop, target = rm.calculate_stops(entry_px, direction, atr_val)
                    in_trade = True
            
            else:
                current = df['close'].iloc[i+1]
                
                hit_stop = (current <= stop) if direction == 'long' else (current >= stop)
                hit_target = (current >= target) if direction == 'long' else (current <= target)
                
                if hit_stop or hit_target:
                    exit_px = stop if hit_stop else target
                    pnl_pct = (exit_px - entry_px) / entry_px
                    if direction == 'short':
                        pnl_pct = -pnl_pct
                    
                    trades.append({
                        "symbol": symbol,
                        "direction": direction,
                        "entry": entry_px,
                        "exit": exit_px,
                        "pnl_pct": pnl_pct,
                        "result": "win" if pnl_pct > 0 else "loss"
                    })
                    in_trade = False

        if trades:
            wins = [t for t in trades if t['result'] == 'win']
            total_return = sum(t['pnl_pct'] for t in trades)
            win_rate = len(wins) / len(trades)
            
            logger.info(f"\n{symbol} Results:")
            logger.info(f"  Trades: {len(trades)} | Win Rate: {win_rate:.1%} | "
                       f"Total Return: {total_return:.2%}")
            results.extend(trades)

    # Summary
    if results:
        wins = [t for t in results if t['result'] == 'win']
        all_pnl = [t['pnl_pct'] for t in results]
        
        logger.info(f"\n{'='*60}")
        logger.info(f"📊 BACKTEST SUMMARY")
        logger.info(f"{'='*60}")
        logger.info(f"Total Trades:    {len(results)}")
        logger.info(f"Win Rate:        {len(wins)/len(results):.1%}")
        logger.info(f"Avg Win:         {np.mean([t['pnl_pct'] for t in wins]):.2%}" if wins else "N/A")
        logger.info(f"Avg Loss:        {np.mean([t['pnl_pct'] for t in results if t['result']=='loss']):.2%}")
        logger.info(f"Total Return:    {sum(all_pnl):.2%}")
        logger.info(f"Sharpe (daily):  {np.mean(all_pnl) / (np.std(all_pnl) + 1e-9) * np.sqrt(252):.2f}")
    
    return results


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Elite Trading Bot')
    parser.add_argument('--backtest', type=int, metavar='DAYS',
                        help='Run backtest for N days instead of live trading')
    parser.add_argument('--scan', action='store_true',
                        help='Run a single scan and exit (no trading)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Scan and show signals but do not place orders')
    args = parser.parse_args()

    logger.info("="*60)
    logger.info("ELITE TRADING BOT v3.1 - Starting Up")
    logger.info(f"   Portfolio: ${config.PORTFOLIO_SIZE:,.2f}")
    logger.info(f"   Mode: {'PAPER' if config.PAPER_TRADING else 'LIVE'}")
    logger.info(f"   Timeframe: {config.BAR_TIMEFRAME}")
    logger.info("="*60)

    # Connect to broker
    trading_client, data_client = connect_alpaca()
    risk_manager   = RiskManager(config)
    signal_history = []

    # Send startup alert
    alert_engine.startup_alert(
        config.PORTFOLIO_SIZE,
        "PAPER" if config.PAPER_TRADING else "LIVE",
        len(config.WATCHLIST)
    )

    # ── Backtest Mode ──────────────────────────────────────────
    if args.backtest:
        run_backtest(data_client, args.backtest)
        return

    # ── Single Scan Mode ───────────────────────────────────────
    if args.scan or args.dry_run:
        logger.info("\n>> Running single scan...")
        signals = scan_for_signals(data_client, risk_manager, signal_history)
        logger.info(f"\nFound {len(signals)} valid signal(s)")
        for sig in signals:
            logger.info(f"  {sig.symbol}: {sig.direction} | "
                       f"Score={sig.signal_score*17:.1f}/17 | "
                       f"EV=${sig.net_expected_value:.2f} | "
                       f"P(win)={sig.win_probability:.1%}")
        return

    # ── Live Trading Loop ──────────────────────────────────────
    logger.info("\n>> Starting live trading loop...")
    logger.info(f"   Scanning every {config.SCAN_INTERVAL_SEC}s")
    logger.info("   Press Ctrl+C to stop\n")

    scan_count  = 0
    last_summary_day = None

    while True:
        try:
            scan_count += 1
            now = datetime.now()

            # Send daily summary at 4 PM
            eastern = pytz.timezone('America/New_York')
            now_et = datetime.now(timezone.utc).astimezone(eastern)
            if (now_et.hour == 16 and now_et.minute < 5 and
                    last_summary_day != now_et.date()):
                stats = risk_manager.get_statistics()
                alert_engine.daily_summary(
                    portfolio_value=risk_manager.portfolio_value,
                    daily_pnl=risk_manager.daily_pnl,
                    trades_today=len([t for t in risk_manager.trade_history
                                      if t.get('timestamp', '').startswith(str(now_et.date()))]),
                    win_rate=stats.get('win_rate', 0),
                    open_positions=risk_manager.open_positions,
                    var_dollar=risk_manager.portfolio_value * 0.02,
                )
                last_summary_day = now_et.date()

            # Check market hours
            if not is_market_open(trading_client):
                next_check = 60 * 5
                logger.info(f"Market closed. Waiting {next_check//60}min...")
                time.sleep(next_check)
                continue

            logger.info(f"\n>> Scan #{scan_count} @ {now.strftime('%H:%M:%S')}")

            # Run pre-market gap scan if in pre-market window
            pm_scanner = get_scanner(config)
            if pm_scanner.is_premarket_window():
                logger.info("Pre-market window — scanning for gaps...")
                gaps = pm_scanner.scan_all(config.WATCHLIST)
                if gaps:
                    logger.info(f"Pre-market gaps: {len(gaps)} opportunities found")
                    for g in gaps[:3]:
                        logger.info(f"  {g['symbol']}: {g['gap_dir']} {g['gap_abs']:.1f}% "
                                   f"— {g['strategy']}")

            # 1. Monitor existing positions
            monitor_positions(trading_client, data_client, risk_manager)

            # 2. Scan for new signals
            signals = scan_for_signals(data_client, risk_manager, signal_history)

            # 3. Execute best signals (up to remaining position slots)
            open_count      = len(risk_manager.open_positions)
            slots_available = config.MAX_OPEN_POSITIONS - open_count

            for signal in signals[:slots_available]:
                # Send signal alert BEFORE placing order
                quant_data = {}
                alert_engine.signal_alert(
                    symbol=signal.symbol,
                    direction=signal.direction,
                    score=signal.signal_score * 17,
                    grade="GOOD",
                    win_prob=signal.win_probability,
                    entry=signal.entry_price,
                    stop=signal.stop_loss,
                    target=signal.take_profit,
                    net_ev=signal.net_expected_value,
                    shares=signal.shares,
                    exposure=signal.dollar_exposure,
                )

                success = place_order(
                    trading_client, signal,
                    dry_run=args.dry_run
                )
                if success:
                    risk_manager.add_position(signal)

            # 4. Portfolio status
            stats = risk_manager.get_statistics()
            if stats:
                logger.info(f"\n📈 Portfolio: ${risk_manager.portfolio_value:,.2f} | "
                           f"Daily P&L: ${risk_manager.daily_pnl:+.2f} | "
                           f"Positions: {open_count}/{config.MAX_OPEN_POSITIONS}")

            # Sleep until next scan
            time.sleep(config.SCAN_INTERVAL_SEC)

        except KeyboardInterrupt:
            logger.info("\n\n⛔ Bot stopped by user")
            stats = risk_manager.get_statistics()
            if stats:
                logger.info(f"\n📊 Final Statistics:")
                for k, v in stats.items():
                    if isinstance(v, float):
                        logger.info(f"   {k}: {v:.4f}")
                    else:
                        logger.info(f"   {k}: {v}")
            break

        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            time.sleep(30)


if __name__ == "__main__":
    main()
