"""
sector_map.py — Ares Symbol-to-Sector ETF Mapping
===================================================
Maps each universe symbol to its GICS-based sector ETF.
Used exclusively by Engine E (RS Rotation) for sector-relative RS computation.

Sector ETFs:
  XLK  — Technology
  XLF  — Financials
  XLV  — Health Care
  XLE  — Energy
  XLY  — Consumer Discretionary
  XLI  — Industrials
  XLB  — Materials
  XLC  — Communication Services
  XLU  — Utilities
  XLRE — Real Estate
  XLP  — Consumer Staples

Maintenance:
  Add new symbols as the universe grows (ares_universe.py rebuilds daily).
  GICS sector assignments change infrequently — check quarterly.
  When a symbol is reclassified, update here and log the change date.

Last updated: 2026-05-29 — full initial universe mapping.
"""

SYMBOL_SECTOR_MAP = {
    # ── XLK — Technology ──────────────────────────────────────────────────────
    "ANET":  "XLK",   # Arista Networks — networking hardware
    "APH":   "XLK",   # Amphenol — electronic components
    "APLD":  "XLK",   # Applied Digital — AI infrastructure
    "ASTS":  "XLK",   # AST SpaceMobile — satellite comms
    "BITO":  "XLK",   # ProShares Bitcoin ETF — crypto proxy
    "BMNR":  "XLK",   # BitMine — crypto mining
    "CIFR":  "XLK",   # Cipher Mining — crypto mining
    "CORZ":  "XLK",   # Core Scientific — crypto/HPC
    "CRM":   "XLK",   # Salesforce — enterprise software
    "CRWV":  "XLK",   # CoreWeave — AI cloud
    "CSCO":  "XLK",   # Cisco — networking
    "DRAM":  "XLK",   # Adram — memory tech
    "DT":    "XLK",   # Dynatrace — observability software
    "DUOL":  "XLK",   # Duolingo — edtech/software
    "EA":    "XLK",   # Electronic Arts — gaming software
    "FBTC":  "XLK",   # Fidelity Bitcoin ETF — crypto proxy
    "FI":    "XLK",   # Fiserv — fintech/payments
    "GOOG":  "XLK",   # Alphabet — technology/advertising
    "HOOD":  "XLK",   # Robinhood — fintech
    "IBIT":  "XLK",   # iShares Bitcoin ETF — crypto proxy
    "ICE":   "XLK",   # Intercontinental Exchange — fintech/data
    "INTU":  "XLK",   # Intuit — financial software
    "IONQ":  "XLK",   # IonQ — quantum computing
    "MARA":  "XLK",   # Marathon Digital — crypto mining
    "META":  "XLK",   # Meta — technology/social media
    "MPWR":  "XLK",   # Monolithic Power — semiconductors
    "MRVL":  "XLK",   # Marvell — semiconductors
    "MSFT":  "XLK",   # Microsoft — technology
    "MSI":   "XLK",   # Motorola Solutions — communications tech
    "MSTR":  "XLK",   # MicroStrategy — Bitcoin/software
    "MU":    "XLK",   # Micron — memory semiconductors
    "NET":   "XLK",   # Cloudflare — cloud networking
    "NOW":   "XLK",   # ServiceNow — enterprise software
    "NVDA":  "XLK",   # NVIDIA — semiconductors/AI
    "NVDL":  "XLK",   # GraniteShares 2x NVDA — leveraged ETF
    "ON":    "XLK",   # ON Semiconductor
    "ORCL":  "XLK",   # Oracle — enterprise software
    "PANW":  "XLK",   # Palo Alto Networks — cybersecurity
    "PLTR":  "XLK",   # Palantir — data analytics/AI
    "RDDT":  "XLK",   # Reddit — social media/tech
    "RKLB":  "XLK",   # Rocket Lab — space tech
    "RSSS":  "XLK",   # Research Solutions — SaaS
    "S":     "XLK",   # SentinelOne — cybersecurity
    "SMCI":  "XLK",   # Super Micro Computer — server hardware
    "SNOW":  "XLK",   # Snowflake — cloud data
    "SOFI":  "XLK",   # SoFi Technologies — fintech
    "SQ":    "XLK",   # Block (Square) — fintech/payments
    "TTD":   "XLK",   # Trade Desk — adtech
    "WKME":  "XLK",   # WalkMe — digital adoption platform
    "ZS":    "XLK",   # Zscaler — cybersecurity

    # ── XLF — Financials ──────────────────────────────────────────────────────
    "BN":    "XLF",   # Brookfield Corp — diversified financials
    "GS":    "XLF",   # Goldman Sachs
    "LPLA":  "XLF",   # LPL Financial
    "MA":    "XLF",   # Mastercard — payments
    "MCO":   "XLF",   # Moody's — financial data
    "SCHW":  "XLF",   # Charles Schwab — brokerage
    "SPGI":  "XLF",   # S&P Global — financial data
    "USB":   "XLF",   # U.S. Bancorp — banking
    "V":     "XLF",   # Visa — payments
    "WFC":   "XLF",   # Wells Fargo — banking

    # ── XLV — Health Care ─────────────────────────────────────────────────────
    "ABT":   "XLV",   # Abbott Laboratories — medical devices
    "BMY":   "XLV",   # Bristol-Myers Squibb — pharma
    "BSX":   "XLV",   # Boston Scientific — medical devices
    "CVS":   "XLV",   # CVS Health — pharmacy/health services
    "ELV":   "XLV",   # Elevance Health — managed care
    "GEHC":  "XLV",   # GE Healthcare — medical imaging
    "HCA":   "XLV",   # HCA Healthcare — hospitals
    "ISRG":  "XLV",   # Intuitive Surgical — surgical robots
    "LLY":   "XLV",   # Eli Lilly — pharma/GLP-1
    "MDT":   "XLV",   # Medtronic — medical devices
    "MRK":   "XLV",   # Merck — pharma
    "NTRA":  "XLV",   # Natera — genetic testing
    "PODD":  "XLV",   # Insulet — insulin delivery
    "RXRX":  "XLV",   # Recursion Pharmaceuticals — biotech/AI
    "TMO":   "XLV",   # Thermo Fisher — life science tools
    "UHS":   "XLV",   # Universal Health Services
    "VRTX":  "XLV",   # Vertex Pharmaceuticals — biotech

    # ── XLE — Energy ──────────────────────────────────────────────────────────
    "BP":    "XLE",   # BP — integrated oil
    "CNQ":   "XLE",   # Canadian Natural Resources
    "CVE":   "XLE",   # Cenovus Energy
    "DOW":   "XLE",   # Dow Inc — chemicals (XLB technically, XLE proxy)
    "DVN":   "XLE",   # Devon Energy — E&P
    "EOG":   "XLE",   # EOG Resources — E&P
    "FANG":  "XLE",   # Diamondback Energy — E&P
    "FCX":   "XLE",   # Freeport-McMoRan — copper mining
    "HES":   "XLE",   # Hess — E&P
    "LNG":   "XLE",   # Cheniere Energy — LNG
    "OKE":   "XLE",   # ONEOK — midstream
    "SLB":   "XLE",   # SLB (Schlumberger) — oilfield services
    "VST":   "XLE",   # Vistra — power gen/energy
    "WMB":   "XLE",   # Williams Companies — midstream
    "XOM":   "XLE",   # ExxonMobil — integrated oil

    # ── XLY — Consumer Discretionary ─────────────────────────────────────────
    "AAL":   "XLY",   # American Airlines
    "CCL":   "XLY",   # Carnival — cruise lines
    "CMG":   "XLY",   # Chipotle — restaurants
    "CPNG":  "XLY",   # Coupang — Korean e-commerce
    "DKNG":  "XLY",   # DraftKings — online gaming
    "HLT":   "XLY",   # Hilton — hotels
    "LULU":  "XLY",   # Lululemon — apparel
    "LYV":   "XLY",   # Live Nation — entertainment
    "MAT":   "XLY",   # Mattel — toys
    "NKE":   "XLY",   # Nike — footwear/apparel
    "PARA":  "XLY",   # Paramount — media/entertainment
    "PINS":  "XLY",   # Pinterest — social/consumer
    "RH":    "XLY",   # RH (Restoration Hardware) — home furnishing
    "SBUX":  "XLY",   # Starbucks — restaurants
    "SE":    "XLY",   # Sea Limited — SE Asia e-commerce/gaming
    "TGT":   "XLY",   # Target — retail
    "TJX":   "XLY",   # TJX Companies — off-price retail
    "TSCO":  "XLY",   # Tractor Supply — specialty retail
    "UBER":  "XLY",   # Uber — rideshare/delivery
    "ULTA":  "XLY",   # Ulta Beauty — specialty retail
    "WSM":   "XLY",   # Williams-Sonoma — home furnishing

    # ── XLI — Industrials ─────────────────────────────────────────────────────
    "B":     "XLI",   # Barnes Group — industrial components
    "CPRT":  "XLI",   # Copart — auto auction
    "CSX":   "XLI",   # CSX — railroad
    "D":     "XLI",   # Dominion Energy — utilities (XLU technically)
    "GD":    "XLI",   # General Dynamics — aerospace/defense
    "GE":    "XLI",   # GE Aerospace — jet engines/defense
    "GEV":   "XLI",   # GE Vernova — power/energy infrastructure
    "JCI":   "XLI",   # Johnson Controls — building tech
    "LDOS":  "XLI",   # Leidos — defense/IT services
    "LEN":   "XLI",   # Lennar — homebuilding
    "LMT":   "XLI",   # Lockheed Martin — defense
    "MLM":   "XLI",   # Martin Marietta — construction materials
    "MOS":   "XLI",   # Mosaic — fertilizers (XLB technically)
    "NVO":   "XLI",   # Novo Nordisk — pharma (XLV technically, listed here as proxy)
    "PWR":   "XLI",   # Quanta Services — electrical/energy infra
    "RTX":   "XLI",   # RTX (Raytheon) — defense/aerospace
    "STLD":  "XLI",   # Steel Dynamics — steel
    "TDG":   "XLI",   # TransDigm — aerospace components
    "URI":   "XLI",   # United Rentals — equipment rental

    # ── XLB — Materials ───────────────────────────────────────────────────────
    "ECL":   "XLB",   # Ecolab — specialty chemicals
    "LYB":   "XLB",   # LyondellBasell — chemicals
    "SHW":   "XLB",   # Sherwin-Williams — paints/coatings

    # ── XLC — Communication Services ─────────────────────────────────────────
    "FWONK": "XLC",   # Liberty Formula One — media/entertainment
    "TMUS":  "XLC",   # T-Mobile — wireless
    "VZ":    "XLC",   # Verizon — wireless

    # ── XLP — Consumer Staples ────────────────────────────────────────────────
    "HSY":   "XLP",   # Hershey — food
    "MKC":   "XLP",   # McCormick — food/spices

    # ── XLRE — Real Estate ────────────────────────────────────────────────────
    "TPL":   "XLRE",  # Texas Pacific Land — land/royalties

    # ── XLU — Utilities ───────────────────────────────────────────────────────
    # (none in current universe)
}
