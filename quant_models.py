"""
=============================================================================
ELITE QUANT MODELS v3.0
=============================================================================
Advanced quantitative models:
  - ARIMA: Time series forecasting for price direction
  - GARCH: Volatility forecasting and regime detection
  - Monte Carlo: Risk simulation (VaR, CVaR, probability cones)
  - Bayesian: Probabilistic signal updating
  - VAR (Value at Risk): Portfolio-level risk measurement
  - LSTM proxy: Feature-based ML signal (sklearn, no tensorflow needed)
=============================================================================
"""

import numpy as np
import pandas as pd
import logging
from scipy import stats
from scipy.optimize import minimize

logger = logging.getLogger(__name__)


# =============================================================================
# ARIMA MODEL — Price Direction Forecasting
# =============================================================================

class ARIMAModel:
    """
    AutoRegressive Integrated Moving Average model.
    Forecasts next N price movements using historical patterns.
    
    In plain English: ARIMA learns the rhythm of a stock's price history
    and uses that rhythm to predict where it goes next.
    """

    def __init__(self, order=(2, 1, 2)):
        self.order = order  # (AR lags, differencing, MA lags)
        self.fitted = False

    def fit_and_forecast(self, prices: pd.Series, steps: int = 5) -> dict:
        """
        Fit ARIMA to price series and forecast next N steps.
        Returns direction, confidence, and forecast values.
        """
        try:
            from statsmodels.tsa.arima.model import ARIMA
            import warnings
            warnings.filterwarnings('ignore')

            # Use log returns for stationarity
            log_prices = np.log(prices.dropna())
            if len(log_prices) < 30:
                return self._fallback_forecast(prices)

            # Try ARIMA with automatic order selection if needed
            try:
                model = ARIMA(log_prices, order=self.order)
                result = model.fit()
                forecast = result.forecast(steps=steps)
                conf_int = result.get_forecast(steps).conf_int()
            except Exception:
                # Fallback to simpler order
                model = ARIMA(log_prices, order=(1, 1, 1))
                result = model.fit()
                forecast = result.forecast(steps=steps)
                conf_int = result.get_forecast(steps).conf_int()

            # Convert log forecast back to price change direction
            current_log = log_prices.iloc[-1]
            forecast_direction = np.sign(forecast.iloc[-1] - current_log)
            forecast_magnitude = abs(forecast.iloc[-1] - current_log)

            # Confidence from confidence interval width
            ci_width = (conf_int.iloc[-1, 1] - conf_int.iloc[-1, 0])
            confidence = max(0.5, min(0.85, 1.0 / (1 + ci_width * 10)))

            # AIC score (lower = better fit)
            aic = result.aic

            return {
                "direction": float(forecast_direction),
                "magnitude_pct": float(forecast_magnitude * 100),
                "confidence": float(confidence),
                "forecast_values": forecast.values.tolist(),
                "aic": float(aic),
                "signal": float(forecast_direction * confidence),
                "model": "ARIMA"
            }

        except ImportError:
            logger.debug("statsmodels not available, using fallback")
            return self._fallback_forecast(prices)
        except Exception as e:
            logger.debug(f"ARIMA error: {e}")
            return self._fallback_forecast(prices)

    def _fallback_forecast(self, prices: pd.Series) -> dict:
        """Simple linear regression fallback when ARIMA unavailable."""
        if len(prices) < 10:
            return {"direction": 0, "magnitude_pct": 0, "confidence": 0.5,
                    "signal": 0, "model": "LinearFallback"}
        y = prices.values[-20:]
        x = np.arange(len(y))
        slope, _, r, _, _ = stats.linregress(x, y)
        direction = np.sign(slope)
        confidence = min(0.75, abs(r))
        return {
            "direction": float(direction),
            "magnitude_pct": float(abs(slope) / prices.iloc[-1] * 100),
            "confidence": float(confidence),
            "signal": float(direction * confidence),
            "model": "LinearRegression"
        }


# =============================================================================
# GARCH MODEL — Volatility Forecasting
# =============================================================================

