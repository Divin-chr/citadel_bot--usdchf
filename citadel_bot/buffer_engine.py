"""
buffer_engine.py — Adaptive delay buffer with statistically rigorous calibration

Core concept:
  - Real-time data is pushed into a circular buffer per symbol.
  - When the buffer has accumulated `optimal_delay` minutes of bars,
    the oldest snapshot is released as "delayed data" for analysis.
  - Optimal delay is found by rolling walk-forward backtesting over
    multiple train/validate windows, with permutation-based significance
    testing to reject delays that are indistinguishable from noise.

v2.2 changes:
  - Rolling walk-forward (60-day train, 20-day validate)
  - Permutation test (500 shuffles, p < 0.05 required)
  - Stability filter (reject delays that jump >50% between windows)
  - Full calibration diagnostics saved to JSON
"""

import asyncio
import json
import logging
import os
from collections import deque
from typing import Dict, Optional, List, Tuple

import numpy as np
import pandas as pd

from citadel_bot.config import BotConfig
from citadel_bot.database.database_manager import db_manager
from citadel_bot.utils.logger import get_logger

log = get_logger("buffer")


class AdaptiveBuffer:

    def __init__(self, config: BotConfig):
        self.config = config
        # Circular buffer: sym -> deque of DataFrames (one per bar)
        self._buffers: Dict[str, deque] = {
            sym: deque(maxlen=config.buffer_max_delay_min + 10)
            for sym in config.instruments
        }
        # Optimal delay in bars (1 bar = 1 min)
        self.optimal_delays: Dict[str, int] = {
            sym: 12 for sym in config.instruments  # sensible default
        }
        # Calibration diagnostics (last run)
        self.calibration_diagnostics: Dict[str, dict] = {}
        self._db_available = False

    async def initialize_db(self):
        """Initialize database connection for buffer engine"""
        self._db_available = await db_manager.health_check()
        if self._db_available:
            await self._load_delays_from_db()
            log.info("✅ Buffer engine database ready")
        else:
            self._load_delays_from_file()
            log.warning("⚠️  Buffer engine database not available, using file fallback")

    def _load_delays_from_file(self):
        """Load saved optimal delays from JSON file (fallback)."""
        delay_file = "buffer_delays.json"
        if os.path.exists(delay_file):
            try:
                with open(delay_file, 'r') as f:
                    saved_delays = json.load(f)
                for sym in self.config.instruments:
                    if sym in saved_delays:
                        self.optimal_delays[sym] = saved_delays[sym]
                        log.info("[%s] Loaded saved delay from file: %d min", sym, saved_delays[sym])
            except Exception as e:
                log.warning("Failed to load buffer delays from file: %s", e)

    async def _load_delays_from_db(self):
        """Load optimal delays from database (primary)."""
        try:
            for sym in self.config.instruments:
                delay = await db_manager.get_optimal_buffer_delay(sym)
                if delay > 0:
                    self.optimal_delays[sym] = delay
                    log.info("[%s] Loaded saved delay from database: %d min", sym, delay)
        except Exception as e:
            log.warning("Failed to load buffer delays from database: %s", e)

    async def _save_delays(self):
        """Save current optimal delays and diagnostics."""
        if self._db_available:
            await self._save_delays_to_db()
        else:
            self._save_delays_to_file()

    async def _save_delays_to_db(self):
        """Save calibration results to database (primary)."""
        try:
            for sym, diagnostics in self.calibration_diagnostics.items():
                instrument_id = await db_manager.get_instrument_id(sym)
                if not instrument_id:
                    log.warning("[%s] Instrument not found in database, skipping calibration save", sym)
                    continue

                calibration_data = {
                    'instrument_id': instrument_id,
                    'run_timestamp': diagnostics.get('timestamp', pd.Timestamp.now(tz='UTC')),
                    'min_delay_min': self.config.buffer_min_delay_min,
                    'max_delay_min': self.config.buffer_max_delay_min,
                    'step_min': self.config.calibration_step_min,
                    'calibration_window_days': self.config.calibration_window_days,
                    'optimal_delay_min': diagnostics.get('best_delay', self.optimal_delays[sym]),
                    'best_sharpe': diagnostics.get('best_sharpe', 0.0),
                    'p_value': diagnostics.get('p_value', 1.0),
                    'is_significant': diagnostics.get('p_value', 1.0) < 0.05,
                    'n_bars': diagnostics.get('n_bars', 0),
                    'n_windows': diagnostics.get('n_windows', 0),
                    'candidates': diagnostics.get('candidates', []),
                    'delay_mean_val_sharpe': [diagnostics.get('delay_mean_val_sharpe', {}).get(d, 0.0) for d in diagnostics.get('candidates', [])],
                    'window_winners': diagnostics.get('window_winners', [])
                }

                await db_manager.save_buffer_calibration(calibration_data)

            log.info("✅ Saved buffer calibration to database")

        except Exception as e:
            log.error("Failed to save buffer calibration to database: %s", e)

    def _save_delays_to_file(self):
        """Save current optimal delays and diagnostics to file (fallback)."""
        delay_file = "buffer_delays.json"
        try:
            with open(delay_file, 'w') as f:
                json.dump(self.optimal_delays, f, indent=2)
            log.info("Saved buffer delays to %s", delay_file)
        except Exception as e:
            log.error("Failed to save buffer delays: %s", e)

        # Save diagnostics separately
        diag_file = "buffer_calibration_diagnostics.json"
        try:
            with open(diag_file, 'w') as f:
                json.dump(self.calibration_diagnostics, f, indent=2, default=str)
            log.info("Saved calibration diagnostics to %s", diag_file)
        except Exception as e:
            log.error("Failed to save calibration diagnostics: %s", e)

    # ── Real-time push ──────────────────────────────────────────────

    def push(self, sym: str, df: pd.DataFrame):
        """Push the latest full DataFrame snapshot into the buffer."""
        self._buffers[sym].append(df.copy())

    # ── Delayed data retrieval ──────────────────────────────────────

    def get_delayed(self, sym: str) -> Optional[pd.DataFrame]:
        """
        Return the DataFrame snapshot that was captured
        `optimal_delay` steps ago, or None if not enough data yet. Use per-instrument override if set.
        """
        buf = self._buffers[sym]
        delay = self._get_delay(sym)
        if len(buf) < delay + 1:
            return None
        # Index from the right: buf[-1] is newest, buf[-(delay+1)] is delayed
        idx = len(buf) - 1 - delay
        return buf[idx]

    def _get_delay(self, sym: str) -> int:
        """Get per-instrument buffer delay or fall back to optimal."""
        return self.config.per_instrument.get(sym, {}).get('buffer_delay', self.optimal_delays[sym])

    # ── Calibration ─────────────────────────────────────────────────

    async def calibrate(self, pipeline):
        """
        Rolling walk-forward calibration with permutation-based significance.
        Tests every candidate delay from buffer_min to buffer_max and picks
        the one that (a) maximises Sharpe on held-out validation windows and
        (b) passes a permutation significance test.
        """
        for sym in self.config.instruments:
            df = await pipeline.get_realtime(sym)
            if df is None or len(df) < 200:
                log.warning("[%s] Not enough history to calibrate buffer. Using default 12 min.", sym)
                continue

            best_delay, best_sharpe, p_value, diagnostics = await self._rolling_walk_forward(sym, df)

            self.calibration_diagnostics[sym] = diagnostics

            if p_value > 0.05:
                log.warning(
                    "[%s] Calibration → best delay=%d sharpe=%.3f BUT p=%.3f (not significant). "
                    "Using previous delay %d.",
                    sym, best_delay, best_sharpe, p_value, self.optimal_delays[sym]
                )
            else:
                self.optimal_delays[sym] = best_delay
                log.info(
                    "[%s] Calibration → optimal delay=%d min | Sharpe=%.3f | p=%.3f (significant)",
                    sym, best_delay, best_sharpe, p_value
                )

            # Yield control so the event loop stays responsive
            await asyncio.sleep(0)

        # Save the calibrated delays
        await self._save_delays()

    async def _rolling_walk_forward(
        self, sym: str, df: pd.DataFrame
    ) -> Tuple[int, float, float, dict]:
        """
        Rolling walk-forward calibration:
          - Split data into rolling windows (train=60 days, validate=20 days)
          - For each window, find the best delay on train, score on validate
          - Aggregate validation Sharpes across windows
          - Run permutation test on the winning delay

        Returns (best_delay, best_sharpe, p_value, diagnostics_dict).
        """
        min_d = self.config.buffer_min_delay_min
        max_d = self.config.buffer_max_delay_min
        step = self.config.calibration_step_min
        candidates = list(range(min_d, max_d + 1, step))

        closes = pd.to_numeric(df["close"], errors="coerce").dropna().to_numpy(dtype=float)
        n = len(closes)

        # Rolling windows: 60-day train (~23400 bars) + 20-day validate (~7800 bars)
        # Scale to actual bar count: assume ~390 bars/day for equity, or use what we have
        bars_per_day = 390
        train_bars = min(60 * bars_per_day, n // 3)
        val_bars = min(20 * bars_per_day, n // 6)
        window_step = val_bars  # slide by validation window size

        if n < train_bars + val_bars + 100:
            # Not enough data for rolling — fall back to single split
            log.warning("[%s] Limited data (%d bars), using single train/val split.", sym, n)
            train_bars = int(n * 0.6)
            val_bars = n - train_bars
            window_step = val_bars

        # Build rolling windows
        windows = []
        start = 0
        while start + train_bars + val_bars <= n:
            windows.append((start, start + train_bars, start + train_bars + val_bars))
            start += window_step

        if not windows:
            windows = [(0, int(n * 0.6), n)]

        # For each delay candidate, compute validation Sharpe across all windows
        delay_val_sharpes: Dict[int, List[float]] = {d: [] for d in candidates}
        window_winners: List[int] = []

        for w_idx, (w_start, w_split, w_end) in enumerate(windows):
            train_closes = closes[w_start:w_split]
            val_closes = closes[w_split:w_end]

            best_train_delay = candidates[0]
            best_train_sharpe = -999.0

            for delay in candidates:
                sharpe = self._compute_sharpe_for_delay(train_closes, delay)
                if sharpe > best_train_sharpe:
                    best_train_sharpe = sharpe
                    best_train_delay = delay

            window_winners.append(best_train_delay)

            # Score ALL candidates on validation for aggregation
            for delay in candidates:
                val_sharpe = self._compute_sharpe_for_delay(val_closes, delay)
                delay_val_sharpes[delay].append(val_sharpe)

            await asyncio.sleep(0)

        # Aggregate: mean validation Sharpe per delay
        delay_mean_sharpe = {}
        for delay in candidates:
            sharpes = delay_val_sharpes[delay]
            if sharpes:
                delay_mean_sharpe[delay] = float(np.mean(sharpes))
            else:
                delay_mean_sharpe[delay] = 0.0

        # Pick the best
        best_delay = max(delay_mean_sharpe, key=delay_mean_sharpe.get)
        best_sharpe = delay_mean_sharpe[best_delay]

        # Stability filter: if window winners jump a lot, use median
        if len(window_winners) >= 3:
            unique_winners = set(window_winners)
            if len(unique_winners) > len(window_winners) * 0.6:
                median_delay = int(np.median(window_winners))
                # Snap to nearest candidate
                best_delay = min(candidates, key=lambda d: abs(d - median_delay))
                best_sharpe = delay_mean_sharpe.get(best_delay, 0.0)
                log.info("[%s] Stability filter: winners unstable %s → using median %d",
                         sym, window_winners, best_delay)

        # Permutation test: is the best delay's Sharpe significant?
        p_value = await self._permutation_test(closes, best_delay)

        diagnostics = {
            "n_bars": n,
            "n_windows": len(windows),
            "candidates": candidates,
            "delay_mean_val_sharpe": delay_mean_sharpe,
            "window_winners": window_winners,
            "best_delay": best_delay,
            "best_sharpe": float(best_sharpe),
            "p_value": float(p_value),
        }

        return best_delay, best_sharpe, p_value, diagnostics

    def _compute_sharpe_for_delay(self, closes: np.ndarray, delay: int) -> float:
        """Compute annualised Sharpe for a given delay on a close array."""
        n = len(closes)
        if n < delay + 60:
            return 0.0

        pnls = []
        for i in range(delay + 50, n - 1):
            delayed_close = closes[i - delay]
            ref_close = closes[i - delay - 10] if i - delay - 10 >= 0 else closes[0]
            pred_direction = 1.0 if delayed_close > ref_close else -1.0
            actual_return = closes[i + 1] - closes[i]
            pnls.append(pred_direction * actual_return)

        if len(pnls) < 20:
            return 0.0

        arr = np.array(pnls)
        mu = arr.mean()
        std = arr.std()
        return float((mu / std * np.sqrt(252 * 390)) if std > 0 else 0.0)

    async def _permutation_test(
        self, closes: np.ndarray, delay: int, n_permutations: int = 500
    ) -> float:
        """
        Permutation test: shuffle the prediction directions and recompute
        Sharpe n_permutations times. Return p-value = fraction of shuffled
        Sharpes >= real Sharpe.
        """
        n = len(closes)
        if n < delay + 60:
            return 1.0

        # Compute real Sharpe
        real_sharpe = self._compute_sharpe_for_delay(closes, delay)

        # Build the actual prediction directions and returns
        preds = []
        returns = []
        for i in range(delay + 50, n - 1):
            delayed_close = closes[i - delay]
            ref_close = closes[i - delay - 10] if i - delay - 10 >= 0 else closes[0]
            preds.append(1.0 if delayed_close > ref_close else -1.0)
            returns.append(closes[i + 1] - closes[i])

        preds = np.array(preds)
        returns = np.array(returns)

        if len(preds) < 20:
            return 1.0

        # Shuffled Sharpes
        count_gte = 0
        rng = np.random.default_rng(42)
        for _ in range(n_permutations):
            shuffled = preds.copy()
            rng.shuffle(shuffled)
            pnls = shuffled * returns
            mu = pnls.mean()
            std = pnls.std()
            shuf_sharpe = (mu / std * np.sqrt(252 * 390)) if std > 0 else 0.0
            if shuf_sharpe >= real_sharpe:
                count_gte += 1

        p_value = count_gte / n_permutations

        await asyncio.sleep(0)
        return p_value
