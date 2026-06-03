"""
=============================================================================
JIM SIMONS STATISTICAL ARBITRAGE
=============================================================================
Renaissance-style statistical edge:

1. COINTEGRATION PAIRS TRADING
   - Find pairs with long-run price relationship (cointegrated)
   - Trade when spread diverges beyond 2 standard deviations
   - Mean-reversion back to equilibrium = profit

2. PCA FACTOR DECOMPOSITION
   - Reduce 150 stocks to latent risk factors
   - Trade stocks with mispriced factor loadings
   - Market-neutral positioning

3. MEAN REVERSION ON RESIDUALS
   - After removing market/factor effects, trade pure alpha
   - High-frequency signal on stationary residuals
   - Ornstein-Uhlenbeck process for optimal entry/exit

4. AUTOCORRELATION MOMENTUM
   - Detect persistent return patterns (momentum)
   - Trade in direction of signal before decay
   - Signal half-life estimation
=============================================================================
"""

import logging
import numpy as np
import pandas as pd
from scipy import stats
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)


class SimonsStatArb:
    """
    Jim Simons-inspired statistical arbitrage engine.
    """

    def __init__(self, config):
        self.config       = config
        self.pairs        = []      # Cointegrated pairs
        self.factor_model = None
        self._price_cache = {}

    # ─────────────────────────────────────────────────────────────────────
    # 1. DATA FETCHING
    # ─────────────────────────────────────────────────────────────────────

    def fetch_prices(self, symbols: List[str],
                      period: str = "1y") -> pd.DataFrame:
        """Fetch historical close prices for multiple symbols."""
        try:
            import yfinance as yf
            data = yf.download(
                symbols, period=period, interval="1d",
                auto_adjust=True, progress=False
            )
            if isinstance(data.columns, pd.MultiIndex):
                prices = data['Close']
            else:
                prices = data[['Close']]
                prices.columns = symbols[:1]

            prices = prices.dropna(how='all')
            return prices
        except Exception as e:
            logger.error(f"Price fetch error: {e}")
            return pd.DataFrame()

    # ─────────────────────────────────────────────────────────────────────
    # 2. COINTEGRATION TESTING
    # ─────────────────────────────────────────────────────────────────────

    def test_cointegration(self, price1: pd.Series,
                            price2: pd.Series) -> dict:
        """
        Engle-Granger cointegration test.
        If p-value < 0.05, series are cointegrated — pairs trade!
        """
        try:
            from statsmodels.tsa.stattools import coint
            score, pvalue, _ = coint(price1, price2)

            if pvalue < 0.05:
                # Estimate hedge ratio via OLS
                slope, intercept, r, p, se = stats.linregress(price2, price1)
                spread = price1 - slope * price2 - intercept
                spread_mean = spread.mean()
                spread_std  = spread.std()
                z_score     = (spread.iloc[-1] - spread_mean) / (spread_std + 1e-9)

                # Half-life of mean reversion
                spread_lag  = spread.shift(1).dropna()
                spread_diff = spread.diff().dropna()
                aligned     = pd.concat([spread_lag, spread_diff], axis=1).dropna()
                if len(aligned) > 10:
                    lm_slope, _, _, _, _ = stats.linregress(
                        aligned.iloc[:, 0], aligned.iloc[:, 1]
                    )
                    half_life = -np.log(2) / (lm_slope + 1e-9)
                    half_life = max(1, min(60, half_life))
                else:
                    half_life = 10

                return {
                    "cointegrated": True,
                    "p_value":      round(float(pvalue), 4),
                    "hedge_ratio":  round(float(slope), 4),
                    "intercept":    round(float(intercept), 4),
                    "z_score":      round(float(z_score), 4),
                    "spread_mean":  round(float(spread_mean), 4),
                    "spread_std":   round(float(spread_std), 4),
                    "half_life":    round(float(half_life), 1),
                    "r_squared":    round(float(r**2), 4),
                }
            else:
                return {"cointegrated": False, "p_value": round(float(pvalue), 4)}

        except Exception as e:
            logger.debug(f"Cointegration test error: {e}")
            return {"cointegrated": False, "p_value": 1.0}

    def find_pairs(self, universe: List[str],
                    max_pairs: int = 20) -> List[dict]:
        """
        Scan universe for cointegrated pairs.
        Returns top pairs by p-value.
        """
        logger.info(f"Scanning {len(universe)} symbols for pairs...")

        # Use top 30 most liquid for pair scanning (speed)
        candidates = universe[:30]
        prices     = self.fetch_prices(candidates, period="1y")

        if prices.empty:
            return []

        pairs = []
        tested = 0

        for i in range(len(candidates)):
            for j in range(i+1, len(candidates)):
                sym1 = candidates[i]
                sym2 = candidates[j]

                if sym1 not in prices.columns or sym2 not in prices.columns:
                    continue

                p1 = prices[sym1].dropna()
                p2 = prices[sym2].dropna()

                # Align
                aligned = pd.concat([p1, p2], axis=1).dropna()
                if len(aligned) < 100:
                    continue

                result = self.test_cointegration(
                    aligned.iloc[:, 0], aligned.iloc[:, 1]
                )
                tested += 1

                if result["cointegrated"]:
                    pairs.append({
                        "symbol1": sym1,
                        "symbol2": sym2,
                        **result,
                    })

        # Sort by p-value (most cointegrated first)
        pairs.sort(key=lambda x: x["p_value"])
        self.pairs = pairs[:max_pairs]

        logger.info(f"Found {len(self.pairs)} cointegrated pairs "
                   f"from {tested} tests")
        return self.pairs

    def get_pair_signal(self, pair: dict,
                         current_prices: dict) -> dict:
        """
        Generate trading signal for a cointegrated pair.
        Trade when z-score > 2 (spread too wide) or < -2 (too narrow).
        """
        sym1 = pair["symbol1"]
        sym2 = pair["symbol2"]

        p1 = current_prices.get(sym1, 0)
        p2 = current_prices.get(sym2, 0)

        if p1 <= 0 or p2 <= 0:
            return {"signal": 0.5, "action": "NONE"}

        # Current spread
        spread     = p1 - pair["hedge_ratio"] * p2 - pair["intercept"]
        z_score    = (spread - pair["spread_mean"]) / (pair["spread_std"] + 1e-9)
        half_life  = pair["half_life"]

        # Entry/exit thresholds
        entry_z    = 2.0
        exit_z     = 0.5

        if z_score > entry_z:
            # Spread too wide: sell sym1, buy sym2
            signal = 0.80
            action = f"SHORT {sym1} / LONG {sym2}"
            direction1 = "short"
            direction2 = "long"
        elif z_score < -entry_z:
            # Spread too narrow: buy sym1, sell sym2
            signal = 0.80
            action = f"LONG {sym1} / SHORT {sym2}"
            direction1 = "long"
            direction2 = "short"
        elif abs(z_score) < exit_z:
            signal = 0.50
            action = "EXIT — spread converged"
            direction1 = "exit"
            direction2 = "exit"
        else:
            signal = 0.50
            action = "HOLD"
            direction1 = "none"
            direction2 = "none"

        return {
            "signal":      round(float(signal), 4),
            "z_score":     round(float(z_score), 4),
            "spread":      round(float(spread), 4),
            "action":      action,
            "direction1":  direction1,
            "direction2":  direction2,
            "half_life":   half_life,
            "sym1":        sym1,
            "sym2":        sym2,
            "p_value":     pair["p_value"],
        }

    # ─────────────────────────────────────────────────────────────────────
    # 3. PCA FACTOR MODEL
    # ─────────────────────────────────────────────────────────────────────

    def build_factor_model(self, universe: List[str],
                            n_factors: int = 5) -> dict:
        """
        Build PCA factor model to extract market-neutral alpha.
        Returns factor loadings and residual returns.
        """
        try:
            from sklearn.decomposition import PCA
            from sklearn.preprocessing import StandardScaler

            prices  = self.fetch_prices(universe[:50], period="6mo")
            if prices.empty or prices.shape[1] < 10:
                return {}

            returns = prices.pct_change().dropna()

            # Standardize
            scaler  = StandardScaler()
            scaled  = scaler.fit_transform(returns.fillna(0))

            # PCA
            pca     = PCA(n_components=min(n_factors, scaled.shape[1]))
            factors = pca.fit_transform(scaled)
            loadings= pca.components_

            explained = pca.explained_variance_ratio_
            logger.info(f"PCA: {n_factors} factors explain "
                       f"{explained.sum():.1%} of variance")

            # Residual returns (market-neutral alpha)
            reconstructed = factors @ loadings
            residuals     = scaled - reconstructed
            residual_df   = pd.DataFrame(
                residuals, index=returns.index,
                columns=returns.columns[:residuals.shape[1]]
            )

            # Z-score residuals for signal generation
            residual_z = (residual_df - residual_df.mean()) / (residual_df.std() + 1e-9)
            latest_z   = residual_z.iloc[-1]

            # Symbols with extreme residuals = stat arb opportunities
            oversold  = latest_z[latest_z < -2.0].index.tolist()
            overbought= latest_z[latest_z > 2.0].index.tolist()

            self.factor_model = {
                "pca":           pca,
                "scaler":        scaler,
                "loadings":      loadings,
                "explained":     explained.tolist(),
                "residual_z":    latest_z.to_dict(),
                "oversold":      oversold,
                "overbought":    overbought,
            }

            return self.factor_model

        except Exception as e:
            logger.error(f"Factor model error: {e}")
            return {}

    def get_statarb_signal(self, symbol: str) -> dict:
        """
        Get statistical arbitrage signal for a symbol.
        Based on PCA residual z-score.
        """
        if not self.factor_model:
            return {"signal": 0.5, "z_score": 0, "action": "NO_MODEL"}

        z = self.factor_model.get("residual_z", {}).get(symbol, 0)

        if z < -2.5:
            signal = 0.85   # Very oversold vs factors = buy
            action = "STATARB_LONG"
        elif z < -2.0:
            signal = 0.75
            action = "STATARB_LONG"
        elif z > 2.5:
            signal = 0.20   # Very overbought vs factors = sell
            action = "STATARB_SHORT"
        elif z > 2.0:
            signal = 0.30
            action = "STATARB_SHORT"
        else:
            signal = 0.50
            action = "NEUTRAL"

        return {
            "signal":  round(float(signal), 4),
            "z_score": round(float(z), 4),
            "action":  action,
            "in_oversold":   symbol in self.factor_model.get("oversold", []),
            "in_overbought": symbol in self.factor_model.get("overbought", []),
        }

    # ─────────────────────────────────────────────────────────────────────
    # 4. AUTOCORRELATION MOMENTUM
    # ─────────────────────────────────────────────────────────────────────

    def autocorrelation_signal(self, returns: pd.Series,
                                lags: List[int] = [1, 5, 10, 20]) -> dict:
        """
        Detect significant autocorrelation patterns.
        Positive autocorrelation = momentum persists.
        Negative autocorrelation = mean reversion.
        """
        try:
            r = returns.dropna()
            if len(r) < 50:
                return {"signal": 0.5, "pattern": "INSUFFICIENT_DATA"}

            autocorrs = {}
            for lag in lags:
                if len(r) > lag + 10:
                    ac = r.autocorr(lag=lag)
                    autocorrs[f"lag_{lag}"] = round(float(ac), 4) if not np.isnan(ac) else 0

            # Signal from short-term autocorrelation
            ac1  = autocorrs.get("lag_1", 0)
            ac5  = autocorrs.get("lag_5", 0)
            ac20 = autocorrs.get("lag_20", 0)

            # Composite momentum/reversion signal
            if ac1 > 0.10 and ac5 > 0.05:
                signal  = 0.70   # Strong momentum
                pattern = "MOMENTUM"
            elif ac1 < -0.10:
                signal  = 0.35   # Mean reversion
                pattern = "MEAN_REVERSION"
            elif ac20 > 0.15:
                signal  = 0.65   # Long-term momentum
                pattern = "TREND"
            else:
                signal  = 0.50
                pattern = "RANDOM_WALK"

            # Direction based on recent return
            recent_ret = r.iloc[-5:].mean()
            if recent_ret > 0 and signal > 0.60:
                direction = "LONG"
            elif recent_ret < 0 and signal < 0.40:
                direction = "SHORT"
            else:
                direction = "NEUTRAL"

            return {
                "signal":       round(float(signal), 4),
                "pattern":      pattern,
                "direction":    direction,
                "autocorrs":    autocorrs,
                "recent_return":round(float(recent_ret), 5),
            }

        except Exception as e:
            logger.debug(f"Autocorrelation error: {e}")
            return {"signal": 0.5, "pattern": "ERROR"}
