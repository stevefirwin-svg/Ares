# ============================================================
#  diagnose.py — Tests what yfinance is actually returning
#  Run: python diagnose.py
# ============================================================

import yfinance as yf

ticker = "MSFT"
print(f"Testing yfinance data for {ticker}...\n")

stock = yf.Ticker(ticker)

# 1. Basic info
print("=== INFO FIELDS ===")
info = stock.info
fields = ["marketCap", "trailingPegRatio", "pegRatio", "returnOnEquity",
          "freeCashflow", "totalDebt", "ebitda", "currentRatio",
          "interestExpense", "revenueGrowth", "earningsGrowth", "trailingPE"]
for f in fields:
    print(f"  {f}: {info.get(f, 'NOT FOUND')}")

# 2. Cash flow statement
print("\n=== CASH FLOW INDEX LABELS ===")
try:
    cf = stock.cashflow
    if cf is not None and not cf.empty:
        print("  Columns (dates):", list(cf.columns[:2]))
        print("  Row labels:")
        for lbl in cf.index:
            print(f"    {lbl}")
    else:
        print("  EMPTY or None")
except Exception as e:
    print(f"  ERROR: {e}")

# 3. Income statement
print("\n=== INCOME STATEMENT INDEX LABELS ===")
try:
    inc = stock.financials
    if inc is not None and not inc.empty:
        print("  Columns (dates):", list(inc.columns[:2]))
        print("  Row labels:")
        for lbl in inc.index:
            print(f"    {lbl}")
    else:
        print("  EMPTY or None")
except Exception as e:
    print(f"  ERROR: {e}")

# 4. History check
print("\n=== PRICE HISTORY CHECK ===")
try:
    hist = stock.history(period="18mo", interval="1d")
    print(f"  Rows returned: {len(hist)}")
    print(f"  Latest close: {hist['Close'].iloc[-1]:.2f}")
except Exception as e:
    print(f"  ERROR: {e}")

print("\nDiagnostic complete.")
