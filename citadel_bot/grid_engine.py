"""
grid_engine.py — Coarse-Bayesian support/resistance strategy.

Based on Teeple (2025), "Support, Resistance, and Technical Trading" (SSRN 3667920).

Model
-----
Limited-attention "coarse Bayesian" retail traders discretise prices into an
equally-spaced grid {..., 0, ε, 2ε, ...} with midpoint posteriors
{ε/2, 3ε/2, 5ε/2, ...}. Within any regime [iε, (i+1)ε]:

  - Lower half [iε, iε+ε/2]: coarse traders round expectations UP to the
    midpoint → believe asset is underpriced → BUY → prices submartingale.
  - Upper half [iε+ε/2, (i+1)ε]: traders round DOWN → believe asset is
    overpriced → SELL → prices supermartingale.

Donaldson & Kim (1993) formalise this as: real support/resistance exists at
spacing ε if Cov^mod(Δp_{t+1}, mod(p_t, ε) − ε/2) < 0 over the ergodic
distribution of mod(p, ε), with statistical significance vs. permutation null.

Two emitted signal modes:
  - MEAN_REVERT: trade toward the regime midpoint when price sits in either half.
  - RANGE_BREAK: trade in the direction of a fresh cross of a grid line
    (Brock-Lakonishok-LeBaron trading-range-break). Takes precedence over
    mean-reversion within the same tick.

Stops and targets are grid-anchored with a small ATR pad so stops are not
sniped exactly at the level.
"""

import asyncio
import json
import logging
import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

from citadel_bot.config import BotConfig
from citadel_bot.database.database_manager import db_manager
from citadel_bot.utils.logger import get_logger

log = get_logger("grid")

GRID_SPACINGS_FILE = "grid_spacings.json"
GRID_DIAGNOSTICS_FILE = "grid_calibration_diagnostics.json"
EMITTER_STATS_FILE = "emitter_stats.json"


# ─────────────────────────────────────────────────────────────────────────────
# Shared TradeSignal (consumed by RiskManager + ExecutionEngine unchanged)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TradeSignal:
    sym: str
    direction: str         # "LONG" | "SHORT"
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    confidence: float      # 0–1, |normalised Cov^mod| (significance proxy)
    rr_ratio: float
    signal_label: str      # "MEAN_REVERT" | "RANGE_BREAK"
    atr: float


@dataclass
class GridLocation:
    eps: float
    grid_below: float
    grid_above: float
    midpoint: float
    regime_position: float  # (price - grid_below) / eps, in [0, 1)


# ─────────────────────────────────────────────────────────────────────────────
# ATR helper (Wilder, used only for stop padding)
# ─────────────────────────────────────────────────────────────────────────────

def compute_atr(df: pd.DataFrame, period: int) -> float:
    """Wilder ATR on the latest bars. Returns 0.0 if insufficient data."""
    if df is None or len(df) < period + 1:
        return 0.0
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(com=period - 1, adjust=False).mean().iloc[-1]
    return float(atr) if not np.isnan(atr) else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Calibrator: find ε per instrument via the Donaldson-Kim Cov^mod test
# ─────────────────────────────────────────────────────────────────────────────

