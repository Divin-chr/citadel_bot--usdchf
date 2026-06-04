"""
prediction_engine.py — Prediction + multi-TF delta comparator + signal generator (v2.3)

v2.3 changes:
  - FIXED: Predictions now generated on REAL-TIME data (not delayed)
  - FIXED: Delta confirmation uses ONLY post-prediction momentum (no lookahead)
  - FIXED: Removed Fibonacci from structure scoring (no statistical edge)
  - FIXED: Removed heuristic pattern detection (no demonstrated edge)
  - Confidence passed through for Kelly sizing with calibration support
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from citadel_bot.config import BotConfig
from citadel_bot.technical_analysis import TAResult

log = logging.getLogger("predictor")


@dataclass
class Prediction:
    sym: str
    direction: int      # +1 long, -1 short, 0 flat
    confidence: float   # 0-1 (uncalibrated)
    calibrated_confidence: float = 0.5  # 0-1 (empirically calibrated win probability)
    predicted_move_pct: float = 0.0     # expected % move
    signal: str = "NEUTRAL"
    ta: Optional[TAResult] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Delta:
    """Comparison between prediction (on real-time data) and post-prediction reality."""
    sym: str
    pred_direction: int
    rt_momentum: float      # real-time momentum AFTER prediction (positive = bullish)
    aligned: bool           # prediction agrees with real-time?
    alignment_score: float  # 0-1, how strongly they agree
    # v2.3: multi-timeframe detail (all post-prediction)
    momentum_5m: float = 0.0
    momentum_15m: float = 0.0
    momentum_60m: float = 0.0


@dataclass
class TradeSignal:
    sym: str
    direction: str      # "LONG" | "SHORT"
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    confidence: float
    rr_ratio: float
    signal_label: str
    atr: float


class PredictionEngine:

    def __init__(self, config: BotConfig):
        self.config = config

    def predict(self, sym: str, ta: TAResult, rt_df: pd.DataFrame) -> Prediction:
        """
        Translate TA result from REAL-TIME data into a directional prediction.
        v2.3: No more delayed data for prediction — delay is for confirmation only.
        """
        direction = ta.direction
        confidence = ta.confidence

        # Predicted move: scale to ATR × composite score distance from 0.5
        atr = ta.atr if ta.atr > 0 else float(rt_df["close"].iloc[-1]) * 0.001
        move_scale = (ta.composite_score - 0.5) * 2  # -1 to +1
        predicted_move_pct = abs(move_scale) * ta.atr_pct * 2

        log.debug("[%s] Prediction → dir=%+d conf=%.2f move=%.3f%%",
                  sym, direction, confidence, predicted_move_pct * 100)

        return Prediction(
            sym=sym,
            direction=direction,
            confidence=confidence,
            calibrated_confidence=0.5 + (confidence - 0.5) * 0.8,  # conservative shrinkage
            predicted_move_pct=predicted_move_pct,
            signal=ta.signal,
            ta=ta,
            timestamp=ta.timestamp,
        )


class SignalGenerator:
    """
    v2.3: Two-stage signal generation:
    1. Generate prediction on real-time data (stored with timestamp)
    2. After confirmation_delay, check if price moved as predicted
    3. If confirmed, emit trade signal
    """

    def __init__(self, config: BotConfig):
        self.config = config
        # v2.3: cooldown tracking — sym → bar counter since last signal
        self._last_signal_bar: Dict[str, int] = {}
        self._bar_counter: Dict[str, int] = {}
        # v2.3: Pending predictions awaiting confirmation
        self._pending_predictions: Dict[str, List[Tuple[datetime, Prediction]]] = {}
        # v2.3: Confirmation delay in bars (1 bar = 1 min)
        self.confirmation_delay_bars = config.__dict__.get('confirmation_delay_min', 15)

    def tick(self, sym: str):
        """Increment bar counter for cooldown tracking. Call once per loop."""
        self._bar_counter[sym] = self._bar_counter.get(sym, 0) + 1

    def store_prediction(self, sym: str, pred: Prediction):
        """Store prediction for later confirmation check."""
        if sym not in self._pending_predictions:
            self._pending_predictions[sym] = []
        self._pending_predictions[sym].append((datetime.now(timezone.utc), pred))

        # Clean old predictions (> 2 hours)
        cutoff = datetime.now(timezone.utc) - pd.Timedelta(hours=2)
        self._pending_predictions[sym] = [
            (t, p) for t, p in self._pending_predictions[sym] if t > cutoff
        ]

    # ── Delta computation (v2.3 — post-prediction momentum only) ───────────────────

    def compute_delta(self, sym: str, pred: Prediction, rt_df: pd.DataFrame) -> Delta:
        """
        Compare prediction to real-time momentum that occurred AFTER the prediction.
        v2.3: NO lookahead — only uses post-prediction price action.
        """
        if rt_df is None or len(rt_df) < 15:
            return Delta(sym, pred.direction, 0.0, False, 0.0)

        rt_df = rt_df.sort_index()
        pred_ts = getattr(pred, "timestamp", None)
        if isinstance(pred_ts, datetime):
            if pred_ts.tzinfo is None:
                pred_ts = pred_ts.replace(tzinfo=timezone.utc)
            if getattr(rt_df.index, "tz", None) is None:
                try:
                    rt_df = rt_df.tz_localize(timezone.utc)
                except Exception:
                    pass
            future_df = rt_df[rt_df.index > pred_ts]
            if future_df.empty:
                log.debug("[%s] Delta computation skipped — no post-prediction bars after %s", sym, pred_ts)
                return Delta(sym, pred.direction, 0.0, False, 0.0)
            rt_df = future_df

        close = rt_df["close"]
        atr = pred.ta.atr if pred.ta.atr > 0 else float(close.iloc[-1]) * 0.001

        # Compute momentum at each timeframe (all post-prediction by construction)
        # Caller must ensure rt_df only contains bars after pred.timestamp
        mom_5m  = self._compute_momentum(close, min(5, len(close)-1))
        mom_15m = self._compute_momentum(close, min(15, len(close)-1))
        mom_60m = self._compute_momentum(close, min(60, len(close)-1))

        # Weighted composite momentum (ATR-normalised)
        weighted_mom = (
            mom_60m * 0.50 +
            mom_15m * 0.30 +
            mom_5m  * 0.20
        )

        # Normalise by ATR to make it scale-independent
        norm_mom = weighted_mom / atr if atr > 0 else 0.0

        # Direction from real-time
        rt_direction = 1 if norm_mom > 0 else (-1 if norm_mom < 0 else 0)
        aligned = (pred.direction == rt_direction) and (pred.direction != 0)

        # Minimum momentum magnitude: must exceed 0.3× ATR to count
        min_magnitude = 0.3
        if abs(norm_mom) < min_magnitude:
            aligned = False

        # Alignment score: continuous, capped at 1.0
        if not aligned:
            alignment_score = 0.0
        else:
            # Score = normalised momentum magnitude, saturates at 2× ATR
            alignment_score = float(np.clip(abs(norm_mom) / 2.0, 0.0, 1.0))

        log.debug(
            "[%s] Delta → pred=%+d mom_5m=%.2f mom_15m=%.2f mom_60m=%.2f "
            "norm=%.3f aligned=%s score=%.2f",
            sym, pred.direction, mom_5m, mom_15m, mom_60m,
            norm_mom, aligned, alignment_score
        )

        return Delta(
            sym=sym,
            pred_direction=pred.direction,
            rt_momentum=float(norm_mom),
            aligned=aligned,
            alignment_score=float(alignment_score),
            momentum_5m=float(mom_5m),
            momentum_15m=float(mom_15m),
            momentum_60m=float(mom_60m),
        )

    @staticmethod
    def _compute_momentum(close: pd.Series, bars: int) -> float:
        """Compute price change over the last N bars."""
        if len(close) < bars + 1:
            if len(close) >= 2:
                return float(close.iloc[-1] - close.iloc[0])
            return 0.0
        return float(close.iloc[-1] - close.iloc[-1 - bars])

    # ── Signal generation (v2.3 — with cooldown) ─────────────────────

    def generate(self, sym: str, pred: Prediction, delta: Delta,
                 rt_df: pd.DataFrame) -> Optional[TradeSignal]:
        """
        Only emit a trade signal when:
          1. Signal cooldown has expired (30 bars since last signal)
          2. Prediction confidence >= threshold
          3. Prediction aligns with real-time momentum (delta confirmed)
          4. R:R ratio meets minimum
          5. Volatility regime is not EXTREME
        """
        cfg = self.config
        ta  = pred.ta

        # Gate 0: cooldown
        current_bar = self._bar_counter.get(sym, 0)
        last_bar = self._last_signal_bar.get(sym, -cfg.signal_cooldown_bars - 1)
        if current_bar - last_bar < cfg.signal_cooldown_bars:
            log.debug("[%s] Signal rejected — cooldown (%d/%d bars)",
                      sym, current_bar - last_bar, cfg.signal_cooldown_bars)
            return None

        # Gate 1: volatility regime halt
        if ta.vol_regime == "EXTREME":
            log.debug("[%s] Signal rejected — EXTREME volatility regime", sym)
            return None

        # Gate 2: confidence
        if pred.confidence < cfg.min_confidence:
            log.debug("[%s] Signal rejected — confidence %.2f < %.2f",
                      sym, pred.confidence, cfg.min_confidence)
            return None

        # Gate 3: neutral signals don't trade
        if pred.direction == 0:
            log.debug("[%s] Signal rejected — direction FLAT", sym)
            return None

        # Gate 4: delta alignment
        if not delta.aligned:
            log.debug("[%s] Signal rejected — prediction not confirmed by real-time delta", sym)
            return None

        if delta.alignment_score < cfg.delta_threshold:
            log.debug("[%s] Signal rejected — alignment score %.2f < %.2f",
                      sym, delta.alignment_score, cfg.delta_threshold)
            return None

        # Compute entry, SL, TP from real-time price + ATR
        c   = float(rt_df["close"].iloc[-1])
        atr = ta.atr if ta.atr > 0 else c * 0.001

        atr_mult = self._get_atr_sl_multiplier(sym)
        tp1_rr_val = self._get_tp1_rr(sym)
        tp2_rr_val = self._get_tp2_rr(sym)

        if pred.direction == 1:  # LONG
            entry = c
            sl    = c - atr * atr_mult
            tp1   = c + atr * atr_mult * tp1_rr_val
            tp2   = c + atr * atr_mult * tp2_rr_val
        else:  # SHORT
            entry = c
            sl    = c + atr * atr_mult
            tp1   = c - atr * atr_mult * tp1_rr_val
            tp2   = c - atr * atr_mult * tp2_rr_val

        risk_pts   = abs(entry - sl)
        reward_pts = abs(tp2 - entry)
        rr = reward_pts / risk_pts if risk_pts > 0 else 0.0

        # Gate 5: R:R
        if rr < cfg.min_rr_ratio:
            log.debug("[%s] Signal rejected — R:R %.2f < %.2f", sym, rr, cfg.min_rr_ratio)
            return None

        direction_str = "LONG" if pred.direction == 1 else "SHORT"
        log.info("[%s] TRADE SIGNAL CONFIRMED → %s | R:R=%.2f | conf=%.2f",
                 sym, direction_str, rr, pred.confidence)

        # Record cooldown
        self._last_signal_bar[sym] = current_bar

        return TradeSignal(
            sym=sym,
            direction=direction_str,
            entry=round(entry, 5),
            stop_loss=round(sl, 5),
            tp1=round(tp1, 5),
            tp2=round(tp2, 5),
            confidence=pred.confidence,
            rr_ratio=round(rr, 2),
            signal_label=pred.signal,
            atr=round(atr, 5),
        )

    def _get_atr_sl_multiplier(self, sym: str) -> float:
        return self.config.per_instrument.get(sym, {}).get('atr_sl_multiplier', self.config.atr_sl_multiplier)

    def _get_tp1_rr(self, sym: str) -> float:
        return self.config.per_instrument.get(sym, {}).get('tp1_rr', self.config.tp1_rr)

    def _get_tp2_rr(self, sym: str) -> float:
        return self.config.per_instrument.get(sym, {}).get('tp2_rr', self.config.tp2_rr)