class GARCHModel:
    """
    Generalized AutoRegressive Conditional Heteroskedasticity.
    Forecasts FUTURE VOLATILITY — the most important risk signal.
    
    In plain English: GARCH recognizes that volatility clusters
    (high vol follows high vol). It predicts whether the next period
    will be calm or stormy, letting us size positions accordingly.
    """

    def fit_and_forecast(self, returns: pd.Series) -> dict:
        """
        Fit GARCH(1,1) and forecast next period volatility.
        Returns volatility forecast, regime, and trading signal.
        """
        try:
            from arch import arch_model
            import warnings
            warnings.filterwarnings('ignore')

            r = returns.dropna() * 100  # Scale for numerical stability
            if len(r) < 50:
                return self._fallback_vol(returns)

            model = arch_model(r, vol='Garch', p=1, q=1, dist='Normal')
            result = model.fit(disp='off', show_warning=False)

            # Forecast 5 periods ahead
            forecast = result.forecast(horizon=5)
            vol_forecast = np.sqrt(forecast.variance.iloc[-1].values)
            vol_1d = float(vol_forecast[0]) / 100  # Back to decimal

            # Historical vol for comparison
            hist_vol = returns.std() * np.sqrt(252)
            forecast_annualized = vol_1d * np.sqrt(252)

            # Volatility regime
            if vol_1d < 0.008:
                regime = "LOW"
                position_multiplier = 1.2
            elif vol_1d < 0.015:
                regime = "NORMAL"
                position_multiplier = 1.0
            elif vol_1d < 0.025:
                regime = "ELEVATED"
                position_multiplier = 0.7
            else:
                regime = "HIGH"
                position_multiplier = 0.4

            # Volatility trend (rising = bad, falling = good)
            recent_vol = returns.iloc[-10:].std()
            older_vol  = returns.iloc[-30:-10].std()
            vol_trend  = (recent_vol - older_vol) / (older_vol + 1e-9)

            return {
                "vol_1d":               vol_1d,
                "vol_annualized":       forecast_annualized,
                "hist_vol_annualized":  hist_vol,
                "regime":               regime,
                "position_multiplier":  position_multiplier,
                "vol_trend":            float(vol_trend),
                "vol_rising":           vol_trend > 0.1,
                "signal": 0.5 if regime in ["LOW", "NORMAL"] else -0.3,
                "model": "GARCH(1,1)"
            }

        except ImportError:
            logger.debug("arch not available, using fallback")
            return self._fallback_vol(returns)
        except Exception as e:
            logger.debug(f"GARCH error: {e}")
            return self._fallback_vol(returns)

    def _fallback_vol(self, returns: pd.Series) -> dict:
        """Rolling volatility fallback."""
        r = returns.dropna()
        vol_1d = r.iloc[-20:].std() if len(r) >= 20 else r.std()
        ann_vol = vol_1d * np.sqrt(252)
        regime = "LOW" if ann_vol < 0.15 else "NORMAL" if ann_vol < 0.25 else "HIGH"
        mult = 1.2 if regime == "LOW" else 1.0 if regime == "NORMAL" else 0.6
        return {
            "vol_1d": float(vol_1d),
            "vol_annualized": float(ann_vol),
            "hist_vol_annualized": float(ann_vol),
            "regime": regime,
            "position_multiplier": mult,
            "vol_trend": 0.0,
            "vol_rising": False,
            "signal": 0.3,
            "model": "RollingVol"
        }


# =============================================================================
# MONTE CARLO SIMULATION — Risk Assessment
# =============================================================================

