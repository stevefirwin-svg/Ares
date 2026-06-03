"""
=============================================================================
TECHNICAL INDICATORS ENGINE
=============================================================================
All technical analysis calculations. Each function takes a pandas DataFrame
with OHLCV data and returns the indicator values.
=============================================================================
"""

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# MOVING AVERAGES
# ─────────────────────────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    return series.rolling(window=period).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Volume Weighted Average Price (intraday reset)."""
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    cumulative_tp_vol = (typical_price * df['volume']).cumsum()
    cumulative_vol = df['volume'].cumsum()
    return cumulative_tp_vol / cumulative_vol


# ─────────────────────────────────────────────────────────────────────────────
# MOMENTUM INDICATORS
# ─────────────────────────────────────────────────────────────────────────────

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (0-100)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD Line, Signal Line, and Histogram."""
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3):
    """Stochastic Oscillator %K and %D."""
    low_min = df['low'].rolling(k_period).min()
    high_max = df['high'].rolling(k_period).max()
    k = 100 * (df['close'] - low_min) / (high_max - low_min).replace(0, np.nan)
    d = k.rolling(d_period).mean()
    return k, d


def rate_of_change(series: pd.Series, period: int = 10) -> pd.Series:
    """Rate of Change (momentum %)."""
    return series.pct_change(period) * 100


# ─────────────────────────────────────────────────────────────────────────────
# TREND INDICATORS
# ─────────────────────────────────────────────────────────────────────────────

def adx(df: pd.DataFrame, period: int = 14):
    """
    Average Directional Index + DI lines.
    ADX > 25 = strong trend. ADX < 20 = ranging/weak trend.
    Returns: adx, +DI, -DI
    """
    high = df['high']
    low = df['low']
    close = df['close']

    # True Range
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Directional Movement
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    # Smoothed
    atr = true_range.ewm(alpha=1/period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr

    # ADX
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val = dx.ewm(alpha=1/period, adjust=False).mean()

    return adx_val, plus_di, minus_di


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0):
    """
    Supertrend indicator. Returns direction (1=bullish, -1=bearish) and line.
    """
    atr_val = atr(df, period)
    hl2 = (df['high'] + df['low']) / 2

    upper_band = hl2 + multiplier * atr_val
    lower_band = hl2 - multiplier * atr_val

    supertrend_dir = pd.Series(index=df.index, dtype=float)
    supertrend_line = pd.Series(index=df.index, dtype=float)

    for i in range(1, len(df)):
        # Upper band
        if upper_band.iloc[i] < upper_band.iloc[i-1] or df['close'].iloc[i-1] > upper_band.iloc[i-1]:
            upper_band.iloc[i] = upper_band.iloc[i]
        else:
            upper_band.iloc[i] = upper_band.iloc[i-1]

        # Lower band
        if lower_band.iloc[i] > lower_band.iloc[i-1] or df['close'].iloc[i-1] < lower_band.iloc[i-1]:
            lower_band.iloc[i] = lower_band.iloc[i]
        else:
            lower_band.iloc[i] = lower_band.iloc[i-1]

        # Direction
        if df['close'].iloc[i] > upper_band.iloc[i-1]:
            supertrend_dir.iloc[i] = 1
        elif df['close'].iloc[i] < lower_band.iloc[i-1]:
            supertrend_dir.iloc[i] = -1
        else:
            supertrend_dir.iloc[i] = supertrend_dir.iloc[i-1]

        supertrend_line.iloc[i] = lower_band.iloc[i] if supertrend_dir.iloc[i] == 1 else upper_band.iloc[i]

    return supertrend_dir, supertrend_line


# ─────────────────────────────────────────────────────────────────────────────
# VOLATILITY INDICATORS
# ─────────────────────────────────────────────────────────────────────────────

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range - measures volatility."""
    high = df['high']
    low = df['low']
    close = df['close']
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()


def bollinger_bands(series: pd.Series, period: int = 20, std: float = 2.0):
    """
    Bollinger Bands: upper, middle (SMA), lower bands + %B + bandwidth.
    %B: 0=at lower band, 0.5=at middle, 1=at upper band
    """
    middle = sma(series, period)
    std_dev = series.rolling(period).std()
    upper = middle + std * std_dev
    lower = middle - std * std_dev
    pct_b = (series - lower) / (upper - lower).replace(0, np.nan)
    bandwidth = (upper - lower) / middle * 100
    return upper, middle, lower, pct_b, bandwidth


