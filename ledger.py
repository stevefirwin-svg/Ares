"""
Ares Position Ledger
====================
Tracks open and closed positions keyed by engine_id.

Each engine maintains its own book. No two engines may hold the same symbol
simultaneously — enforced via symbol_ownership.json (cross-engine lock).

Usage:
    from ledger import Ledger
    ledger = Ledger()
    ledger.record_entry("B", "CVX", 20, 207.13, "2026-05-29")
    ledger.record_entry("A", "AAPL", 50, 185.00, "2026-05-29")
    ledger.get_positions("B")       # Only Engine B positions
    ledger.get_positions("A")       # Only Engine A positions

Position key format: "{engine_id}:{symbol}"  e.g. "B:CVX"

Ownership lock:
    record_entry() writes engine_id → symbol into symbol_ownership.json.
    record_exit()  removes the symbol from symbol_ownership.json (full exit only).
    record_trim()  does NOT release ownership (partial trim, position still open).
"""

import json
import os
from datetime import datetime
from typing import Dict, List, Optional

LEDGER_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ares_position_ledger.json")
OWNERSHIP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "symbol_ownership.json")

VALID_ENGINES = {"A", "B", "C", "D", "E", "F"}


class Ledger:

    def __init__(self, path: str = LEDGER_FILE, ownership_path: str = OWNERSHIP_FILE):
        self.path           = path
        self.ownership_path = ownership_path
        self.data           = self._load()

    # ── I/O ───────────────────────────────────────────────────────────────────

    def _load(self) -> Dict:
        if os.path.exists(self.path):
            with open(self.path, "r") as f:
                return json.load(f)
        return {"positions": {}, "closed": []}

    def _save(self):
        """Atomic write — tmp + os.replace prevents corruption on crash."""
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2, default=str)
        os.replace(tmp, self.path)

    def _load_ownership(self) -> Dict:
        if os.path.exists(self.ownership_path):
            with open(self.ownership_path, "r") as f:
                return json.load(f)
        return {}

    def _save_ownership(self, ownership: Dict):
        """Atomic write for symbol_ownership.json."""
        tmp = self.ownership_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(ownership, f, indent=2)
        os.replace(tmp, self.ownership_path)

    # ── Ownership helpers ─────────────────────────────────────────────────────

    def get_symbol_owner(self, symbol: str) -> Optional[str]:
        """Return the engine_id that owns this symbol, or None if unowned."""
        return self._load_ownership().get(symbol)

    def is_symbol_owned(self, symbol: str) -> bool:
        return symbol in self._load_ownership()

    # ── Entry / Exit ──────────────────────────────────────────────────────────

    def record_entry(self, engine_id: str, symbol: str, shares: int,
                     entry_price: float, date: str, metadata: Dict = None):
        """
        Record a new position entry.

        Raises ValueError if:
          - engine_id is not in VALID_ENGINES
          - symbol is already owned by another engine
        """
        if engine_id not in VALID_ENGINES:
            raise ValueError(f"[Ledger] Unknown engine_id '{engine_id}'. Must be one of {VALID_ENGINES}.")

        ownership = self._load_ownership()
        current_owner = ownership.get(symbol)
        if current_owner and current_owner != engine_id:
            raise ValueError(
                f"[Ledger] Symbol {symbol} already owned by Engine {current_owner}. "
                f"Engine {engine_id} cannot enter."
            )

        key = f"{engine_id}:{symbol}"
        self.data["positions"][key] = {
            "engine_id":   engine_id,
            "symbol":      symbol,
            "shares":      shares,
            "entry_price": entry_price,
            "entry_date":  date,
            "metadata":    metadata or {},
        }
        self._save()

        # Claim ownership
        ownership[symbol] = engine_id
        self._save_ownership(ownership)

    def record_exit(self, engine_id: str, symbol: str, exit_price: float,
                    date: str, reason: str):
        """
        Record a full position exit and move to closed list.
        Releases symbol ownership on full exit.
        """
        key = f"{engine_id}:{symbol}"
        if key not in self.data["positions"]:
            return None

        pos = self.data["positions"].pop(key)
        pos["exit_price"]  = exit_price
        pos["exit_date"]   = date
        pos["exit_reason"] = reason
        pos["exit_path"]   = reason   # outcome_tracker compatibility
        pos["pnl"]         = round((exit_price - pos["entry_price"]) * pos["shares"], 2)
        pos["pnl_pct"]     = round(((exit_price / pos["entry_price"]) - 1) * 100, 4)
        self.data["closed"].append(pos)
        self._save()

        # Release ownership only on full exit
        ownership = self._load_ownership()
        ownership.pop(symbol, None)
        self._save_ownership(ownership)

        return pos

    def record_trim(self, engine_id: str, symbol: str, shares_sold: int,
                    trim_price: float, date: str, reason: str):
        """
        Record a partial position trim — reduces shares held, keeps position open.
        Does NOT release symbol ownership (position still open).

        Returns the updated position dict, or None if key not found.
        """
        key = f"{engine_id}:{symbol}"
        if key not in self.data["positions"]:
            return None

        pos = self.data["positions"][key]
        shares_before = pos.get("shares", 0)
        shares_after  = max(0, shares_before - shares_sold)

        trim_pnl_pct = round(((trim_price / pos["entry_price"]) - 1) * 100, 4)
        trim_pnl_abs = round((trim_price - pos["entry_price"]) * shares_sold, 2)

        trim_record = {
            "date":          date,
            "reason":        reason,
            "shares_sold":   shares_sold,
            "trim_price":    trim_price,
            "pnl_pct":       trim_pnl_pct,
            "pnl_abs":       trim_pnl_abs,
            "shares_before": shares_before,
            "shares_after":  shares_after,
        }
        pos.setdefault("trims", []).append(trim_record)
        pos["shares"] = shares_after

        if shares_after == 0:
            # All shares trimmed away — treat as full exit, release ownership
            # RF-24: pnl must sum ALL realized trims, not just reprice the final tranche
            # from entry_price (which ignores prior partial realizations at different prices).
            all_trims = pos.get("trims", [])  # includes the trim we just appended
            total_pnl_abs = round(sum(t["pnl_abs"] for t in all_trims), 2)
            # pnl_pct: total realized $ / (entry_price × original_shares)
            original_shares = shares_before  # before this trim = last remaining
            # Sum original shares from trims chain (first trim's shares_before is original)
            if all_trims:
                original_shares_total = all_trims[0]["shares_before"]
            else:
                original_shares_total = shares_before
            cost_basis = pos["entry_price"] * original_shares_total
            total_pnl_pct = round((total_pnl_abs / cost_basis) * 100, 4) if cost_basis > 0 else 0.0

            pos["exit_price"]  = trim_price
            pos["exit_date"]   = date
            pos["exit_reason"] = reason
            pos["exit_path"]   = reason
            pos["pnl_pct"]     = total_pnl_pct    # RF-24: summed across all tranches
            pos["pnl"]         = total_pnl_abs    # RF-24: summed across all tranches
            self.data["positions"].pop(key)
            self.data["closed"].append(pos)

            ownership = self._load_ownership()
            ownership.pop(symbol, None)
            self._save_ownership(ownership)

        self._save()
        return pos

    def update_metadata(self, engine_id: str, symbol: str, updates: Dict):
        """
        Merge `updates` into an open position's metadata dict.
        Used to set flags mid-trade (e.g. measured_move_trimmed for Engine F).
        No-op if position key not found.
        """
        key = f"{engine_id}:{symbol}"
        if key not in self.data["positions"]:
            return
        self.data["positions"][key].setdefault("metadata", {}).update(updates)
        self._save()

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_positions(self, engine_id: str) -> List[Dict]:
        """Get all open positions for a specific engine."""
        return [v for v in self.data["positions"].values() if v["engine_id"] == engine_id]

    def get_held_symbols(self, engine_id: str) -> set:
        """Get set of symbols currently held by a specific engine."""
        return {v["symbol"] for v in self.data["positions"].values() if v["engine_id"] == engine_id}

    def get_all_held_symbols(self) -> set:
        """Get all held symbols across all engines."""
        return {v["symbol"] for v in self.data["positions"].values()}

    def get_closed(self, engine_id: str = None) -> List[Dict]:
        """Get closed trades, optionally filtered by engine."""
        if engine_id:
            return [t for t in self.data["closed"] if t.get("engine_id") == engine_id]
        return self.data["closed"]

    def get_engine_capital_deployed(self, engine_id: str) -> float:
        """
        Return total market value of open positions for this engine.
        Uses entry_price × shares as a conservative estimate (no live price needed).
        """
        return sum(
            p["entry_price"] * p["shares"]
            for p in self.get_positions(engine_id)
        )

    def get_performance_summary(self, engine_id: str) -> Dict:
        """Quick P&L summary for an engine."""
        closed = self.get_closed(engine_id)
        if not closed:
            return {"trades": 0, "net_pnl": 0.0, "win_rate": 0.0, "avg_pnl": 0.0}
        winners   = [t for t in closed if t.get("pnl", 0) > 0]
        total_pnl = sum(t.get("pnl", 0) for t in closed)
        return {
            "trades":   len(closed),
            "winners":  len(winners),
            "win_rate": round(len(winners) / len(closed) * 100, 1),
            "net_pnl":  round(total_pnl, 2),
            "avg_pnl":  round(total_pnl / len(closed), 2),
        }

    def print_status(self):
        """Print per-engine status table."""
        all_engines = set(
            v["engine_id"] for v in self.data["positions"].values()
        ) | set(
            t.get("engine_id") for t in self.data["closed"] if t.get("engine_id")
        )

        ownership = self._load_ownership()

        print("=" * 70)
        print("  ARES LEDGER STATUS")
        print("=" * 70)
        print(f"  Symbol ownership lock: {len(ownership)} symbols claimed")
        print()

        for eid in sorted(all_engines):
            positions = self.get_positions(eid)
            perf      = self.get_performance_summary(eid)
            deployed  = self.get_engine_capital_deployed(eid)
            print(f"  Engine {eid}")
            print(f"    Open positions: {len(positions)}  deployed=${deployed:,.0f}")
            for p in positions:
                print(f"      {p['symbol']:6s}  {p['shares']} shares @ ${p['entry_price']:.2f}  ({p['entry_date']})")
            print(f"    Closed trades:  {perf['trades']}")
            print(f"    Win rate:       {perf['win_rate']}%")
            print(f"    Net P&L:        ${perf['net_pnl']:,.2f}")
            print()

        print("=" * 70)


if __name__ == "__main__":
    ledger = Ledger()
    ledger.print_status()