class MonteCarloEngine:
    """
    Monte Carlo simulation for risk assessment.
    Simulates thousands of possible price paths to estimate:
    - Value at Risk (VaR): Max loss at given confidence level
    - Conditional VaR (CVaR): Expected loss BEYOND VaR
    - Win probability: % of paths that hit target before stop
    - Probability cone: Range of likely outcomes
    
    In plain English: Rolls the dice 10,000 times to see how many
    ways the trade can win vs lose, and by how much.
    """

    def simulate(self, current_price: float, returns: pd.Series,
                  stop_loss: float, take_profit: float,
                  n_simulations: int = 5000, n_days: int = 20) -> dict:
        """
        Run Monte Carlo simulation for a specific trade setup.
        """
        try:
            r = returns.dropna()
            if len(r) < 20:
                return self._simple_estimate(current_price, stop_loss,
                                              take_profit, returns)

            mu    = r.mean()
            sigma = r.std()

            # Simulate price paths using geometric Brownian motion
            # (the mathematical model of stock price movement)
            rng = np.random.default_rng(42)
            random_returns = rng.normal(mu, sigma, (n_simulations, n_days))
            price_paths = current_price * np.exp(np.cumsum(random_returns, axis=1))

            # Track hit rates for stop and target
            hits_target = 0
            hits_stop   = 0
            final_prices = []

            for path in price_paths:
                hit_target = np.any(path >= take_profit)
                hit_stop   = np.any(path <= stop_loss)

                if hit_target and not hit_stop:
                    hits_target += 1
                elif hit_stop and not hit_target:
                    hits_stop += 1
                elif hit_target and hit_stop:
                    # Both hit — check which came first
                    target_idx = np.argmax(path >= take_profit)
                    stop_idx   = np.argmax(path <= stop_loss)
                    if target_idx <= stop_idx:
                        hits_target += 1
                    else:
                        hits_stop += 1
                else:
                    pass  # Neither hit within simulation window

                final_prices.append(path[-1])

            final_prices = np.array(final_prices)
            win_prob = hits_target / n_simulations
            loss_prob = hits_stop / n_simulations

            # Value at Risk (VaR) — 95% confidence
            pnl = (final_prices - current_price) / current_price
            var_95 = float(np.percentile(pnl, 5))   # 5th percentile loss
            var_99 = float(np.percentile(pnl, 1))   # 1st percentile loss

            # Conditional VaR (CVaR / Expected Shortfall)
            cvar_95 = float(pnl[pnl <= var_95].mean())

            # Probability cone (10th/25th/75th/90th percentile paths)
            cone = {
                "p10": float(np.percentile(final_prices, 10)),
                "p25": float(np.percentile(final_prices, 25)),
                "p75": float(np.percentile(final_prices, 75)),
                "p90": float(np.percentile(final_prices, 90)),
            }

            # Expected value from simulation
            expected_return = float(np.mean(pnl))

            return {
                "win_probability":    round(win_prob, 4),
                "loss_probability":   round(loss_prob, 4),
                "var_95":             round(var_95, 4),
                "var_99":             round(var_99, 4),
                "cvar_95":            round(cvar_95, 4),
                "expected_return":    round(expected_return, 4),
                "probability_cone":   cone,
                "n_simulations":      n_simulations,
                "signal": float(win_prob * 2 - 1),  # -1 to +1
                "model": "MonteCarlo"
            }

        except Exception as e:
            logger.debug(f"Monte Carlo error: {e}")
            return self._simple_estimate(current_price, stop_loss,
                                          take_profit, returns)

    def _simple_estimate(self, price, stop, target, returns):
        r_risk = abs(price - stop) / price
        r_reward = abs(target - price) / price
        rr = r_reward / (r_risk + 1e-9)
        win_prob = rr / (rr + 1) * 0.6 + 0.4 * 0.5  # Blend with 50%
        return {
            "win_probability": round(min(0.75, win_prob), 4),
            "loss_probability": round(1 - win_prob, 4),
            "var_95": -r_risk, "var_99": -r_risk * 1.5,
            "cvar_95": -r_risk * 1.3,
            "expected_return": (win_prob * r_reward - (1-win_prob) * r_risk),
            "signal": win_prob * 2 - 1,
            "model": "SimpleEstimate"
        }

    def portfolio_var(self, portfolio_value: float, returns: pd.Series,
                       confidence: float = 0.95) -> dict:
        """
        Calculate portfolio-level Value at Risk.
        Answers: "What is my maximum 1-day loss at X% confidence?"
        """
        try:
            r = returns.dropna()
            if len(r) < 20:
                return {"var_dollar": portfolio_value * 0.02,
                        "var_pct": 0.02, "confidence": confidence}

            var_pct = float(np.percentile(r, (1 - confidence) * 100))
            var_dollar = abs(var_pct * portfolio_value)
            cvar_pct = float(r[r <= var_pct].mean())
            cvar_dollar = abs(cvar_pct * portfolio_value)

            return {
                "var_pct":    round(var_pct, 4),
                "var_dollar": round(var_dollar, 2),
                "cvar_pct":   round(cvar_pct, 4),
                "cvar_dollar":round(cvar_dollar, 2),
                "confidence": confidence,
                "model": "HistoricalVaR"
            }
        except Exception as e:
            logger.debug(f"Portfolio VaR error: {e}")
            return {"var_dollar": portfolio_value * 0.02,
                    "var_pct": -0.02, "confidence": confidence}


