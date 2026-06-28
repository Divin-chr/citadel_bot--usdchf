"""
calibrate_costs.py — empirically back-fit backtest cost-model constants from
recent MetaApi deals.

For each instrument with at least N recent fills:
  - Compare every fill price against the mid-quote at the deal timestamp
    (sourced from the local market_data Postgres table or the CSV cache).
  - Aggregate per-instrument empirical spread and per-side slippage.
  - Print a `per_instrument` YAML block the user can paste into config.yaml.

This is a one-shot helper — the backtest defaults stay parametric; the user
reviews the printed numbers and decides whether to commit them.

Usage:
    python scripts/calibrate_costs.py [--days 30] [--min-fills 5]
"""

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from citadel_bot.config import BotConfig
from citadel_bot.database.database_manager import db_manager, init_database, close_database


def _mid_quote_at(sym: str, ts: datetime, df: Optional[pd.DataFrame]) -> Optional[float]:
    """Find the OHLC bar whose timestamp brackets ts, return its (open+close)/2."""
    if df is None or df.empty:
        return None
    try:
        idx = df.index.get_indexer([ts], method="nearest")[0]
    except Exception:
        return None
    if idx < 0 or idx >= len(df):
        return None
    row = df.iloc[idx]
    return float((row["open"] + row["close"]) / 2.0)


def _load_local_bars(sym: str, data_dir: str) -> Optional[pd.DataFrame]:
    """Prefer m1, fall back to h1 / d1 CSVs persisted by DataPipeline."""
    base = Path(data_dir) / "market_data"
    for suffix in ["_m1.csv", "_h1.csv", "_d1.csv"]:
        path = base / f"{sym}{suffix}"
        if path.exists():
            try:
                df = pd.read_csv(path, index_col=0, parse_dates=True)
                df.columns = [c.lower() for c in df.columns]
                if "open" in df.columns and "close" in df.columns:
                    return df.sort_index()
            except Exception:
                continue
    return None


async def _fetch_recent_deals(days: int) -> List[dict]:
    """
    Query MetaApi deals from the live connection's history_storage.

    This script does NOT spin up a MetaApi connection itself — it expects the
    main bot to be (or have recently been) running so the SDK's local deal
    cache is warm. We read whatever the SDK serialised to disk under .metaapi/.
    """
    metaapi_dir = PROJECT_ROOT / ".metaapi"
    if not metaapi_dir.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    deals: List[dict] = []
    try:
        import json
        # MetaApi serialises deals as <accountId>-MetaApi-deals.bin; format is
        # SDK-internal. If we can't parse it, fall back to printing zero deals
        # and let the user run with --days to widen the window.
        for path in metaapi_dir.glob("*-MetaApi-deals.bin"):
            try:
                raw = path.read_bytes()
                # The SDK uses a custom binary format; we look for JSON blobs
                # delimited by braces as a lightweight extraction.
                text = raw.decode("utf-8", errors="ignore")
                start = text.find("[{")
                end = text.rfind("}]")
                if start >= 0 and end > start:
                    chunk = text[start: end + 2]
                    deals.extend(json.loads(chunk))
            except Exception:
                continue
    except Exception:
        return []

    parsed: List[dict] = []
    for d in deals:
        try:
            ts = d.get("time")
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if not isinstance(ts, datetime):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
            parsed.append({
                "symbol": d.get("symbol"),
                "time": ts,
                "price": float(d.get("price") or 0.0),
                "volume": float(d.get("volume") or 0.0),
                "entryType": d.get("entryType"),
                "type": d.get("type"),
                "profit": float(d.get("profit") or 0.0),
            })
        except Exception:
            continue
    return parsed


async def calibrate(days: int, min_fills: int) -> Dict[str, Dict[str, float]]:
    config = BotConfig.from_file("config.yaml")

    # Best-effort DB hookup; we fall back to CSV if Postgres isn't reachable.
    db_ok = False
    try:
        await init_database({
            "database_url": config.database_url,
            "host": config.database_host,
            "port": config.database_port,
            "database": config.database_name,
            "user": config.database_user,
            "password": config.database_password,
        })
        db_ok = await db_manager.health_check()
    except Exception:
        db_ok = False

    deals = await _fetch_recent_deals(days)
    if not deals:
        print(
            f"\n  No deals found in the last {days} days under .metaapi/ "
            "(the local SDK cache may be empty or in a format this script can't parse). "
            "Try running the bot for longer, or widen with --days.\n"
        )
        if db_ok:
            await close_database()
        return {}

    per_symbol_bars: Dict[str, Optional[pd.DataFrame]] = {}

    def _bars(sym: str) -> Optional[pd.DataFrame]:
        if sym not in per_symbol_bars:
            per_symbol_bars[sym] = _load_local_bars(sym, config.data_dir)
        return per_symbol_bars[sym]

    grouped: Dict[str, List[float]] = defaultdict(list)
    for d in deals:
        sym = d.get("symbol")
        if not sym:
            continue
        mid = _mid_quote_at(sym, d["time"], _bars(sym))
        if mid is None or mid <= 0 or d["price"] <= 0:
            continue
        slip_pts = abs(d["price"] - mid)
        grouped[sym].append(slip_pts)

    overrides: Dict[str, Dict[str, float]] = {}
    for sym, slips in grouped.items():
        if len(slips) < min_fills:
            continue
        s = pd.Series(slips)
        spread_estimate = round(float(s.median() * 2), 4)   # mid-to-fill ≈ half-spread
        slippage_estimate = round(float(s.quantile(0.75) - s.median()), 4)
        overrides[sym] = {
            "backtest_spread_pts": spread_estimate,
            "backtest_slippage_pts": slippage_estimate,
            "n_fills": len(slips),
        }

    if db_ok:
        await close_database()
    return overrides


def _emit_yaml(overrides: Dict[str, Dict[str, float]]):
    if not overrides:
        return
    print("\n# ─── Recommended per-instrument cost overrides (paste into config.yaml) ───\n")
    block = {"per_instrument": {
        sym: {k: v for k, v in d.items() if k != "n_fills"} for sym, d in overrides.items()
    }}
    print(yaml.dump(block, default_flow_style=False, sort_keys=False))
    print("# Fill counts per symbol:")
    for sym, d in overrides.items():
        print(f"#   {sym}: {int(d['n_fills'])} fills")
    print()


async def main():
    parser = argparse.ArgumentParser(description="Calibrate backtest cost-model from MetaApi deals")
    parser.add_argument("--days", type=int, default=30,
                        help="Look back this many days for deal history.")
    parser.add_argument("--min-fills", type=int, default=5,
                        help="Require at least this many fills per instrument before emitting.")
    args = parser.parse_args()

    overrides = await calibrate(args.days, args.min_fills)
    _emit_yaml(overrides)


if __name__ == "__main__":
    asyncio.run(main())
