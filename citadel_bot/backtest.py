"""
backtest.py — Offline backtester for the grid strategy (Teeple 2025).

Walk-forward with rolling recalibration:
  - Split the series into N+1 equal chunks of `fold_size` bars.
  - Fold k: calibrate ε on bars [0 : k*fold_size); trade on bars
    [k*fold_size : (k+1)*fold_size). Roll forward; ε is rebuilt each fold
    on an expanding window so the backtest mirrors live recalibration cadence.
  - B&H benchmark over the full out-of-sample window is reported alongside
    so strategy edge can be judged against just holding the instrument.

Usage:
    python citadel_bot/backtest.py --sym US500 --days 365 --folds 12
    python citadel_bot/backtest.py --sym US500 --csv my_data.csv --folds 12
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from citadel_bot.config import BotConfig
from citadel_bot.grid_engine import GridCalibrator, GridSignalGenerator

log = logging.getLogger("backtest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

STARTING_EQUITY = 100_000.0
LOOKAHEAD_BARS = 240  # cap each trade at 4 hours (m1)


# ─────────────────────────────────────────────────────────────────────────────
# Data + cost helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.columns = [c.lower() for c in df.columns]
    return df[["open", "high", "low", "close", "volume"]].sort_index()


def _get_spread(sym: str, config: BotConfig) -> float:
    if config.backtest_spread_pts > 0:
        return config.backtest_spread_pts
    try:
        from citadel_bot.utils.instrument_catalog import get_instrument
        info = get_instrument(sym)
        if info:
            return info.typical_spread
    except ImportError:
        pass
    return 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward fold construction
# ─────────────────────────────────────────────────────────────────────────────

class _ReplayPipeline:
    """Pipeline shim that returns a fixed DataFrame slice to GridCalibrator."""
    def __init__(self, df: pd.DataFrame):
        self.df = df

    async def get_realtime(self, sym: str) -> pd.DataFrame:
        return self.df


def make_folds(df: pd.DataFrame, n_folds: int) -> List[Tuple[pd.DataFrame, pd.DataFrame]]:
    """
    Build expanding-window walk-forward folds.

    Returns a list of (train_df, test_df) pairs. For n_folds == 1 the legacy
    30/70 split is used so single-fold backtests stay comparable.
    For n_folds >= 2: the series is divided into n_folds + 1 equal chunks; the
    first chunk seeds the initial calibration window, and each subsequent chunk
    is traded after recalibrating on every bar seen so far.
    """
    n = len(df)
    if n_folds <= 1:
        split = int(n * 0.30)
        return [(df.iloc[:split], df.iloc[split:])]

    fold_size = n // (n_folds + 1)
    if fold_size < 500:
        max_folds = max(1, n // 500 - 1)
        log.warning("Folds=%d gives %d bars per fold (<500). Falling back to %d folds.",
                    n_folds, fold_size, max_folds)
        n_folds = max_folds
        fold_size = n // (n_folds + 1)

    folds: List[Tuple[pd.DataFrame, pd.DataFrame]] = []
    for k in range(1, n_folds + 1):
        train_end = k * fold_size
        test_end = (k + 1) * fold_size if k < n_folds else n
        folds.append((df.iloc[:train_end], df.iloc[train_end:test_end]))
    return folds


# ─────────────────────────────────────────────────────────────────────────────
# Trade simulation
# ─────────────────────────────────────────────────────────────────────────────

def _simulate_fold(
    sym: str,
    test_df: pd.DataFrame,
    generator: GridSignalGenerator,
    config: BotConfig,
    starting_equity: float,
    peak_so_far: float,
    max_dd_so_far: float,
    spread: float,
    slippage: float,
    commission: float,
) -> Tuple[List[dict], float, float, float]:
    """
    Walk through `test_df` bar by bar, emit signals, simulate fills with the
    cost model, and return (trades, ending_equity, peak, max_dd).
    """
    trades: List[dict] = []
    equity = starting_equity
    peak = peak_so_far
    max_dd = max_dd_so_far
    half_spread = spread / 2.0

    warmup = max(config.atr_period_for_stops + 5, 50)
    multiplier = config.get_multiplier(sym)

    for i in range(warmup, len(test_df) - 1):
        generator.tick(sym)
        window = test_df.iloc[max(0, i - warmup): i + 1]

        signal, _gate = generator.generate(sym, window)
        if signal is None:
            continue

        lookahead = test_df.iloc[i + 1: i + 1 + LOOKAHEAD_BARS]
        if lookahead.empty:
            continue

        direction = 1 if signal.direction == "LONG" else -1
        entry_cost = half_spread + slippage
        effective_entry = signal.entry + entry_cost * direction
        sl_effective = signal.stop_loss
        hit_sl = hit_tp1 = hit_tp2 = False
        pnl = 0.0

        for _, bar in lookahead.iterrows():
            h, low_ = bar["high"], bar["low"]

            if direction == 1:
                sl_hit = low_ <= sl_effective
                tp1_hit = (not hit_tp1) and h >= signal.tp1
                tp2_hit = hit_tp1 and h >= signal.tp2
            else:
                sl_hit = h >= sl_effective
                tp1_hit = (not hit_tp1) and low_ <= signal.tp1
                tp2_hit = hit_tp1 and low_ <= signal.tp2

            if sl_hit:
                exit_cost = half_spread + slippage
                if direction == 1:
                    pnl += (sl_effective - exit_cost - effective_entry) * (0.5 if hit_tp1 else 1.0)
                else:
                    pnl += (effective_entry - sl_effective - exit_cost) * (0.5 if hit_tp1 else 1.0)
                hit_sl = True
                break

            if tp1_hit:
                hit_tp1 = True
                exit_cost = half_spread + slippage
                if direction == 1:
                    pnl += (signal.tp1 - exit_cost - effective_entry) * 0.5
                else:
                    pnl += (effective_entry - signal.tp1 - exit_cost) * 0.5
                if config.trailing_stop_after_tp1:
                    sl_effective = effective_entry
            elif tp2_hit:
                exit_cost = half_spread + slippage
                if direction == 1:
                    pnl += (signal.tp2 - exit_cost - effective_entry) * 0.5
                else:
                    pnl += (effective_entry - signal.tp2 - exit_cost) * 0.5
                hit_tp2 = True
                break

        if not hit_sl and not hit_tp2:
            last_c = float(lookahead["close"].iloc[-1])
            exit_cost = half_spread + slippage
            if direction == 1:
                pnl += (last_c - exit_cost - effective_entry) * (0.5 if hit_tp1 else 1.0)
            else:
                pnl += (effective_entry - last_c - exit_cost) * (0.5 if hit_tp1 else 1.0)

        qty = 1.0
        dollar_pnl = pnl * multiplier - commission * qty * 2

        equity += dollar_pnl
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, drawdown)

        trades.append({
            "datetime": test_df.index[i],
            "sym": sym,
            "mode": signal.signal_label,
            "direction": signal.direction,
            "entry": signal.entry,
            "effective_entry": round(effective_entry, 6),
            "sl": signal.stop_loss,
            "tp1": signal.tp1,
            "tp2": signal.tp2,
            "rr": signal.rr_ratio,
            "confidence": signal.confidence,
            "hit_sl": hit_sl,
            "hit_tp1": hit_tp1,
            "hit_tp2": hit_tp2,
            "pnl_pts": round(pnl, 6),
            "pnl_usd": round(dollar_pnl, 2),
            "equity": round(equity, 2),
        })

    return trades, equity, peak, max_dd


# ─────────────────────────────────────────────────────────────────────────────
# Buy-and-hold benchmark
# ─────────────────────────────────────────────────────────────────────────────

def _benchmark_buy_hold(sym: str, oos_df: pd.DataFrame, config: BotConfig) -> dict:
    """
    Long-only buy-and-hold over the out-of-sample window, sized to risk the
    same notional as the strategy's starting equity. Reports total return,
    Sharpe (annualised from per-bar returns), and max drawdown.
    """
    if oos_df is None or oos_df.empty:
        return {"total_return_pct": 0.0, "sharpe_ratio": 0.0, "max_drawdown_pct": 0.0, "final_equity": STARTING_EQUITY}

    closes = oos_df["close"].astype(float)
    if closes.iloc[0] <= 0:
        return {"total_return_pct": 0.0, "sharpe_ratio": 0.0, "max_drawdown_pct": 0.0, "final_equity": STARTING_EQUITY}

    multiplier = config.get_multiplier(sym)
    qty = STARTING_EQUITY / (closes.iloc[0] * multiplier)
    equity_curve = STARTING_EQUITY + (closes - closes.iloc[0]) * multiplier * qty

    returns = equity_curve.pct_change().dropna()
    sharpe = float(returns.mean() / returns.std() * np.sqrt(252 * 390)) if returns.std() > 0 else 0.0

    peak = equity_curve.cummax()
    drawdowns = (peak - equity_curve) / peak.where(peak > 0, 1.0)
    max_dd = float(drawdowns.max())

    final = float(equity_curve.iloc[-1])
    return {
        "total_return_pct": round((final - STARTING_EQUITY) / STARTING_EQUITY * 100, 2),
        "sharpe_ratio": round(sharpe, 3),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "final_equity": round(final, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Backtest driver
# ─────────────────────────────────────────────────────────────────────────────

async def run_backtest(sym: str, df: pd.DataFrame, config: BotConfig, n_folds: int = 1) -> dict:
    if sym not in config.instruments:
        config.instruments = [sym]

    spread = _get_spread(sym, config)
    slippage = config.backtest_slippage_pts
    commission = config.backtest_commission_per_lot
    log.info("[%s] Cost model: spread=%.4f slip=%.4f commission=%.4f",
             sym, spread, slippage, commission)

    folds = make_folds(df, n_folds)
    log.info("[%s] Walk-forward folds: %d (each test segment ~%d bars)",
             sym, len(folds), len(folds[0][1]) if folds else 0)

    all_trades: List[dict] = []
    fold_summaries: List[dict] = []
    equity = STARTING_EQUITY
    peak = equity
    max_dd = 0.0
    oos_index_start: Optional[pd.Timestamp] = None

    for fold_idx, (train_df, test_df) in enumerate(folds):
        if test_df.empty:
            continue
        if oos_index_start is None:
            oos_index_start = test_df.index[0]

        calibrator = GridCalibrator(config)
        generator = GridSignalGenerator(config, calibrator)
        await calibrator.calibrate(_ReplayPipeline(train_df))
        eps = calibrator.epsilons.get(sym, 0.0)
        diag = calibrator.diagnostics.get(sym, {})

        fold_trades: List[dict] = []
        if eps > 0:
            fold_trades, equity, peak, max_dd = _simulate_fold(
                sym, test_df, generator, config,
                starting_equity=equity, peak_so_far=peak, max_dd_so_far=max_dd,
                spread=spread, slippage=slippage, commission=commission,
            )
            all_trades.extend(fold_trades)
        else:
            log.warning("[%s] Fold %d: no significant ε on %d train bars; sitting out.",
                        sym, fold_idx + 1, len(train_df))

        fold_summaries.append({
            "fold": fold_idx + 1,
            "train_bars": len(train_df),
            "test_bars": len(test_df),
            "epsilon": eps,
            "cov_mod": round(float(diag.get("best_cov", 0.0)), 6),
            "p_value": round(float(diag.get("best_pvalue", 1.0)), 4),
            "threshold": round(float(diag.get("effective_threshold", config.grid_min_significance)), 6),
            "trades": len(fold_trades),
            "pnl_usd": round(sum(t["pnl_usd"] for t in fold_trades), 2),
        })

    # ── Buy-and-hold benchmark over the out-of-sample window ─────────
    if oos_index_start is not None:
        oos_df = df.loc[oos_index_start:]
        benchmark = _benchmark_buy_hold(sym, oos_df, config)
    else:
        benchmark = _benchmark_buy_hold(sym, df, config)

    return _build_report(sym, all_trades, fold_summaries, benchmark, equity, max_dd, spread, slippage, commission)


def _build_report(
    sym: str,
    trades: List[dict],
    folds: List[dict],
    benchmark: dict,
    final_equity: float,
    max_dd: float,
    spread: float,
    slippage: float,
    commission: float,
) -> dict:
    if not trades:
        log.warning("[%s] No trades generated across all folds.", sym)
        report = {
            "sym": sym,
            "total_trades": 0,
            "folds": folds,
            "benchmark_buy_hold": benchmark,
        }
        _print_report(sym, report, final_equity, max_dd, spread, slippage, commission)
        return report

    trade_df = pd.DataFrame(trades)
    wins = trade_df[trade_df["pnl_usd"] > 0]
    losses = trade_df[trade_df["pnl_usd"] <= 0]
    total_pnl = float(trade_df["pnl_usd"].sum())
    returns = trade_df["pnl_usd"] / STARTING_EQUITY
    sharpe = (returns.mean() / returns.std() * np.sqrt(252)) if returns.std() > 0 else 0.0

    mean_revert = trade_df[trade_df["mode"] == "MEAN_REVERT"]
    range_break = trade_df[trade_df["mode"] == "RANGE_BREAK"]

    def _profit_factor(w, l):
        loss_sum = float(l["pnl_usd"].sum())
        if loss_sum == 0:
            return 0.0
        return round(abs(float(w["pnl_usd"].sum()) / loss_sum), 3)

    report = {
        "sym": sym,
        "total_trades": len(trade_df),
        "mean_revert_trades": len(mean_revert),
        "range_break_trades": len(range_break),
        "win_rate": round(len(wins) / len(trade_df), 3),
        "avg_win_usd": round(float(wins["pnl_usd"].mean()), 2) if len(wins) else 0.0,
        "avg_loss_usd": round(float(losses["pnl_usd"].mean()), 2) if len(losses) else 0.0,
        "profit_factor": _profit_factor(wins, losses),
        "total_pnl_usd": round(total_pnl, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "sharpe_ratio": round(float(sharpe), 3),
        "final_equity": round(final_equity, 2),
        "cost_model": f"spread={spread} slip={slippage} comm={commission}",
        "folds": folds,
        "benchmark_buy_hold": benchmark,
    }

    out = Path("data") / f"backtest_{sym}.csv"
    out.parent.mkdir(exist_ok=True)
    trade_df.to_csv(out, index=False)
    log.info("Trade log saved -> %s", out)

    _print_report(sym, report, final_equity, max_dd, spread, slippage, commission)
    return report


def _print_report(sym: str, report: dict, final_equity: float, max_dd: float,
                  spread: float, slippage: float, commission: float):
    print("\n" + "=" * 70)
    print(f"  GRID BACKTEST REPORT - {sym}")
    print("=" * 70)

    folds = report.get("folds", [])
    if folds:
        print("\n  Per-fold calibration & P&L:")
        print(f"  {'fold':>4} {'train':>8} {'test':>8} {'epsilon':>10} {'cov_mod':>10} "
              f"{'p_value':>8} {'thr':>8} {'trades':>7} {'pnl_usd':>10}")
        for f in folds:
            eps_str = f"{f['epsilon']:.6g}" if f['epsilon'] > 0 else "—"
            print(f"  {f['fold']:>4} {f['train_bars']:>8} {f['test_bars']:>8} "
                  f"{eps_str:>10} {f['cov_mod']:>10.4g} {f['p_value']:>8.4f} "
                  f"{f['threshold']:>8.4f} {f['trades']:>7} {f['pnl_usd']:>10.2f}")

    print("\n  Strategy:")
    for k in ["total_trades", "mean_revert_trades", "range_break_trades",
              "win_rate", "avg_win_usd", "avg_loss_usd", "profit_factor",
              "total_pnl_usd", "max_drawdown_pct", "sharpe_ratio", "final_equity"]:
        v = report.get(k, 0)
        if isinstance(v, float):
            print(f"    {k:<24}: {v:.6g}")
        else:
            print(f"    {k:<24}: {v}")

    bench = report.get("benchmark_buy_hold", {})
    if bench:
        print("\n  Buy & Hold benchmark (same OOS window):")
        for k, v in bench.items():
            print(f"    {k:<24}: {v}")

        strat_return_pct = (final_equity - STARTING_EQUITY) / STARTING_EQUITY * 100
        bench_return = bench.get("total_return_pct", 0.0)
        edge = strat_return_pct - bench_return
        verdict = "BEATS B&H" if edge > 0 else "UNDERPERFORMS B&H"
        print(f"\n  Strategy return: {strat_return_pct:+.2f}%  |  B&H: {bench_return:+.2f}%  |  Edge: {edge:+.2f}%  ({verdict})")

    print(f"\n  Cost model: spread={spread} slippage={slippage} commission={commission}")
    print("=" * 70 + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic generator for sanity tests
# ─────────────────────────────────────────────────────────────────────────────

def _synthetic_grid_series(n: int, eps: float, p0: float, sigma: float, pull: float, seed: int = 42) -> pd.DataFrame:
    """
    p_{t+1} = p_t + noise − pull · (mod(p_t, ε) − ε/2)
    Stronger `pull` ⇒ stronger S&R bouncing inside each [iε, (i+1)ε] regime.
    """
    rng = np.random.default_rng(seed)
    closes = np.empty(n)
    closes[0] = p0
    for i in range(1, n):
        prev = closes[i - 1]
        mod_dev = (prev % eps) - eps / 2.0
        closes[i] = prev + rng.normal(0, sigma) - pull * mod_dev
    high = closes + np.abs(rng.normal(0, sigma * 0.5, n))
    low = closes - np.abs(rng.normal(0, sigma * 0.5, n))
    op = closes + rng.normal(0, sigma * 0.3, n)
    vol = rng.integers(1000, 5000, n).astype(float)
    return pd.DataFrame({
        "open": op, "high": high, "low": low, "close": closes, "volume": vol,
    }, index=pd.date_range("2024-01-02 09:30", periods=n, freq="1min"))


async def main():
    parser = argparse.ArgumentParser(description="Citadel Bot — Grid Backtester (walk-forward)")
    parser.add_argument("--sym", default="US500", help="Instrument symbol")
    parser.add_argument("--csv", default=None, help="Path to CSV (datetime,open,high,low,close,volume)")
    parser.add_argument("--days", type=int, default=90, help="Days of synthetic data when no CSV")
    parser.add_argument("--folds", type=int, default=12,
                        help="Walk-forward folds. 1 = legacy 30/70 split. Default 12 ≈ monthly.")
    parser.add_argument("--synthetic-eps", type=float, default=10.0,
                        help="ε used when generating synthetic data (sanity test)")
    parser.add_argument("--synthetic-pull", type=float, default=0.05,
                        help="Mean-reversion pull coefficient in synthetic data")
    args = parser.parse_args()

    config = BotConfig.from_file("config.yaml")

    if args.csv:
        df = load_csv(args.csv)
        log.info("Loaded %d bars from %s", len(df), args.csv)
    else:
        log.warning("No CSV provided — using synthetic grid series (sanity check).")
        n = args.days * 390
        df = _synthetic_grid_series(
            n=n, eps=args.synthetic_eps, p0=5000.0, sigma=2.0, pull=args.synthetic_pull,
        )

    await run_backtest(args.sym, df, config, n_folds=args.folds)


if __name__ == "__main__":
    asyncio.run(main())