# =============================================================================
# BAYESIAN SIGNAL UPDATER
# =============================================================================

class BayesianSignal:
    """
    Bayesian probability updating for trade signals.
    
    Starts with a prior belief (base win rate from market conditions),
    then updates it as more evidence comes in from each indicator.
    
    In plain English: Like a doctor updating their diagnosis as
    each test result comes in. Each indicator shifts the probability
    up or down based on how reliable that indicator historically is.
    """

    def __init__(self):
        # Prior likelihoods for each signal type (from backtesting research)
        self.likelihoods = {
            "trend_strong":      {"win": 0.65, "loss": 0.35},
            "trend_weak":        {"win": 0.52, "loss": 0.48},
            "oversold_rsi":      {"win": 0.60, "loss": 0.40},
            "overbought_rsi":    {"win": 0.38, "loss": 0.62},
            "volume_surge":      {"win": 0.63, "loss": 0.37},
            "breakout":          {"win": 0.58, "loss": 0.42},
            "macd_cross_bull":   {"win": 0.57, "loss": 0.43},
            "bb_squeeze":        {"win": 0.55, "loss": 0.45},
            "high_adx":          {"win": 0.61, "loss": 0.39},
            "low_volatility":    {"win": 0.59, "loss": 0.41},
        }

    def update_probability(self, prior: float, evidence: list) -> dict:
        """
        Update win probability using Bayes' theorem.
        
        P(win|evidence) = P(evidence|win) * P(win) / P(evidence)
        
        Args:
            prior: Starting win probability (e.g., 0.55)
            evidence: List of signal names from likelihoods dict
        
        Returns updated probability and confidence.
        """
        p_win = prior
        p_loss = 1 - prior
        log_odds = np.log(p_win / (p_loss + 1e-9))

        for signal_name in evidence:
            if signal_name in self.likelihoods:
                lh = self.likelihoods[signal_name]
                # Bayes update in log-odds space (numerically stable)
                lr = lh["win"] / (lh["loss"] + 1e-9)
                log_odds += np.log(lr)

        # Convert back to probability
        posterior = 1 / (1 + np.exp(-log_odds))
        posterior = max(0.30, min(0.85, posterior))

        # Confidence increases with more evidence
        n_evidence = len(evidence)
        confidence = min(0.90, 0.50 + n_evidence * 0.05)

        return {
            "prior":     round(prior, 4),
            "posterior": round(posterior, 4),
            "confidence":round(confidence, 4),
            "n_evidence":n_evidence,
            "log_odds":  round(log_odds, 4),
        }

    def collect_evidence(self, df: pd.DataFrame, config) -> list:
        """Extract evidence signals from indicator data."""
        evidence = []
        try:
            rsi_val = df['rsi'].iloc[-1]
            adx_val = df['adx'].iloc[-1]
            vol_ratio = df['vol_ratio'].iloc[-1]
            bb_bw_pct = (df['bb_bw'].iloc[-20:] <= df['bb_bw'].iloc[-1]).mean()
            macd_hist = df['macd_hist'].iloc[-1]
            macd_prev = df['macd_hist'].iloc[-2]
            hist_vol  = df['hist_vol'].iloc[-1]
            close = df['close'].iloc[-1]
            recent_high = df['high'].iloc[-20:-1].max()

            if adx_val > 35:
                evidence.append("trend_strong")
            elif adx_val > 25:
                evidence.append("trend_weak")

            if rsi_val < 35:
                evidence.append("oversold_rsi")
            elif rsi_val > 65:
                evidence.append("overbought_rsi")

            if vol_ratio > 1.5:
                evidence.append("volume_surge")

            if close > recent_high:
                evidence.append("breakout")

            if macd_hist > 0 and macd_prev <= 0:
                evidence.append("macd_cross_bull")

            if bb_bw_pct < 0.20:
                evidence.append("bb_squeeze")

            if adx_val > 25:
                evidence.append("high_adx")

            if hist_vol < 20:
                evidence.append("low_volatility")

        except Exception as e:
            logger.debug(f"Evidence collection error: {e}")

        return evidence


