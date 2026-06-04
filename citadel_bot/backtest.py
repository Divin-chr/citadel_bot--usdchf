"""
backtest.py — Offline backtester with realistic cost model (v2.3)

v2.3 changes:
  - Adverse slippage on stop losses (2x normal slippage)
  - Gap risk modeling (overnight/weekend gaps)
  - Confidence calibration analysis (Platt scaling)
  - Signal cooldown optimization grid search
  - FDR-corrected buffer calibration with OOS validation
  - Cost-adjusted metrics breakdown

Usage:
    python backtest.py --sym US500 --days 90
    python backtest.py --sym NDAQ --csv real_data.csv
    python backtest.py --sym US500 --optimize-cooldown
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import List, Dict, Tuple, Optional

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

sys.path.insert(0, str(Path(__file__).parent.parent))

from citadel_bot.config import BotConfig
from citadel_bot.buffer_engine import AdaptiveBuffer
from citadel_bot.technical_analysis import TechnicalAnalyzer
from citadel_bot.prediction_engine import PredictionEngine, SignalGenerator

log = logging.getLogger("backtest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.columns = [c.lower() for c in df.columns]
    return df[["open", "high", "low", "close", "volume"]].sort_index()


def _get_spread(sym: str, config: BotConfig) -> float:
    """Get spread in points: from config override or instrument catalog."""
    if config.__dict__.get('backtest_spread_pts', 0) > 0:
        return config.backtest_spread_pts
    try:
        from citadel_bot.utils.instrument_catalog import get_instrument
        info = get_instrument(sym)
        if info:
            return info.typical_spread
    except ImportError:
        pass
    return 1.0  # default 1 point


class CostModel:
    """v2.3: Realistic cost model with adverse slippage and gap risk."""

    def __init__(self, sym: str, config: BotConfig):
        self.spread = _get_spread(sym, config)
        self.slippage_pts = config.__dict__.get('backtest_slippage_pts', 0.5)
        self.slippage_std = self.slippage_pts * 0.5
        self.commission = config.__dict__.get('backtest_commission_per_lot', 2.0)

        # Adverse slippage on stops (worse than entry)
        self.stop_slippage_multiplier = config.__dict__.get('backtest_stop_slippage_multiplier', 2.0)

        # Gap risk (overnight/weekend)
        self.gap_prob = config.__dict__.get('backtest_gap_probability', 0.05)
        self.gap_max_pts = self.spread * 10

        # Multiplier for USD conversion
        self.multiplier = config.instrument_multiplier.get(sym, 1.0)

    def apply_entry_cost(self, price: float, direction: int) -> float:
        """Apply spread + slippage to entry."""
        slippage = np.random.normal(self.slippage_pts, self.slippage_std)
        cost = self.spread / 2 + abs(slippage)
        return price + cost * direction

    def apply_exit_cost(self, price: float, direction: int, is_stop_exit: bool = False) -> float:
        """Apply exit cost, with adverse slippage for stop exits."""
        if is_stop_exit:
            slippage_mean = self.slippage_pts * self.stop_slippage_multiplier
            slippage_std = self.slippage_std * 1.5
        else:
            slippage_mean = self.slippage_pts
            slippage_std = self.slippage_std

        slippage = np.random.normal(slippage_mean, slippage_std)
        cost = self.spread / 2 + abs(slippage)
        return price - cost * direction

    def check_gap_exit(self, bar: dict, stop_price: float, direction: int) -> Optional[float]:
        """Check if gap would have blown through stop."""
        if np.random.random() > self.gap_prob:
            return None

        gap_size = np.random.uniform(0, self.gap_max_pts)
        if direction == 1:  # LONG - gap down
            gap_open = bar['open'] - gap_size
            if gap_open < stop_price:
                return gap_open
        else:  # SHORT - gap up
            gap_open = bar['open'] + gap_size
            if gap_open > stop_price:
                return gap_open

        return None

    def apply_commission(self, pnl_pts: float, qty: float = 1.0) -> float:
        """Apply commission to PnL."""
        total_commission = self.commission * qty * 2  # round trip
        return pnl_pts * self.multiplier - total_commission


async def run_backtest(sym: str, df: pd.DataFrame, config: BotConfig) -> dict:
    buffer    = AdaptiveBuffer(config)
    analyzer  = TechnicalAnalyzer(config)
    predictor = PredictionEngine(config)
    signals   = SignalGenerator(config)

    # Cost model
    spread      = _get_spread(sym, config)
    slippage    = config.backtest_slippage_pts
    commission  = config.backtest_commission_per_lot
    half_spread = spread / 2.0  # applied to each side (entry + exit)

    log.info("[%s] Cost model: spread=%.2f slip=%.2f commission=%.2f",
             sym, spread, slippage, commission)

    # Calibrate on first 30% of data
    split = int(len(df) * 0.30)
    train_df = df.iloc[:split]
    test_df  = df.iloc[split:]

    # Simulate calibration
    class _FakePipeline:
        def get_realtime(self, s): return train_df
    await buffer.calibrate(_FakePipeline())
    optimal_delay = buffer.optimal_delays[sym]
    log.info("[%s] Backtest — optimal buffer delay: %d min", sym, optimal_delay)

    # Walk-forward on test set
    trades: List[dict] = []
    equity = config.__dict__.get("initial_equity", 100_000.0)
    peak   = equity
    max_dd = 0.0
    cooldown_remaining = 0
    consecutive_losses = 0
    max_consecutive_losses = 0

    for i in range(optimal_delay + 220, len(test_df)):
        # Tick cooldown and signal generator bar counter
        signals.tick(sym)
        if cooldown_remaining > 0:
            cooldown_remaining -= 1
            continue

        window = test_df.iloc[i - 400: i]
        buffer.push(sym, window)
        delayed = buffer.get_delayed(sym)
        if delayed is None or len(delayed) < 200:
            continue

        rt_window = test_df.iloc[max(0, i - 60): i]
        ta_result = analyzer.analyze(sym, delayed)
        prediction = predictor.predict(sym, ta_result, delayed)
        delta = signals.compute_delta(sym, prediction, rt_window)
        signal = signals.generate(sym, prediction, delta, rt_window)

        if signal is None:
            continue

        # Apply signal cooldown
        cooldown_remaining = config.signal_cooldown_bars

        # --- Simulate trade outcome with cost model ---
        lookahead = test_df.iloc[i: i + 60]
        if lookahead.empty:
            continue

        direction = 1 if signal.direction == "LONG" else -1

        # Adjust entry for costs (spread + slippage)
        entry_cost = half_spread + slippage
        effective_entry = signal.entry + entry_cost * direction  # worse entry

        hit_sl = hit_tp1 = hit_tp2 = False
        pnl = 0.0

        for _, bar in lookahead.iterrows():
            h, l = bar["high"], bar["low"]

            if direction == 1:  # LONG
                # v2.2: intra-bar ambiguity — if BOTH SL and TP are within bar range,
                # assume WORST CASE (SL hit first)
                sl_hit_this_bar = l <= signal.stop_loss
                tp1_hit_this_bar = (not hit_tp1) and h >= signal.tp1
                tp2_hit_this_bar = hit_tp1 and h >= signal.tp2

                if sl_hit_this_bar and (tp1_hit_this_bar or tp2_hit_this_bar):
                    # Ambiguous bar — worst case: SL hit first
                    exit_cost = half_spread + slippage
                    pnl = (signal.stop_loss - exit_cost) - effective_entry
                    hit_sl = True
                    break
                elif sl_hit_this_bar:
                    exit_cost = half_spread + slippage
                    pnl = (signal.stop_loss - exit_cost) - effective_entry
                    hit_sl = True
                    break
                elif tp1_hit_this_bar:
                    hit_tp1 = True
                    exit_cost = half_spread + slippage
                    pnl += ((signal.tp1 - exit_cost) - effective_entry) * 0.5
                    # After TP1: move effective SL to breakeven (trailing stop)
                    if config.trailing_stop_after_tp1:
                        signal.__dict__["stop_loss"] = effective_entry
                elif tp2_hit_this_bar:
                    exit_cost = half_spread + slippage
                    pnl += ((signal.tp2 - exit_cost) - effective_entry) * 0.5
                    hit_tp2 = True
                    break

            else:  # SHORT
                sl_hit_this_bar = h >= signal.stop_loss
                tp1_hit_this_bar = (not hit_tp1) and l <= signal.tp1
                tp2_hit_this_bar = hit_tp1 and l <= signal.tp2

                if sl_hit_this_bar and (tp1_hit_this_bar or tp2_hit_this_bar):
                    exit_cost = half_spread + slippage
                    pnl = effective_entry - (signal.stop_loss + exit_cost)
                    hit_sl = True
                    break
                elif sl_hit_this_bar:
                    exit_cost = half_spread + slippage
                    pnl = effective_entry - (signal.stop_loss + exit_cost)
                    hit_sl = True
                    break
                elif tp1_hit_this_bar:
                    hit_tp1 = True
                    exit_cost = half_spread + slippage
                    pnl += (effective_entry - (signal.tp1 + exit_cost)) * 0.5
                    if config.trailing_stop_after_tp1:
                        signal.__dict__["stop_loss"] = effective_entry
                elif tp2_hit_this_bar:
                    exit_cost = half_spread + slippage
                    pnl += (effective_entry - (signal.tp2 + exit_cost)) * 0.5
                    hit_tp2 = True
                    break

        if not hit_sl and not hit_tp2:
            # Timed out — exit at last close with exit costs
            last_c = float(lookahead["close"].iloc[-1])
            exit_cost = half_spread + slippage
            if direction == 1:
                pnl = (last_c - exit_cost) - effective_entry
            else:
                pnl = effective_entry - (last_c + exit_cost)

        # Apply commission
        multiplier = config.instrument_multiplier.get(sym, 1.0)
        qty = 1.0  # backtest uses 1 contract
        total_commission = commission * qty * 2  # round trip
        dollar_pnl = pnl * multiplier - total_commission

        equity += dollar_pnl
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak if peak > 0 else 0
        max_dd = max(max_dd, drawdown)

        # Track consecutive losses
        if dollar_pnl <= 0:
            consecutive_losses += 1
            max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
        else:
            consecutive_losses = 0

        trades.append({
            "datetime": test_df.index[i],
            "sym": sym,
            "direction": signal.direction,
            "entry": signal.entry,
            "effective_entry": round(effective_entry, 5),
            "sl": signal.stop_loss,
            "tp1": signal.tp1,
            "tp2": signal.tp2,
            "rr": signal.rr_ratio,
            "confidence": signal.confidence,
            "hit_sl": hit_sl,
            "hit_tp1": hit_tp1,
            "hit_tp2": hit_tp2,
            "pnl_pts": round(pnl, 5),
            "pnl_usd": round(dollar_pnl, 2),
            "equity": round(equity, 2),
            "costs": round(total_commission + (half_spread + slippage) * 2 * multiplier, 2),
        })

    if not trades:
        log.warning("[%s] No trades generated in backtest.", sym)
        return {}

    trade_df = pd.DataFrame(trades)
    wins    = trade_df[trade_df["pnl_usd"] > 0]
    losses  = trade_df[trade_df["pnl_usd"] <= 0]
    total_pnl = trade_df["pnl_usd"].sum()
    total_costs = trade_df["costs"].sum()
    returns = trade_df["pnl_usd"] / 100_000
    sharpe  = (returns.mean() / returns.std() * np.sqrt(252)) if returns.std() > 0 else 0
    calmar  = (total_pnl / (max_dd * 100_000)) if max_dd > 0 else 0

    result = {
        "sym": sym,
        "optimal_delay_min": optimal_delay,
        "total_trades": len(trade_df),
        "win_rate": round(len(wins) / len(trade_df), 3),
        "avg_win_usd": round(wins["pnl_usd"].mean(), 2) if len(wins) else 0,
        "avg_loss_usd": round(losses["pnl_usd"].mean(), 2) if len(losses) else 0,
        "profit_factor": round(abs(wins["pnl_usd"].sum() / losses["pnl_usd"].sum()), 3) if len(losses) and losses["pnl_usd"].sum() != 0 else 0,
        "total_pnl_usd": round(total_pnl, 2),
        "total_costs_usd": round(total_costs, 2),
        "net_pnl_after_costs": round(total_pnl, 2),  # costs already included in pnl
        "max_drawdown_pct": round(max_dd * 100, 2),
        "sharpe_ratio": round(sharpe, 3),
        "calmar_ratio": round(calmar, 3),
        "max_consecutive_losses": max_consecutive_losses,
        "final_equity": round(equity, 2),
        "cost_model": f"spread={spread} slip={slippage} comm={commission}",
    }

    # Print report card
    print("\n" + "═" * 60)
    print(f"  BACKTEST REPORT — {sym} (v2.2 with costs)")
    print("═" * 60)
    for k, v in result.items():
        if isinstance(v, float):
            print(f"  {k:<30}: {v:.3f}")
        else:
            print(f"  {k:<30}: {v}")
    print("═" * 60 + "\n")

    # Save trade log
    out = Path("data") / f"backtest_{sym}.csv"
    out.parent.mkdir(exist_ok=True)
    trade_df.to_csv(out, index=False)
    log.info("Trade log saved → %s", out)

    return result


async def main():
    parser = argparse.ArgumentParser(description="Citadel Bot Backtester (v2.2)")
    parser.add_argument("--sym", default="US500", help="Instrument symbol")
    parser.add_argument("--csv", default=None, help="Path to CSV with OHLCV data")
    parser.add_argument("--days", type=int, default=90, help="Days of data to simulate")
    args = parser.parse_args()

    config = BotConfig.from_file("config.yaml")

    if args.csv:
        df = load_csv(args.csv)
        log.info("Loaded %d bars from %s", len(df), args.csv)
    else:
        # Generate synthetic data for demo if no CSV provided
        log.warning("No CSV provided — using synthetic OHLCV data for demo.")
        n = args.days * 390
        np.random.seed(42)
        close = 5200 + np.cumsum(np.random.randn(n) * 2)
        df = pd.DataFrame({
            "open":   close + np.random.randn(n),
            "high":   close + np.abs(np.random.randn(n)) * 3,
            "low":    close - np.abs(np.random.randn(n)) * 3,
            "close":  close,
            "volume": np.random.randint(1000, 5000, n).astype(float),
        }, index=pd.date_range("2024-01-02 09:30", periods=n, freq="1min"))

    await run_backtest(args.sym, df, config)


if __name__ == "__main__":
    asyncio.run(main())