class GridCalibrator:
    """
    Picks an empirically-supported ε for each instrument.

    For each candidate ε:
      cov_mod = Cov(Δp_{t+1}, mod(p_t, ε) − ε/2)
    Negative cov ⇒ submartingale in lower half and supermartingale in upper
    half ⇒ real S&R per Donaldson & Kim (1993).

    Significance: permutation test (shuffle Δp_{t+1}, recompute cov 500×, the
    p-value is the fraction of shuffles with cov ≤ observed cov).
    """

    def __init__(self, config: BotConfig):
        self.config = config
        # sym → chosen ε (0.0 = no significant ε, sym is rejected)
        self.epsilons: Dict[str, float] = {sym: 0.0 for sym in config.instruments}
        # last calibration diagnostics per sym
        self.diagnostics: Dict[str, dict] = {}
        self._db_available = False
        # Wall-clock timestamp of the most recent successful calibration pass.
        # Used by the main loop to decide when to recalibrate (config.grid_recalibration_days).
        self.last_calibration_at: Optional[datetime] = None

    # ── DB / file initialisation ─────────────────────────────────────

    async def initialize_db(self):
        self._db_available = await db_manager.health_check()
        if self._db_available:
            await self._load_from_db()
            log.info("Grid calibrator: database ready")
        else:
            self._load_from_file()
            log.warning("Grid calibrator: database unavailable, using JSON fallback")

    def _load_from_file(self):
        if not os.path.exists(GRID_SPACINGS_FILE):
            return
        try:
            with open(GRID_SPACINGS_FILE, "r") as f:
                saved = json.load(f)
            for sym in self.config.instruments:
                if sym in saved:
                    self.epsilons[sym] = float(saved[sym])
                    log.info("[%s] Loaded grid ε=%.6f from file", sym, self.epsilons[sym])
        except Exception as exc:
            log.warning("Failed to load grid spacings from file: %s", exc)

    async def _load_from_db(self):
        try:
            for sym in self.config.instruments:
                eps = await db_manager.get_optimal_grid_spacing(sym)
                if eps and eps > 0:
                    self.epsilons[sym] = float(eps)
                    log.info("[%s] Loaded grid ε=%.6f from database", sym, eps)
        except Exception as exc:
            log.warning("Failed to load grid spacings from database: %s", exc)

    async def _save(self):
        if self._db_available:
            await self._save_to_db()
        else:
            self._save_to_file()

    def _save_to_file(self):
        try:
            with open(GRID_SPACINGS_FILE, "w") as f:
                json.dump(self.epsilons, f, indent=2)
            log.info("Saved grid spacings to %s", GRID_SPACINGS_FILE)
        except Exception as exc:
            log.error("Failed to save grid spacings to file: %s", exc)
        try:
            with open(GRID_DIAGNOSTICS_FILE, "w") as f:
                json.dump(self.diagnostics, f, indent=2, default=str)
        except Exception as exc:
            log.error("Failed to save grid diagnostics: %s", exc)

    async def _save_to_db(self):
        for sym, diag in self.diagnostics.items():
            try:
                instrument_id = await db_manager.get_instrument_id(sym)
                if not instrument_id:
                    log.warning("[%s] Instrument missing in DB, skipping grid persist", sym)
                    continue
                await db_manager.save_grid_calibration({
                    "instrument_id": instrument_id,
                    "run_timestamp": diag.get("timestamp", pd.Timestamp.now(tz="UTC")),
                    "candidates": diag.get("candidates", []),
                    "cov_mod_by_candidate": diag.get("cov_by_candidate", []),
                    "pvalue_by_candidate": diag.get("pvalue_by_candidate", []),
                    "epsilon": float(self.epsilons.get(sym, 0.0)),
                    "cov_mod": diag.get("best_cov", 0.0),
                    "p_value": diag.get("best_pvalue", 1.0),
                    "is_significant": diag.get("best_pvalue", 1.0) < diag.get(
                        "effective_threshold", self.config.grid_min_significance
                    ),
                    "n_bars": diag.get("n_bars", 0),
                })
            except Exception as exc:
                log.error("[%s] Failed to save grid calibration to DB: %s", sym, exc)

    # ── Candidate selection per asset class ──────────────────────────

    def _candidates_for(self, sym: str) -> List[float]:
        try:
            from citadel_bot.utils.instrument_catalog import get_instrument
            info = get_instrument(sym)
            category = info.category if info else "indices"
        except Exception:
            category = "indices"

        if category == "indices":
            return list(self.config.grid_candidates_indices)
        if category == "forex":
            return list(self.config.grid_candidates_forex)
        if category == "crypto":
            return list(self.config.grid_candidates_crypto)
        if category == "commodities":
            return list(self.config.grid_candidates_commodities)
        return list(self.config.grid_candidates_indices)

    # ── Main calibration loop (called from main.start()) ─────────────

    async def calibrate(self, pipeline):
        """Run Cov^mod test for each instrument; pick most-negative significant ε."""
        for sym in self.config.instruments:
            df = await pipeline.get_realtime(sym)
            if df is None or len(df) < 500:
                log.warning("[%s] Not enough history (have=%d) to calibrate grid; instrument disabled.",
                            sym, 0 if df is None else len(df))
                self.epsilons[sym] = 0.0
                continue

            closes = pd.to_numeric(df["close"], errors="coerce").dropna().to_numpy(dtype=float)
            candidates = self._candidates_for(sym)

            best_eps, best_cov, best_p, diag = await self._best_epsilon(closes, candidates)
            threshold = self._effective_threshold(len(candidates))
            self.diagnostics[sym] = {
                "timestamp": pd.Timestamp.now(tz="UTC"),
                "candidates": candidates,
                "cov_by_candidate": diag["covs"],
                "pvalue_by_candidate": diag["pvals"],
                "best_cov": float(best_cov),
                "best_pvalue": float(best_p),
                "effective_threshold": float(threshold),
                "correction_method": self.config.grid_correction_method,
                "n_bars": int(len(closes)),
            }

            if best_p < threshold and best_eps > 0:
                self.epsilons[sym] = best_eps
                log.info("[%s] Grid calibrated -> eps=%.6f | cov_mod=%.4g | p=%.4f (threshold=%.4f, %s)",
                         sym, best_eps, best_cov, best_p, threshold,
                         self.config.grid_correction_method)
            else:
                self.epsilons[sym] = 0.0
                log.warning(
                    "[%s] No significant ε after %s correction "
                    "(best ε=%.6f cov=%.4g p=%.4f, threshold=%.4f). Instrument will not be traded.",
                    sym, self.config.grid_correction_method,
                    best_eps, best_cov, best_p, threshold,
                )

            await asyncio.sleep(0)

        self.last_calibration_at = datetime.now(timezone.utc)
        await self._save()

    def _effective_threshold(self, n_candidates: int) -> float:
        """Apply the configured multiple-comparisons correction to grid_min_significance."""
        alpha = float(self.config.grid_min_significance)
        method = (self.config.grid_correction_method or "none").lower()
        if method == "bonferroni" and n_candidates > 0:
            return alpha / n_candidates
        return alpha

    async def _best_epsilon(
        self, closes: np.ndarray, candidates: List[float]
    ) -> Tuple[float, float, float, dict]:
        covs: List[float] = []
        pvals: List[float] = []
        for eps in candidates:
            cov = self._compute_cov_mod(closes, eps)
            covs.append(cov)
            if cov >= 0:
                pvals.append(1.0)
            else:
                pvals.append(await self._permutation_test(closes, eps, cov))
            await asyncio.sleep(0)

        # Pick most-negative cov among candidates whose p passes the corrected threshold;
        # fall back to most-negative cov overall (with its p) when none pass.
        threshold = self._effective_threshold(len(candidates))
        eligible = [(c, p, e) for c, p, e in zip(covs, pvals, candidates) if p < threshold]
        pool = eligible if eligible else list(zip(covs, pvals, candidates))
        best = min(pool, key=lambda triple: triple[0])
        best_cov, best_p, best_eps = best
        return best_eps, best_cov, best_p, {"covs": covs, "pvals": pvals}

    @staticmethod
    def _compute_cov_mod(closes: np.ndarray, eps: float) -> float:
        """Cov(Δp_{t+1}, mod(p_t, ε) − ε/2). Negative = support/resistance present."""
        if eps <= 0 or len(closes) < 50:
            return 0.0
        prices = closes[:-1]
        deltas = np.diff(closes)  # Δp_{t+1} = p_{t+1} − p_t aligned with prices
        mod = np.mod(prices, eps) - eps / 2.0
        if mod.std() == 0 or deltas.std() == 0:
            return 0.0
        return float(np.cov(deltas, mod, ddof=0)[0, 1])

    async def _permutation_test(
        self, closes: np.ndarray, eps: float, observed_cov: float, n: int = 500
    ) -> float:
        """Fraction of shuffles with cov ≤ observed_cov (left-tail p-value)."""
        if eps <= 0 or len(closes) < 50:
            return 1.0
        prices = closes[:-1]
        deltas = np.diff(closes)
        mod = np.mod(prices, eps) - eps / 2.0

        rng = np.random.default_rng(42)
        count_le = 0
        deltas_shuf = deltas.copy()
        for i in range(n):
            rng.shuffle(deltas_shuf)
            cov = float(np.cov(deltas_shuf, mod, ddof=0)[0, 1])
            if cov <= observed_cov:
                count_le += 1
            if i % 100 == 99:
                await asyncio.sleep(0)
        return count_le / n