def historical_volatility(series: pd.Series, period: int = 20) -> pd.Series:
    """Annualized historical volatility from log returns."""
    log_returns = np.log(series / series.shift(1))
    return log_returns.rolling(period).std() * np.sqrt(252) * 100


# ─────────────────────────────────────────────────────────────────────────────
# VOLUME INDICATORS
# ─────────────────────────────────────────────────────────────────────────────

def obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume - cumulative volume pressure."""
    direction = np.sign(df['close'].diff())
    return (direction * df['volume']).cumsum()


def volume_ratio(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Current volume vs average volume ratio."""
    avg_vol = sma(df['volume'], period)
    return df['volume'] / avg_vol.replace(0, np.nan)


def money_flow_index(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Money Flow Index - volume-weighted RSI."""
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    money_flow = typical_price * df['volume']

    pos_flow = money_flow.where(typical_price > typical_price.shift(1), 0)
    neg_flow = money_flow.where(typical_price < typical_price.shift(1), 0)

    pos_mf = pos_flow.rolling(period).sum()
    neg_mf = neg_flow.rolling(period).sum()

    mfr = pos_mf / neg_mf.replace(0, np.nan)
    return 100 - (100 / (1 + mfr))


# ─────────────────────────────────────────────────────────────────────────────
# STATISTICAL INDICATORS
# ─────────────────────────────────────────────────────────────────────────────

def zscore(series: pd.Series, period: int = 20) -> pd.Series:
    """Rolling Z-score: how many standard deviations from the mean."""
    mean = series.rolling(period).mean()
    std = series.rolling(period).std()
    return (series - mean) / std.replace(0, np.nan)


def linear_regression_slope(series: pd.Series, period: int = 20) -> pd.Series:
    """Rolling linear regression slope (normalized)."""
    slopes = []
    for i in range(len(series)):
        if i < period:
            slopes.append(np.nan)
        else:
            y = series.iloc[i-period:i].values
            x = np.arange(period)
            slope = np.polyfit(x, y, 1)[0]
            # Normalize by price level
            slopes.append(slope / series.iloc[i] * 100)
    return pd.Series(slopes, index=series.index)


def hurst_exponent(series: pd.Series, max_lag: int = 20) -> float:
    """
    Hurst Exponent: 
    H > 0.5 = trending (momentum works)
    H = 0.5 = random walk
    H < 0.5 = mean-reverting
    """
    lags = range(2, max_lag)
    tau = [np.std(np.subtract(series[lag:].values, series[:-lag].values)) for lag in lags]
    valid = [(lag, t) for lag, t in zip(lags, tau) if t > 0]
    if len(valid) < 2:
        return 0.5
    x = np.log([v[0] for v in valid])
    y = np.log([v[1] for v in valid])
    return np.polyfit(x, y, 1)[0]


def compute_all_indicators(df: pd.DataFrame, config) -> pd.DataFrame:
    """
    Compute ALL indicators at once and attach to DataFrame.
    Returns enriched DataFrame.
    """
    c = df['close']
    
    # Moving Averages
    df['ema_fast']   = ema(c, config.EMA_FAST)
    df['ema_medium'] = ema(c, config.EMA_MEDIUM)
    df['ema_slow']   = ema(c, config.EMA_SLOW)
    df['ema_trend']  = ema(c, config.EMA_TREND)
    df['vwap']       = vwap(df)

    # Momentum
    df['rsi']  = rsi(c, config.RSI_PERIOD)
    df['roc']  = rate_of_change(c, 10)
    df['macd'], df['macd_signal'], df['macd_hist'] = macd(
        c, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL
    )
    df['stoch_k'], df['stoch_d'] = stochastic(df)
    df['mfi'] = money_flow_index(df)

    # Trend
    df['adx'], df['plus_di'], df['minus_di'] = adx(df, config.ADX_PERIOD)

    # Volatility
    df['atr']  = atr(df, config.ATR_PERIOD)
    df['atr_pct'] = df['atr'] / c * 100
    df['bb_upper'], df['bb_mid'], df['bb_lower'], df['bb_pct'], df['bb_bw'] = bollinger_bands(
        c, config.BB_PERIOD, config.BB_STD
    )
    df['hist_vol'] = historical_volatility(c)

    # Volume
    df['obv'] = obv(df)
    df['vol_ratio'] = volume_ratio(df, config.VOLUME_SMA_PERIOD)
    df['vol_surge'] = df['vol_ratio'] > config.VOLUME_SURGE_MULT

    # Statistical
    df['zscore'] = zscore(c)
    df['zscore_vol'] = zscore(df['volume'])

    return df
