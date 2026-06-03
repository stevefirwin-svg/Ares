"""
=============================================================================
ELITE TRADING BOT - MAIN ENGINE
=============================================================================
Orchestrates everything: data fetching, signal generation, order execution,
and position management. This is the file you RUN to start the bot.

Usage:
    python main.py                    # Live trading loop
    python main.py --backtest 90      # Backtest last 90 days
    python main.py --scan             # Single scan, no trading
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

import pandas as pd
import numpy as np

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from utils.indicators import compute_all_indicators
from utils.risk_manager import RiskManager, TradeSignal
from strategies.signal_engine import generate_composite_signal


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
    """Connect to Alpaca broker. Returns (trading_client, data_client)."""
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        paper = config.PAPER_TRADING
        trading_client = TradingClient(
            config.API_KEY, config.API_SECRET, paper=paper
        )
        data_client = StockHistoricalDataClient(config.API_KEY, config.API_SECRET)

        account = trading_client.get_account()
        logger.info(f"✅ Connected to Alpaca ({'PAPER' if paper else 'LIVE'} trading)")
        logger.info(f"   Account: ${float(account.equity):,.2f} equity | "
                    f"${float(account.buying_power):,.2f} buying power")

        return trading_client, data_client

    except ImportError:
        logger.error("❌ alpaca-py not installed. Run: pip install alpaca-py --break-system-packages")
        sys.exit(1)
    except Exception as e:
        logger.error(f"❌ Could not connect to Alpaca: {e}")
        logger.error("   Check your API keys in config.py")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────────────────────────────────────

def fetch_bars(data_client, symbol: str, timeframe_str: str = "5Min", 
               limit: int = 300) -> pd.DataFrame:
    """
    Fetch OHLCV bars from Alpaca.
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
        start = end - timedelta(days=30)  # Fetch 30 days, we'll use what we need

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
    """Check if market is currently open for trading."""
    try:
        clock = trading_client.get_clock()
        if not clock.is_open:
            return False

        now = datetime.now()
        # Check our custom trading window
        after_open = (now.hour > config.MARKET_OPEN_HOUR or 
                      (now.hour == config.MARKET_OPEN_HOUR and 
                       now.minute >= config.MARKET_OPEN_MIN))
        before_close = (now.hour < config.MARKET_CLOSE_HOUR or 
                        (now.hour == config.MARKET_CLOSE_HOUR and 
                         now.minute <= config.MARKET_CLOSE_MIN))
        return after_open and before_close
    except:
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
# MAIN SCANNING LOOP
# ─────────────────────────────────────────────────────────────────────────────