# ─────────────────────────────────────────────────────────────────────────────
# Emitter performance tracker — disables an emitter (per symbol) when its
# trailing realised P&L is structurally negative. Re-enables automatically.
# ─────────────────────────────────────────────────────────────────────────────

class EmitterPerformanceTracker:
    """
    Tracks per-(sym, signal_mode) closed-trade P&L over a rolling 30-day window.

    Kill switch: an emitter is disabled when BOTH gates are true:
      - rolling 30-day realised P&L < config.emitter_kill_threshold_usd
      - closed trades in window >= config.emitter_min_trades_for_kill

    Once losses age out of the 30-day window and the rolling sum turns positive
    (or the trade count drops below the floor), the emitter auto-re-enables.

    Persisted to data/emitter_stats.json so kill state survives bot restarts.
    """

    WINDOW_DAYS = 30

    def __init__(self, config: BotConfig):
        self.config = config
        self._stats: Dict[Tuple[str, str], deque] = {}
        self._stats_path = Path(config.data_dir) / EMITTER_STATS_FILE
        self._load()

    def record(self, sym: str, signal_mode: str, pnl: float):
        if not sym or not signal_mode:
            return
        key = (sym, str(signal_mode).upper())
        if key not in self._stats:
            self._stats[key] = deque(maxlen=500)
        self._stats[key].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "pnl": float(pnl),
        })
        self._save()

    def is_enabled(self, sym: str, signal_mode: str) -> bool:
        key = (sym, str(signal_mode).upper())
        history = self._stats.get(key)
        if not history:
            return True

        cutoff = datetime.now(timezone.utc) - timedelta(days=self.WINDOW_DAYS)
        recent = []
        for entry in history:
            try:
                ts = datetime.fromisoformat(entry["ts"])
            except (KeyError, ValueError):
                continue
            if ts >= cutoff:
                recent.append(entry)

        if len(recent) < self.config.emitter_min_trades_for_kill:
            return True
        total_pnl = sum(float(e.get("pnl", 0.0)) for e in recent)
        if total_pnl < self.config.emitter_kill_threshold_usd:
            log.warning(
                "[%s] Emitter %s killed: 30d P&L=%.2f over %d trades (threshold=%.2f)",
                sym, signal_mode, total_pnl, len(recent),
                self.config.emitter_kill_threshold_usd,
            )
            return False
        return True

    def snapshot(self) -> Dict[str, Dict[str, dict]]:
        """For dashboards: per-sym, per-mode summary of the rolling window."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.WINDOW_DAYS)
        out: Dict[str, Dict[str, dict]] = {}
        for (sym, mode), history in self._stats.items():
            recent = []
            for e in history:
                try:
                    if datetime.fromisoformat(e["ts"]) >= cutoff:
                        recent.append(e)
                except (KeyError, ValueError):
                    continue
            pnl = sum(float(e.get("pnl", 0.0)) for e in recent)
            out.setdefault(sym, {})[mode] = {
                "trades": len(recent),
                "pnl_30d": round(pnl, 2),
                "enabled": self.is_enabled(sym, mode),
            }
        return out

    def _save(self):
        try:
            self._stats_path.parent.mkdir(parents=True, exist_ok=True)
            serialisable = {
                f"{sym}|{mode}": list(history)
                for (sym, mode), history in self._stats.items()
            }
            with open(self._stats_path, "w") as f:
                json.dump(serialisable, f, indent=2)
        except Exception as exc:
            log.warning("Failed to persist emitter stats: %s", exc)

    def _load(self):
        if not self._stats_path.exists():
            return
        try:
            with open(self._stats_path, "r") as f:
                raw = json.load(f)
            for key, entries in raw.items():
                if "|" not in key:
                    continue
                sym, mode = key.split("|", 1)
                self._stats[(sym, mode)] = deque(entries, maxlen=500)
            log.info("Loaded emitter stats for %d (sym, mode) keys", len(self._stats))
        except Exception as exc:
            log.warning("Failed to load emitter stats: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Signal generator: emits MEAN_REVERT or RANGE_BREAK trade signals
# ─────────────────────────────────────────────────────────────────────────────

class GridSignalGenerator:

    def __init__(
        self,
        config: BotConfig,
        calibrator: GridCalibrator,
        emitter_tracker: Optional[EmitterPerformanceTracker] = None,
    ):
        self.config = config
        self.calibrator = calibrator
        self.emitter_tracker = emitter_tracker or EmitterPerformanceTracker(config)
        # Rolling window of recent closes per symbol (range-break + regime monitor)
        self._recent_closes: Dict[str, deque] = {}
        # Bar-counter cooldown: sym → bar index of last emitted signal
        self._last_signal_bar: Dict[str, int] = {}
        self._bar_counter: Dict[str, int] = {}
        # Symbols suspended by the live regime monitor (cleared on recalibration).
        self._suspended: Set[str] = set()
        # Throttle the regime check — it runs at most once every N bars per sym.
        self._regime_check_every_bars = 50
        self._last_regime_check_bar: Dict[str, int] = {}

    def tick(self, sym: str):
        self._bar_counter[sym] = self._bar_counter.get(sym, 0) + 1

    def reset_suspensions(self):
        """Called after a recalibration completes — give every symbol another shot."""
        if self._suspended:
            log.info("Clearing %d regime-suspension(s) after recalibration: %s",
                     len(self._suspended), sorted(self._suspended))
        self._suspended.clear()

    def locate(self, sym: str, price: float) -> Optional[GridLocation]:
        eps = self.calibrator.epsilons.get(sym, 0.0)
        if eps <= 0 or price <= 0:
            return None
        grid_below = (price // eps) * eps
        grid_above = grid_below + eps
        midpoint = grid_below + eps / 2.0
        regime_position = (price - grid_below) / eps
        return GridLocation(
            eps=eps,
            grid_below=grid_below,
            grid_above=grid_above,
            midpoint=midpoint,
            regime_position=regime_position,
        )

    def generate(self, sym: str, df: pd.DataFrame) -> Tuple[Optional[TradeSignal], str]:
        """
        Returns (signal_or_None, rejection_reason).

        rejection_reason is a short tag for logging; empty string when a signal
        is returned.
        """
        if df is None or df.empty:
            return None, "NO_DATA"

        cfg = self.config
        price = float(df["close"].iloc[-1])
        loc = self.locate(sym, price)
        if loc is None:
            return None, "EPSILON_NOT_SET"

        # Update the rolling close window used by both range-break and regime check
        self._update_recent_closes(sym, df)

        # Live regime monitor — suspend the symbol if Cov^mod has drifted toward zero
        if sym not in self._suspended and self._regime_break_detected(sym, loc.eps):
            self._suspended.add(sym)
        if sym in self._suspended:
            return None, "REGIME_BREAK"

        # Cooldown to avoid hammering a single regime
        current_bar = self._bar_counter.get(sym, 0)
        last_bar = self._last_signal_bar.get(sym, -10_000)
        if current_bar - last_bar < cfg.signal_cooldown_bars:
            return None, "COOLDOWN"

        atr = compute_atr(df, cfg.atr_period_for_stops)
        if atr <= 0:
            atr = price * 0.001  # fallback: 10 bps of price

        # ── Range-break has priority over mean-reversion ──────────────
        # Each emitter is independently gated by the kill switch.
        # NB: cooldown is armed by the caller via mark_executed(sym) AFTER
        # the order is placed, not here — so risk-rejected signals don't
        # burn the cooldown window and block subsequent attempts.
        rb_signal = self._range_break_signal(sym, loc, price, atr)
        if rb_signal is not None:
            if not self.emitter_tracker.is_enabled(sym, "RANGE_BREAK"):
                return None, "EMITTER_KILLED_RANGE_BREAK"
            return rb_signal, ""

        mr_signal = self._mean_revert_signal(sym, loc, price, atr)
        if mr_signal is not None:
            if not self.emitter_tracker.is_enabled(sym, "MEAN_REVERT"):
                return None, "EMITTER_KILLED_MEAN_REVERT"
            return mr_signal, ""

        return None, "DEAD_ZONE"

    def mark_executed(self, sym: str):
        """
        Arm the per-symbol cooldown after the bracket order has been
        successfully placed at the broker. Call from the orchestrator so
        risk-rejected signals don't burn the cooldown window.
        """
        self._last_signal_bar[sym] = self._bar_counter.get(sym, 0)

    # ── Recent-close tracking + regime monitor ───────────────────────

    def _update_recent_closes(self, sym: str, df: pd.DataFrame):
        window = max(self.config.regime_monitor_window, self.config.range_break_lookback + 5)
        if sym not in self._recent_closes:
            self._recent_closes[sym] = deque(maxlen=window)
        bucket = self._recent_closes[sym]
        # Keep only NEW bars (most recent N). For a fresh symbol, seed with the tail.
        if not bucket:
            tail = pd.to_numeric(df["close"], errors="coerce").dropna().tail(window).tolist()
            bucket.extend(float(x) for x in tail)
        else:
            latest = float(df["close"].iloc[-1])
            if bucket[-1] != latest:
                bucket.append(latest)

    def _regime_break_detected(self, sym: str, eps: float) -> bool:
        """Throttled rolling Cov^mod check vs the calibrated value."""
        if not self.config.regime_monitor_enabled:
            return False
        bucket = self._recent_closes.get(sym)
        if not bucket or len(bucket) < self.config.regime_monitor_window // 2:
            return False
        current_bar = self._bar_counter.get(sym, 0)
        last_check = self._last_regime_check_bar.get(sym, -10_000)
        if current_bar - last_check < self._regime_check_every_bars:
            return False
        self._last_regime_check_bar[sym] = current_bar

        calibrated_cov = self.calibrator.diagnostics.get(sym, {}).get("best_cov", 0.0)
        if calibrated_cov >= 0:
            # Should not happen for a chosen ε, but guard anyway.
            return False

        recent_cov = GridCalibrator._compute_cov_mod(np.array(bucket, dtype=float), eps)
        # If recent cov is more than (1 - regime_break_threshold) of the calibrated value
        # away from negative-infinity (i.e., closer to zero than the threshold allows),
        # we treat the regime as broken.
        ratio = recent_cov / calibrated_cov if calibrated_cov != 0 else 0.0
        if ratio < self.config.regime_break_threshold:
            log.warning(
                "[%s] Regime drift detected: recent cov_mod=%.4g vs calibrated %.4g "
                "(ratio=%.2f < threshold=%.2f). Suspending until next recalibration.",
                sym, recent_cov, calibrated_cov, ratio, self.config.regime_break_threshold,
            )
            return True
        return False

    # ── Range-break emitter ──────────────────────────────────────────

    def _range_break_signal(
        self,
        sym: str,
        loc: GridLocation,
        price: float,
        atr: float,
    ) -> Optional[TradeSignal]:
        """
        Detect a fresh grid-line cross confirmed by N consecutive closes beyond it.

        For an UP break:
          - The first close among the last `range_break_lookback` bars must be at or
            below grid_below (we were "in" the lower regime).
          - The last `range_break_confirmation_closes` closes must all be strictly
            above grid_below (we've broken out and stayed out).
        Symmetric for DOWN breaks.
        """
        bucket = self._recent_closes.get(sym)
        if not bucket:
            return None

        lookback = max(self.config.range_break_lookback, self.config.range_break_confirmation_closes + 1)
        confirm = max(1, self.config.range_break_confirmation_closes)
        if len(bucket) < lookback:
            return None

        window = list(bucket)[-lookback:]
        baseline = window[0]
        tail = window[-confirm:]

        # Up break: baseline at/below the lower line; tail entirely above it
        if baseline <= loc.grid_below and all(x > loc.grid_below for x in tail):
            entry = price
            tp1 = loc.midpoint
            tp2 = loc.grid_above
            sl = loc.grid_below - self.config.atr_sl_buffer * atr
            return self._build_signal(sym, "LONG", entry, sl, tp1, tp2, loc, atr,
                                       label="RANGE_BREAK")
        # Down break: baseline at/above the upper line; tail entirely below it
        if baseline >= loc.grid_above and all(x < loc.grid_above for x in tail):
            entry = price
            tp1 = loc.midpoint
            tp2 = loc.grid_below
            sl = loc.grid_above + self.config.atr_sl_buffer * atr
            return self._build_signal(sym, "SHORT", entry, sl, tp1, tp2, loc, atr,
                                       label="RANGE_BREAK")
        return None

    # ── Mean-reversion emitter ───────────────────────────────────────

    def _mean_revert_signal(
        self,
        sym: str,
        loc: GridLocation,
        price: float,
        atr: float,
    ) -> Optional[TradeSignal]:
        edge_dz = self.config.grid_edge_dead_zone
        mid_dz = self.config.grid_dead_zone

        # Lower half: long toward midpoint
        if edge_dz <= loc.regime_position <= 0.5 - mid_dz:
            entry = price
            tp1 = loc.midpoint
            tp2 = loc.grid_above
            sl = loc.grid_below - self.config.atr_sl_buffer * atr
            return self._build_signal(sym, "LONG", entry, sl, tp1, tp2, loc, atr,
                                       label="MEAN_REVERT")
        # Upper half: short toward midpoint
        if 0.5 + mid_dz <= loc.regime_position <= 1 - edge_dz:
            entry = price
            tp1 = loc.midpoint
            tp2 = loc.grid_below
            sl = loc.grid_above + self.config.atr_sl_buffer * atr
            return self._build_signal(sym, "SHORT", entry, sl, tp1, tp2, loc, atr,
                                       label="MEAN_REVERT")
        return None

    # ── Signal assembly + confidence ─────────────────────────────────

    def _build_signal(
        self,
        sym: str,
        direction: str,
        entry: float,
        sl: float,
        tp1: float,
        tp2: float,
        loc: GridLocation,
        atr: float,
        label: str,
    ) -> Optional[TradeSignal]:
        # Validate geometry before returning
        if direction == "LONG" and not (sl < entry < tp1 <= tp2):
            return None
        if direction == "SHORT" and not (tp2 <= tp1 < entry < sl):
            return None

        risk = abs(entry - sl)
        reward = abs(tp2 - entry)
        if risk <= 0:
            return None
        rr = reward / risk

        # Confidence: |normalised cov_mod| of the chosen ε, in [0,1].
        # Cov scales with eps^2 (variance of the mod uniform); divide to normalise.
        diag = self.calibrator.diagnostics.get(sym, {})
        cov = diag.get("best_cov", 0.0)
        eps_var = (loc.eps ** 2) / 12.0 if loc.eps > 0 else 1.0
        confidence = float(min(1.0, abs(cov) / eps_var)) if eps_var > 0 else 0.5

        return TradeSignal(
            sym=sym,
            direction=direction,
            entry=round(entry, 6),
            stop_loss=round(sl, 6),
            tp1=round(tp1, 6),
            tp2=round(tp2, 6),
            confidence=confidence,
            rr_ratio=round(rr, 2),
            signal_label=label,
            atr=round(atr, 6),
        )
