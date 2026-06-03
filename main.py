"""
CardIntel Phase 1 — main.py
Orchestrates: ingest → score → alert → save → email digest

Usage:
    python main.py              # Run one scan now
    python main.py --schedule   # Run daily at time set in config.py
    python main.py --test       # Score from mock data (no network calls)
    python main.py --report     # Show last saved scores (no new scan)
"""

import sys
import logging
import json
import argparse
from datetime import datetime
from pathlib import Path

# ─── Logging setup ────────────────────────────────────────────────────────
Path("logs").mkdir(exist_ok=True)
log_file = f"logs/cardintel_{datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)


from config import WATCHLIST, DAILY_SCAN_TIME
from ingestion import ingest_all
from scorer import score_all
from watchlist import init_db, save_scan, get_all_latest_scores
from alert_engine import run_alerts, build_html_report, send_email


# ─── Core scan pipeline ───────────────────────────────────────────────────

def run_scan(verbose: bool = True) -> list[dict]:
    """Full pipeline: ingest → score → alert → save → email."""
    logger.info("=" * 60)
    logger.info("CardIntel Phase 1 — Scan started")
    logger.info("=" * 60)
    
    # 1. Ingest
    logger.info(f"Ingesting {len(WATCHLIST)} cards from eBay and PSA...")
    enriched = ingest_all()
    
    # 2. Score
    logger.info("Scoring all cards...")
    scored = score_all(enriched)
    
    # 3. Save to DB
    save_scan(scored)
    
    # 4. Alerts
    logger.info("Evaluating alert conditions...")
    alerts = run_alerts(scored)
    
    # 5. Daily digest email
    subject = (
        f"CardIntel Digest — {datetime.now().strftime('%b %d')} — "
        f"{sum(1 for c in scored if c.get('in_buy_zone'))} in buy zone"
    )
    html = build_html_report(scored, alerts)
    send_email(subject, html)
    
    # 6. Console summary
    if verbose:
        print_summary(scored, alerts)
    
    return scored


def run_test_scan() -> list[dict]:
    """Score with mock price data — no network calls. For testing config."""
    logger.info("TEST MODE — using mock price data")
    
    mock_cards = []
    for card in WATCHLIST:
        threshold = card.get("buy_threshold", 1000)
        # Simulate price at 92% of buy threshold (in buy zone)
        mock_price = threshold * 0.92
        mock_cards.append({
            **card,
            "price_stats": {
                "avg_price": round(mock_price, 2),
                "median_price": round(mock_price, 2),
                "min_price": round(mock_price * 0.9, 2),
                "max_price": round(mock_price * 1.1, 2),
                "sale_count": 7,
                "price_30d_avg": round(mock_price, 2),
            },
            "psa_data": {"grade_10_pop": None, "total_pop": None, "source": "mock"},
            "ingested_at": datetime.now().isoformat(),
        })
    
    scored = score_all(mock_cards)
    alerts = run_alerts(scored)
    print_summary(scored, alerts)
    return scored


def show_saved_report():
    """Print the last saved scores from DB without running a new scan."""
    rows = get_all_latest_scores()
    if not rows:
        print("No saved scans found. Run 'python main.py' first.")
        return
    
    print("\n" + "=" * 70)
    print("CardIntel — Last Saved Scores")
    print("=" * 70)
    print(f"{'Score':>6}  {'Signal':12}  {'Avg Price':>12}  {'BZ':>3}  Card")
    print("-" * 70)
    for r in rows:
        avg = f"${r['avg_price']:>10,.0f}" if r["avg_price"] else f"{'—':>11}"
        bz = "✓" if r["in_buy_zone"] else " "
        scanned = r["scanned_at"][:16]
        print(f"{r['score']:>6.1f}  {r['signal']:12}  {avg}  {bz:>3}  {r['card_name']}")
    print(f"\nLast scan: {rows[0]['scanned_at'][:16] if rows else 'never'}")


def print_summary(scored: list[dict], alerts: list[dict]):
    """Console-friendly summary table."""
    print("\n" + "=" * 70)
    print("CardIntel — Scan Complete")
    print("=" * 70)
    print(f"{'Score':>6}  {'Signal':12}  {'Avg Price':>12}  {'Sales':>5}  Card")
    print("-" * 70)
    
    for card in scored:
        stats = card.get("price_stats", {})
        avg = stats.get("avg_price")
        count = stats.get("sale_count", 0)
        avg_str = f"${avg:>10,.0f}" if avg else f"{'—':>11}"
        bz = " ← BUY ZONE" if card.get("in_buy_zone") else ""
        print(f"{card['score']:>6.1f}  {card.get('signal','?'):12}  {avg_str}  {count:>5}  {card['name']}{bz}")
    
    if alerts:
        print(f"\n{'─'*70}")
        print(f"ALERTS FIRED: {len(alerts)}")
        for a in sorted(alerts, key=lambda x: x["urgency"]):
            tag = f"[{a['type']}]"
            print(f"  {a['urgency']:6} {tag:15} {a['card']}")
            print(f"         └─ {a['message']}")
    
    buy_zone = [c for c in scored if c.get("in_buy_zone")]
    if buy_zone:
        print(f"\n🟢 IN BUY ZONE ({len(buy_zone)} cards):")
        for c in buy_zone:
            avg = c.get("price_stats", {}).get("avg_price")
            tgt = c.get("target_price")
            upside = f"+{((tgt-avg)/avg*100):.0f}% to target" if avg and tgt else ""
            print(f"   • {c['name']} — ${avg:,.0f}  {upside}")
    
    print(f"\nLog saved to: {log_file}")
    print("=" * 70)


# ─── Scheduler ───────────────────────────────────────────────────────────

def run_scheduled():
    """Run the scan on a daily schedule."""
    try:
        import schedule
        import time
    except ImportError:
        logger.error("Install schedule: pip install schedule --break-system-packages")
        return
    
    logger.info(f"Scheduler started — daily scan at {DAILY_SCAN_TIME}")
    schedule.every().day.at(DAILY_SCAN_TIME).do(run_scan)
    
    # Run once immediately
    run_scan()
    
    while True:
        schedule.run_pending()
        time.sleep(60)


# ─── Entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CardIntel Phase 1")
    parser.add_argument("--schedule", action="store_true", help="Run on daily schedule")
    parser.add_argument("--test",     action="store_true", help="Test with mock data")
    parser.add_argument("--report",   action="store_true", help="Show last saved report")
    args = parser.parse_args()
    
    init_db()
    
    if args.report:
        show_saved_report()
    elif args.test:
        run_test_scan()
    elif args.schedule:
        run_scheduled()
    else:
        run_scan()