def scan_for_signals(data_client, risk_manager: RiskManager, 
                      signal_history: list) -> list[TradeSignal]:
    """
    Scan all watchlist symbols for trade signals.
    Returns list of validated TradeSignals ready to execute.
    """
    candidates = []

    for symbol in config.WATCHLIST:
        try:
            # Fetch data
            df = fetch_bars(data_client, symbol, config.BAR_TIMEFRAME)
            if df.empty or len(df) < 200:
                continue

            # Volume filter
            avg_vol = df['volume'].tail(20).mean()
            if avg_vol < config.MIN_AVG_VOLUME:
                continue

            # Compute all indicators
            df = compute_all_indicators(df, config)
            df = df.dropna(subset=['rsi', 'macd', 'adx', 'atr'])
            
            if len(df) < 50:
                continue

            # Generate composite signal
            signal_data = generate_composite_signal(df, config, signal_history)

            # Quick threshold check
            if signal_data['signal_score'] < config.MIN_SIGNAL_SCORE:
                logger.debug(f"{symbol}: score {signal_data['signal_score']:.2f} < threshold")
                continue

            # Entry price and stops
            entry = signal_data['close']
            atr_val = signal_data['atr']
            direction = signal_data['direction']

            # Calculate stops
            from utils.risk_manager import RiskManager as RM
            stop, target = risk_manager.calculate_stops(
                entry, direction, atr_val
            )

            # Size the position
            size_result = risk_manager.optimal_position_size(
                entry, stop, target,
                signal_data['win_probability'],
                atr_val,
                signal_data['signal_score']
            )

            if size_result['shares'] == 0:
                continue

            # Build TradeSignal object
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
                    k: v['score'] for k, v in signal_data['strategy_results'].items()
                },
                sizing_method=size_result['method'],
                estimated_cost=costs['total_cost'],
                cost_pct=costs['cost_pct'],
                net_expected_value=cost_adj_ev,
            )

            # Validate through risk manager
            approved, reason = risk_manager.validate_trade(trade_signal)
            if approved:
                candidates.append(trade_signal)
                total_17 = signal_data.get('total_score_17', 0)
                grade    = signal_data.get('grade', '')
                logger.info(f">> Signal: {symbol} {direction.upper()} | "
                           f"SCORE: {total_17:.1f}/17 [{grade}] | "
                           f"P(win): {signal_data['win_probability']:.1%} | "
                           f"Gross EV: ${signal_data['expected_value']:.2f} | "
                           f"Costs: ${costs['total_cost']:.2f} | "
                           f"Net EV: ${cost_adj_ev:.2f}")
            else:
                logger.debug(f"{symbol}: rejected - {reason}")

        except Exception as e:
            logger.error(f"Error scanning {symbol}: {e}", exc_info=False)

    # Sort by expected value (best first)
    candidates.sort(key=lambda x: x.expected_value, reverse=True)
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
    logger.info("🤖 ELITE TRADING BOT - Starting Up")
    logger.info(f"   Portfolio: ${config.PORTFOLIO_SIZE:,.2f}")
    logger.info(f"   Mode: {'PAPER' if config.PAPER_TRADING else '🔴 LIVE'}")
    logger.info(f"   Timeframe: {config.BAR_TIMEFRAME}")
    logger.info("="*60)

    # Connect to broker
    trading_client, data_client = connect_alpaca()
    risk_manager = RiskManager(config)
    signal_history = []

    # ── Backtest Mode ──────────────────────────────────────────
    if args.backtest:
        run_backtest(data_client, args.backtest)
        return

    # ── Single Scan Mode ───────────────────────────────────────
    if args.scan or args.dry_run:
        logger.info("\n🔍 Running single scan...")
        signals = scan_for_signals(data_client, risk_manager, signal_history)
        logger.info(f"\nFound {len(signals)} valid signal(s)")
        for sig in signals:
            logger.info(f"  {sig.symbol}: {sig.direction} | "
                       f"Score={sig.signal_score:.2f} | "
                       f"EV=${sig.expected_value:.2f} | "
                       f"P(win)={sig.win_probability:.1%}")
        return

    # ── Live Trading Loop ──────────────────────────────────────
    logger.info("\n🚀 Starting live trading loop...")
    logger.info(f"   Scanning every {config.SCAN_INTERVAL_SEC}s")
    logger.info("   Press Ctrl+C to stop\n")

    scan_count = 0
    while True:
        try:
            scan_count += 1
            now = datetime.now()

            # Check market hours
            if not is_market_open(trading_client):
                next_check = 60 * 5
                logger.info(f"⏸️  Market closed. Waiting {next_check//60}min...")
                time.sleep(next_check)
                continue

            logger.info(f"\n🔄 Scan #{scan_count} @ {now.strftime('%H:%M:%S')}")

            # 1. Monitor existing positions
            monitor_positions(trading_client, data_client, risk_manager)

            # 2. Scan for new signals
            signals = scan_for_signals(data_client, risk_manager, signal_history)

            # 3. Execute best signals (up to remaining position slots)
            open_count = len(risk_manager.open_positions)
            slots_available = config.MAX_OPEN_POSITIONS - open_count

            for signal in signals[:slots_available]:
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
