# ============================================================
#  fundamentals.py — Lynch / Buffett / Munger / Ackman metrics
# ============================================================

import yfinance as yf
import math
import config

def _safe(val):
    try:
        if val is None:
            return None
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except Exception:
        return None

def get_fundamentals(ticker: str) -> dict:
    try:
        stock = yf.Ticker(ticker)
        info  = stock.info

        market_cap = _safe(info.get("marketCap"))
        if not market_cap or market_cap == 0:
            return None

        inc = stock.financials
        cf  = stock.cashflow

        # FCF
        fcf      = _safe(cf.loc["Free Cash Flow"].iloc[0])
        fcf_prev = _safe(cf.loc["Free Cash Flow"].iloc[1]) if cf.shape[1] >= 2 else None
        if fcf is None:
            fcf = _safe(info.get("freeCashflow"))
        if fcf is None:
            return None

        fcf_yield_pct  = (fcf / market_cap) * 100
        fcf_growth_pct = ((fcf - fcf_prev) / abs(fcf_prev)) * 100 if fcf_prev and fcf_prev != 0 else None

        # Revenue
        rev_curr = _safe(inc.loc["Total Revenue"].iloc[0])
        rev_prev = _safe(inc.loc["Total Revenue"].iloc[1]) if inc.shape[1] >= 2 else None
        rev_growth_pct = ((rev_curr - rev_prev) / abs(rev_prev)) * 100 if rev_curr and rev_prev and rev_prev != 0 else None
        if rev_growth_pct is None:
            rg = _safe(info.get("revenueGrowth"))
            rev_growth_pct = rg * 100 if rg else None

        # Rule of 40
        fcf_margin = (fcf / rev_curr) * 100 if fcf and rev_curr and rev_curr != 0 else None
        rule_of_40 = rev_growth_pct + fcf_margin if rev_growth_pct is not None and fcf_margin is not None else None

        # PEG
        peg = _safe(info.get("trailingPegRatio"))
        if peg is None:
            pe = _safe(info.get("trailingPE"))
            eg = _safe(info.get("earningsGrowth"))
            peg = pe / (eg * 100) if pe and eg and eg > 0 else None

        # EPS Growth
        ni0 = _safe(inc.loc["Net Income"].iloc[0])
        ni1 = _safe(inc.loc["Net Income"].iloc[1]) if inc.shape[1] >= 2 else None
        eps_growth_pct = ((ni0 - ni1) / abs(ni1)) * 100 if ni0 and ni1 and ni1 != 0 else None
        if eps_growth_pct is None:
            eg = _safe(info.get("earningsGrowth"))
            eps_growth_pct = eg * 100 if eg else None

        # ROE
        roe = _safe(info.get("returnOnEquity"))
        roe = roe * 100 if roe else None

        # Debt/EBITDA
        total_debt = _safe(info.get("totalDebt"))
        ebitda     = _safe(info.get("ebitda"))
        debt_to_ebitda = total_debt / ebitda if total_debt and ebitda and ebitda > 0 else None

        # Current Ratio
        current_ratio = _safe(info.get("currentRatio"))

        # Interest Coverage
        ebit_val = _safe(inc.loc["EBIT"].iloc[0])             if "EBIT"             in inc.index else None
        int_exp  = _safe(inc.loc["Interest Expense"].iloc[0]) if "Interest Expense" in inc.index else None
        interest_coverage = abs(ebit_val / int_exp) if ebit_val and int_exp and int_exp != 0 else None

        # Score
        score = 0.0
        total = 0.0

        def check(value, threshold, higher_is_better=True, weight=1.0):
            nonlocal score, total
            total += weight
            if value is None:
                return
            if higher_is_better:
                score += weight if value >= threshold else weight * max(0.0, value / threshold)
            else:
                score += weight if value <= threshold else weight * max(0.0, threshold / value)

        check(peg,               config.MAX_PEG_RATIO,          higher_is_better=False, weight=2.0)
        check(eps_growth_pct,    config.MIN_EPS_GROWTH_PCT,     higher_is_better=True,  weight=1.5)
        check(roe,               config.MIN_ROE_PCT,            higher_is_better=True,  weight=1.5)
        check(fcf_yield_pct,     config.MIN_FCF_YIELD_PCT,      higher_is_better=True,  weight=2.0)
        check(fcf_growth_pct,    config.MIN_FCF_GROWTH_PCT,     higher_is_better=True,  weight=1.5)
        check(rev_growth_pct,    config.MIN_REVENUE_GROWTH_PCT, higher_is_better=True,  weight=1.5)
        check(rule_of_40,        config.MIN_RULE_OF_40,         higher_is_better=True,  weight=2.0)
        check(debt_to_ebitda,    config.MAX_DEBT_TO_EBITDA,     higher_is_better=False, weight=1.5)
        check(current_ratio,     config.MIN_CURRENT_RATIO,      higher_is_better=True,  weight=0.5)
        check(interest_coverage, config.MIN_INTEREST_COVERAGE,  higher_is_better=True,  weight=0.5)

        fundamental_score = round((score / total) if total > 0 else 0.0, 4)

        def fmt(val, d=2):
            return round(val, d) if val is not None else None

        return {
            "ticker":            ticker,
            "peg_ratio":         fmt(peg),
            "eps_growth_pct":    fmt(eps_growth_pct, 1),
            "roe_pct":           fmt(roe, 1),
            "fcf":               fmt(fcf / 1e9),
            "fcf_yield_pct":     fmt(fcf_yield_pct),
            "fcf_growth_pct":    fmt(fcf_growth_pct, 1),
            "rev_growth_pct":    fmt(rev_growth_pct, 1),
            "rule_of_40":        fmt(rule_of_40, 1),
            "debt_to_ebitda":    fmt(debt_to_ebitda),
            "current_ratio":     fmt(current_ratio),
            "interest_coverage": fmt(interest_coverage, 1),
            "market_cap_b":      fmt(market_cap / 1e9, 1),
            "sector":            info.get("sector", "Unknown"),
            "industry":          info.get("industry", "Unknown"),
            "fundamental_score": fundamental_score,
        }

    except Exception as e:
        print(f"  [fundamentals ERROR] {ticker}: {e}")
        import traceback
        traceback.print_exc()
        return None