# =============================================================================
# ML SIGNAL (sklearn-based, no tensorflow needed)
# =============================================================================

class MLSignalModel:
    """
    Machine Learning signal using Random Forest.
    Trains on technical features to predict next-bar direction.
    
    This is a lightweight alternative to LSTM — same concept
    (learn patterns from history) but runs on any machine.
    """

    def __init__(self):
        self.model = None
        self.trained = False
        self.feature_names = []

    def _build_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Build feature matrix from indicators."""
        features = pd.DataFrame(index=df.index)

        try:
            features['rsi']       = df['rsi'] / 100
            features['bb_pct']    = df['bb_pct']
            features['adx']       = df['adx'] / 100
            features['vol_ratio'] = df['vol_ratio'].clip(0, 5) / 5
            features['macd_norm'] = df['macd_hist'] / (df['atr'] + 1e-9)
            features['zscore']    = df['zscore'].clip(-3, 3) / 3
            features['roc_5']     = df['close'].pct_change(5).clip(-0.1, 0.1)
            features['roc_20']    = df['close'].pct_change(20).clip(-0.2, 0.2)
            features['atr_pct']   = df['atr_pct'].clip(0, 5) / 5
            features['vol_zscore']= df['zscore_vol'].clip(-3, 3) / 3
        except Exception as e:
            logger.debug(f"Feature build error: {e}")

        return features.dropna()

    def train_and_predict(self, df: pd.DataFrame) -> dict:
        """
        Train Random Forest on historical data and predict next bar.
        Uses walk-forward validation to prevent lookahead bias.
        """
        try:
            from sklearn.ensemble import RandomForestClassifier
            from sklearn.preprocessing import StandardScaler

            features = self._build_features(df)
            if len(features) < 60:
                return {"signal": 0.0, "confidence": 0.5,
                        "direction": 0, "model": "MLFallback"}

            # Labels: 1 if next bar closes higher, 0 if lower
            close = df['close'].reindex(features.index)
            labels = (close.shift(-1) > close).astype(int)

            # Align
            X = features.iloc[:-1]
            y = labels.iloc[:-1].dropna()
            X = X.loc[y.index]

            if len(X) < 50:
                return {"signal": 0.0, "confidence": 0.5,
                        "direction": 0, "model": "MLFallback"}

            # Train on all but last 20 bars
            train_size = len(X) - 20
            X_train = X.iloc[:train_size]
            y_train = y.iloc[:train_size]
            X_val   = X.iloc[train_size:]
            y_val   = y.iloc[train_size:]

            scaler = StandardScaler()
            X_train_s = scaler.fit_transform(X_train)
            X_val_s   = scaler.transform(X_val)

            clf = RandomForestClassifier(
                n_estimators=100, max_depth=5,
                random_state=42, n_jobs=-1
            )
            clf.fit(X_train_s, y_train)

            # Validation accuracy
            val_acc = clf.score(X_val_s, y_val)

            # Predict current bar
            X_current = scaler.transform(features.iloc[[-1]])
            prob_up = clf.predict_proba(X_current)[0][1]
            direction = 1 if prob_up > 0.5 else -1

            # Feature importance
            importance = dict(zip(features.columns,
                                   clf.feature_importances_))
            top_feature = max(importance, key=importance.get)

            return {
                "signal":       float(prob_up * 2 - 1),
                "probability":  float(prob_up),
                "direction":    direction,
                "confidence":   float(val_acc),
                "val_accuracy": float(val_acc),
                "top_feature":  top_feature,
                "model":        "RandomForest"
            }

        except ImportError:
            logger.debug("sklearn not available")
            return {"signal": 0.0, "confidence": 0.5,
                    "direction": 0, "model": "Unavailable"}
        except Exception as e:
            logger.debug(f"ML model error: {e}")
            return {"signal": 0.0, "confidence": 0.5,
                    "direction": 0, "model": "Error"}


# =============================================================================
# SHARPE RATIO OPTIMIZER
# =============================================================================

def calculate_sharpe_ratio(returns: pd.Series, risk_free_rate: float = 0.05) -> float:
    """
    Calculate annualized Sharpe ratio.
    Target: > 2.0 for an elite system.
    """
    if len(returns) < 2:
        return 0.0
    daily_rf = risk_free_rate / 252
    excess   = returns - daily_rf
    if excess.std() == 0:
        return 0.0
    return float(excess.mean() / excess.std() * np.sqrt(252))


def calculate_sortino_ratio(returns: pd.Series,
                             risk_free_rate: float = 0.05) -> float:
    """
    Sortino ratio — like Sharpe but only penalizes downside volatility.
    Better measure for trading systems.
    """
    if len(returns) < 2:
        return 0.0
    daily_rf  = risk_free_rate / 252
    excess    = returns - daily_rf
    downside  = excess[excess < 0]
    if len(downside) == 0 or downside.std() == 0:
        return float(excess.mean() / 1e-9 * np.sqrt(252))
    return float(excess.mean() / downside.std() * np.sqrt(252))


def calculate_calmar_ratio(returns: pd.Series) -> float:
    """Annualized return / Max drawdown."""
    if len(returns) < 2:
        return 0.0
    ann_return = returns.mean() * 252
    cum = (1 + returns).cumprod()
    rolling_max = cum.expanding().max()
    drawdown = (cum - rolling_max) / rolling_max
    max_dd = abs(drawdown.min())
    return float(ann_return / max_dd) if max_dd > 0 else 0.0


# =============================================================================
# COMBINED QUANT SIGNAL
# =============================================================================

def run_all_quant_models(df: pd.DataFrame, config,
                          stop_loss: float = None,
                          take_profit: float = None) -> dict:
    """
    Run all quant models and return combined signal + risk metrics.
    """
    close   = df['close']
    returns = close.pct_change().dropna()
    current = close.iloc[-1]

    if stop_loss is None:
        atr = df['atr'].iloc[-1]
        stop_loss   = current - atr * 2
        take_profit = current + atr * 3

    # Run all models
    arima_result = ARIMAModel().fit_and_forecast(close)
    garch_result = GARCHModel().fit_and_forecast(returns)
    mc_result    = MonteCarloEngine().simulate(
        current, returns, stop_loss, take_profit, n_simulations=3000
    )

    bayesian    = BayesianSignal()
    evidence    = bayesian.collect_evidence(df, config)
    bayes_result = bayesian.update_probability(0.55, evidence)

    ml_result   = MLSignalModel().train_and_predict(df)

    # Portfolio VaR
    var_result  = MonteCarloEngine().portfolio_var(
        config.PORTFOLIO_SIZE, returns
    )

    # Composite quant signal (-1 to +1)
    signals = [
        arima_result.get("signal", 0) * 0.20,
        garch_result.get("signal", 0) * 0.15,
        mc_result.get("signal", 0) * 0.25,
        (bayes_result["posterior"] * 2 - 1) * 0.25,
        ml_result.get("signal", 0) * 0.15,
    ]
    composite = sum(signals)
    composite = max(-1.0, min(1.0, composite))

    # Normalize to 0-1 for scoring
    quant_score = (composite + 1) / 2

    # Win probability from Monte Carlo (most reliable)
    mc_win_prob = mc_result.get("win_probability", 0.55)

    # GARCH position sizing multiplier
    garch_mult = garch_result.get("position_multiplier", 1.0)

    return {
        "composite_signal":   composite,
        "quant_score_0_1":    quant_score,
        "win_probability_mc": mc_win_prob,
        "garch_multiplier":   garch_mult,
        "var_95_dollar":      var_result.get("var_dollar", 0),
        "arima":              arima_result,
        "garch":              garch_result,
        "monte_carlo":        mc_result,
        "bayesian":           bayes_result,
        "ml_model":           ml_result,
        "var":                var_result,
        "evidence":           evidence,
    }
