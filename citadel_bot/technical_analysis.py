"""
technical_analysis.py — Debiased, orthogonalised TA engine (v2.2)

Computes on DELAYED data (from buffer). All individual indicators are
still computed and stored on TAResult for logging/analysis, but the
composite score is built from 5 ORTHOGONAL signal groups:

  1. Trend      — multi-TF EMA alignment (replaces trend+MA+cross overlap)
  2. Momentum   — RSI z-score (standalone mean-reversion/continuation)
  3. Acceleration — MACD histogram rate-of-change
  4. Volatility — BB %B + squeeze + ATR regime percentile
  5. Structure  — S/R proximity + patterns + Fibonacci confluence

Each group outputs a continuous -1 to +1 score. Equal weighting (20%).
No long bias: symmetric scoring for above/below conditions.
Volatility regime detection adjusts trend vs mean-reversion weighting.
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict

import numpy as np
import pandas as pd

from citadel_bot.config import BotConfig

log = logging.getLogger("ta")


@dataclass
class TAResult:
    sym: str
    timestamp: pd.Timestamp

    # Trend
    trend_daily: str    = "NEUTRAL"
    trend_weekly: str   = "NEUTRAL"
    trend_monthly: str  = "NEUTRAL"
    trend_strength: float = 0.5

    # Moving averages
    ma_50: float  = 0.0
    ma_100: float = 0.0
    ma_200: float = 0.0
    price_vs_ma50: str  = "ABOVE"
    price_vs_ma200: str = "ABOVE"
    golden_cross: bool  = False
    death_cross: bool   = False

    # RSI
    rsi: float = 50.0
    rsi_signal: str = "NEUTRAL"

    # MACD
    macd_line: float  = 0.0
    macd_signal: float = 0.0
    macd_hist: float  = 0.0
    macd_cross: str   = "NONE"

    # Bollinger Bands
    bb_upper: float = 0.0
    bb_lower: float = 0.0
    bb_mid: float   = 0.0
    bb_pct: float   = 0.5
    bb_squeeze: bool = False

    # ATR
    atr: float = 0.0
    atr_pct: float = 0.0

    # Volume
    volume_ratio: float = 1.0
    volume_signal: str  = "NEUTRAL"

    # S/R
    support_levels:    List[float] = field(default_factory=list)
    resistance_levels: List[float] = field(default_factory=list)
    nearest_support:   float = 0.0
    nearest_resistance: float = 0.0

    # Fibonacci
    fib_levels: Dict[str, float] = field(default_factory=dict)

    # Patterns
    patterns: List[str] = field(default_factory=list)

    # Orthogonal group scores (-1 to +1 each)
    group_trend: float = 0.0
    group_momentum: float = 0.0
    group_acceleration: float = 0.0
    group_volatility: float = 0.0
    group_structure: float = 0.0

    # Volatility regime
    vol_regime: str = "NORMAL"          # LOW / NORMAL / HIGH / EXTREME
    vol_percentile: float = 0.5         # ATR percentile over lookback
    market_regime: str = "UNKNOWN"      # TRENDING / CHOPPY / RANGE / UNKNOWN
    chop_score: float = 0.0             # 0 clean, 1 very choppy

    # Composite
    composite_score: float = 0.5
    confidence: float = 0.5
    signal: str = "NEUTRAL"
    direction: int = 0

    # Raw price
    close: float = 0.0


class TechnicalAnalyzer:

    def __init__(self, config: BotConfig):
        self.config = config

    def analyze(self, sym: str, df: pd.DataFrame) -> TAResult:
        result = TAResult(sym=sym, timestamp=df.index[-1], close=float(df["close"].iloc[-1]))
        close = df["close"]
        high  = df["high"]
        low   = df["low"]
        vol   = df["volume"]
        c     = float(close.iloc[-1])

        # Compute all raw indicators (unchanged from v2.1)
        self._compute_trend(result, df)
        self._compute_mas(result, close, c)
        self._compute_rsi(result, close)
        self._compute_macd(result, close)
        self._compute_bb(result, close, c)
        self._compute_atr(result, high, low, close, c)
        self._compute_volume(result, vol)
        self._compute_sr(result, high, low, c)
        self._compute_fibonacci(result, high, low)
        self._detect_patterns(result, df)

        # v2.2: volatility regime detection
        self._compute_vol_regime(result, high, low, close)
        self._compute_market_regime(result, high, low, close)

        # v2.2: orthogonal group scoring + debiased composite
        self._compute_group_scores(result, c)
        self._composite_score(result)
        return result

    # ── Trend ────────────────────────────────────────────────────────

    def _compute_trend(self, r: TAResult, df: pd.DataFrame):
        close = df["close"]

        # Explicit multi-timeframe trend context: 1m, 5m, 1h, daily, weekly
        tf_series = {
            "1m": close,
            "5m": close.resample("5min").last().dropna(),
            "1h": close.resample("1h").last().dropna(),
            "1d": close.resample("1D").last().dropna(),
            "1w": close.resample("1W").last().dropna(),
        }

        r.trend_daily = self._ema_trend(tf_series["1d"]) if len(tf_series["1d"]) > 5 else "NEUTRAL"
        r.trend_weekly = self._ema_trend(tf_series["1w"]) if len(tf_series["1w"]) > 5 else "NEUTRAL"
        monthly = close.resample("ME").last().dropna()
        r.trend_monthly = self._ema_trend(monthly) if len(monthly) > 3 else "NEUTRAL"

        weights = {"1m": 0.15, "5m": 0.20, "1h": 0.25, "1d": 0.20, "1w": 0.20}
        scores = {"BULLISH": 1, "NEUTRAL": 0, "BEARISH": -1}
        raw = sum(
            scores[self._ema_trend(series)] * weights[label]
            for label, series in tf_series.items()
            if len(series) >= 5
        )
        r.trend_strength = float(np.clip(raw / 1.0, 0.0, 1.0))

    @staticmethod
    def _ema_trend(s: pd.Series) -> str:
        if len(s) < 5:
            return "NEUTRAL"
        ema_fast = s.ewm(span=5, adjust=False).mean().iloc[-1]
        ema_slow = s.ewm(span=min(15, len(s)), adjust=False).mean().iloc[-1]
        diff_pct = (ema_fast - ema_slow) / ema_slow if ema_slow != 0 else 0
        if diff_pct > 0.001:  return "BULLISH"
        if diff_pct < -0.001: return "BEARISH"
        return "NEUTRAL"

    # ── Moving averages ──────────────────────────────────────────────

    def _compute_mas(self, r: TAResult, close: pd.Series, c: float):
        for p in self.config.ma_periods:
            if len(close) >= p:
                val = float(close.tail(p).mean())
            else:
                val = float(close.mean())
            if p == 50:  r.ma_50  = val
            if p == 100: r.ma_100 = val
            if p == 200: r.ma_200 = val
        r.price_vs_ma50  = "ABOVE" if c > r.ma_50  else "BELOW"
        r.price_vs_ma200 = "ABOVE" if c > r.ma_200 else "BELOW"
        if len(close) >= 201:
            prev_ma50  = float(close.tail(51).head(50).mean())
            prev_ma200 = float(close.tail(201).head(200).mean())
            r.golden_cross = (prev_ma50 < prev_ma200) and (r.ma_50 > r.ma_200)
            r.death_cross  = (prev_ma50 > prev_ma200) and (r.ma_50 < r.ma_200)

    # ── RSI ──────────────────────────────────────────────────────────

    def _compute_rsi(self, r: TAResult, close: pd.Series):
        p = self.config.rsi_period
        if len(close) < p + 1:
            return
        delta = close.diff()
        gain  = delta.clip(lower=0).ewm(com=p - 1, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(com=p - 1, adjust=False).mean()
        rs    = gain / loss.replace(0, np.nan)
        rsi   = 100 - (100 / (1 + rs))
        r.rsi = float(rsi.iloc[-1])
        if r.rsi >= 70:   r.rsi_signal = "OVERBOUGHT"
        elif r.rsi <= 30: r.rsi_signal = "OVERSOLD"
        else:             r.rsi_signal = "NEUTRAL"

    # ── MACD ─────────────────────────────────────────────────────────

    def _compute_macd(self, r: TAResult, close: pd.Series):
        fast, slow, sig = self.config.macd_fast, self.config.macd_slow, self.config.macd_signal
        if len(close) < slow + sig:
            return
        ema_fast   = close.ewm(span=fast, adjust=False).mean()
        ema_slow   = close.ewm(span=slow, adjust=False).mean()
        macd_line  = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=sig, adjust=False).mean()
        hist       = macd_line - signal_line
        r.macd_line   = float(macd_line.iloc[-1])
        r.macd_signal = float(signal_line.iloc[-1])
        r.macd_hist   = float(hist.iloc[-1])
        if len(hist) >= 2:
            prev_hist = float(hist.iloc[-2])
            if prev_hist < 0 and r.macd_hist > 0:
                r.macd_cross = "BULLISH_CROSS"
            elif prev_hist > 0 and r.macd_hist < 0:
                r.macd_cross = "BEARISH_CROSS"

    # ── Bollinger Bands ──────────────────────────────────────────────

    def _compute_bb(self, r: TAResult, close: pd.Series, c: float):
        p   = self.config.bb_period
        std = self.config.bb_std
        if len(close) < p:
            return
        ma  = close.tail(p).mean()
        sd  = close.tail(p).std()
        r.bb_mid   = float(ma)
        r.bb_upper = float(ma + std * sd)
        r.bb_lower = float(ma - std * sd)
        band_width = r.bb_upper - r.bb_lower
        r.bb_pct   = float((c - r.bb_lower) / band_width) if band_width > 0 else 0.5
        if len(close) >= 50:
            bw_hist = []
            for i in range(50, len(close)):
                slc = close.iloc[i-p:i]
                bw_hist.append(float(slc.std() * 2 * std))
            r.bb_squeeze = band_width < (np.mean(bw_hist) * 0.7)

    # ── ATR ──────────────────────────────────────────────────────────

    def _compute_atr(self, r: TAResult, high: pd.Series, low: pd.Series,
                     close: pd.Series, c: float):
        p = self.config.atr_period
        if len(close) < p + 1:
            return
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs()
        ], axis=1).max(axis=1)
        r.atr = float(tr.ewm(com=p - 1, adjust=False).mean().iloc[-1])
        r.atr_pct = r.atr / c if c > 0 else 0.0

    # ── Volume ───────────────────────────────────────────────────────

    def _compute_volume(self, r: TAResult, vol: pd.Series):
        p = self.config.volume_ma_period
        if len(vol) < p:
            return
        vol_ma = float(vol.tail(p).mean())
        cur    = float(vol.iloc[-1])
        r.volume_ratio = cur / vol_ma if vol_ma > 0 else 1.0
        if r.volume_ratio > 1.2:   r.volume_signal = "HIGH"
        elif r.volume_ratio < 0.8: r.volume_signal = "LOW"
        else:                      r.volume_signal = "NORMAL"

    # ── Support & Resistance ─────────────────────────────────────────

    def _compute_sr(self, r: TAResult, high: pd.Series, low: pd.Series, c: float):
        supports:    List[float] = []
        resistances: List[float] = []
        h = high.values
        l = low.values
        for i in range(2, len(h) - 2):
            if l[i] < l[i-1] and l[i] < l[i+1] and l[i] < l[i-2] and l[i] < l[i+2]:
                supports.append(float(l[i]))
            if h[i] > h[i-1] and h[i] > h[i+1] and h[i] > h[i-2] and h[i] > h[i+2]:
                resistances.append(float(h[i]))

        def cluster(levels: List[float], pct=0.002) -> List[float]:
            if not levels:
                return []
            levels = sorted(levels)
            clusters, group = [], [levels[0]]
            for v in levels[1:]:
                if abs(v - group[-1]) / group[-1] < pct:
                    group.append(v)
                else:
                    clusters.append(float(np.mean(group)))
                    group = [v]
            clusters.append(float(np.mean(group)))
            return clusters

        r.support_levels    = cluster(supports)
        r.resistance_levels = cluster(resistances)
        below = [s for s in r.support_levels    if s < c]
        above = [s for s in r.resistance_levels if s > c]
        r.nearest_support    = max(below) if below else c * 0.995
        r.nearest_resistance = min(above) if above else c * 1.005

    # ── Fibonacci ────────────────────────────────────────────────────

    def _compute_fibonacci(self, r: TAResult, high: pd.Series, low: pd.Series):
        """
        v2.3: Fibonacci levels retained for logging but NOT used in scoring.
        No statistical edge demonstrated in backtests.
        """
        swing_high = float(high.tail(50).max())
        swing_low  = float(low.tail(50).min())
        diff = swing_high - swing_low
        r.fib_levels = {
            "23.6%": swing_high - 0.236 * diff,
            "38.2%": swing_high - 0.382 * diff,
            "50.0%": swing_high - 0.500 * diff,
            "61.8%": swing_high - 0.618 * diff,
            "78.6%": swing_high - 0.786 * diff,
        }

    # ── Patterns ─────────────────────────────────────────────────────

    def _detect_patterns(self, r: TAResult, df: pd.DataFrame):
        close = df["close"]
        patterns = []
        if self._higher_highs_lows(df):
            patterns.append("HIGHER_HIGHS_LOWS")
        if self._lower_highs_lows(df):
            patterns.append("LOWER_HIGHS_LOWS")
        if self._bull_flag(close):
            patterns.append("BULL_FLAG")
        if self._bear_flag(close):
            patterns.append("BEAR_FLAG")
        if self._head_and_shoulders(df["high"]):
            patterns.append("HEAD_AND_SHOULDERS")
        if self._ascending_triangle(df):
            patterns.append("ASCENDING_TRIANGLE")
        r.patterns = patterns

    @staticmethod
    def _higher_highs_lows(df: pd.DataFrame, n=20) -> bool:
        h = df["high"].tail(n).values
        l = df["low"].tail(n).values
        return (h[-1] > h[-5] > h[-10]) and (l[-1] > l[-5] > l[-10])

    @staticmethod
    def _lower_highs_lows(df: pd.DataFrame, n=20) -> bool:
        h = df["high"].tail(n).values
        l = df["low"].tail(n).values
        return (h[-1] < h[-5] < h[-10]) and (l[-1] < l[-5] < l[-10])

    @staticmethod
    def _bull_flag(close: pd.Series, n=30) -> bool:
        if len(close) < n:
            return False
        pole      = close.tail(n).head(10)
        flag      = close.tail(n).tail(20)
        pole_move = (float(pole.iloc[-1]) - float(pole.iloc[0])) / float(pole.iloc[0])
        flag_range = (float(flag.max()) - float(flag.min())) / float(flag.mean())
        return pole_move > 0.01 and flag_range < 0.008

    @staticmethod
    def _bear_flag(close: pd.Series, n=30) -> bool:
        if len(close) < n:
            return False
        pole      = close.tail(n).head(10)
        flag      = close.tail(n).tail(20)
        pole_move = (float(pole.iloc[0]) - float(pole.iloc[-1])) / float(pole.iloc[0])
        flag_range = (float(flag.max()) - float(flag.min())) / float(flag.mean())
        return pole_move > 0.01 and flag_range < 0.008

    @staticmethod
    def _head_and_shoulders(high: pd.Series, n=40) -> bool:
        if len(high) < n:
            return False
        h = high.tail(n).values
        mid = len(h) // 2
        left_shoulder  = h[:mid - 5].max()
        head           = h[mid - 5: mid + 5].max()
        right_shoulder = h[mid + 5:].max()
        return (head > left_shoulder * 1.005) and (head > right_shoulder * 1.005) \
               and abs(left_shoulder - right_shoulder) / head < 0.03

    @staticmethod
    def _ascending_triangle(df: pd.DataFrame, n=30) -> bool:
        if len(df) < n:
            return False
        high_slice = df["high"].tail(n)
        low_slice  = df["low"].tail(n)
        flat_top   = (high_slice.max() - high_slice.mean()) / high_slice.mean() < 0.005
        rising_low = float(low_slice.iloc[-1]) > float(low_slice.iloc[0])
        return flat_top and rising_low

    # ── Volatility regime ────────────────────────────────────────────

    def _compute_vol_regime(self, r: TAResult, high: pd.Series,
                            low: pd.Series, close: pd.Series):
        """Compute ATR percentile over rolling window for regime detection."""
        p = self.config.atr_period
        lookback = self.config.vol_regime_lookback_days * 390  # approx bars
        if len(close) < p + 10:
            return

        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs()
        ], axis=1).max(axis=1)
        atr_series = tr.ewm(com=p - 1, adjust=False).mean()

        # Use available history up to lookback
        window = atr_series.tail(min(lookback, len(atr_series)))
        current_atr = float(window.iloc[-1])

        if len(window) < 50:
            r.vol_percentile = 0.5
            r.vol_regime = "NORMAL"
            return

        percentile = float((window < current_atr).sum() / len(window))
        r.vol_percentile = percentile

        if percentile >= self.config.vol_regime_halt_pct:
            r.vol_regime = "EXTREME"
        elif percentile >= self.config.vol_regime_high_pct:
            r.vol_regime = "HIGH"
        elif percentile <= 0.25:
            r.vol_regime = "LOW"
        else:
            r.vol_regime = "NORMAL"

    def _compute_market_regime(self, r: TAResult, high: pd.Series,
                               low: pd.Series, close: pd.Series):
        """Classify whether recent price action is directional enough to trade."""
        if len(close) < 40 or r.atr <= 0:
            return

        window = min(60, len(close) - 1)
        recent_close = close.tail(window + 1)
        recent_high = high.tail(window)
        recent_low = low.tail(window)

        net_move = abs(float(recent_close.iloc[-1] - recent_close.iloc[0]))
        path = float(recent_close.diff().abs().sum())
        efficiency = net_move / path if path > 0 else 0.0

        price_range = float(recent_high.max() - recent_low.min())
        range_atr = price_range / r.atr if r.atr > 0 else 0.0

        ma_fast = recent_close.ewm(span=8, adjust=False).mean()
        ma_slow = recent_close.ewm(span=21, adjust=False).mean()
        slope_atr = abs(float(ma_fast.iloc[-1] - ma_slow.iloc[-1])) / r.atr if r.atr > 0 else 0.0

        chop = 0.0
        if efficiency < 0.18:
            chop += 0.45
        elif efficiency < 0.28:
            chop += 0.25
        if range_atr < 1.8:
            chop += 0.30
        elif range_atr < 2.5:
            chop += 0.15
        if slope_atr < 0.20:
            chop += 0.25
        elif slope_atr < 0.35:
            chop += 0.10

        r.chop_score = float(np.clip(chop, 0.0, 1.0))
        if r.chop_score >= 0.65:
            r.market_regime = "CHOPPY"
        elif range_atr < 2.2 and efficiency < 0.35:
            r.market_regime = "RANGE"
        elif efficiency >= 0.35 and slope_atr >= 0.25:
            r.market_regime = "TRENDING"
        else:
            r.market_regime = "UNKNOWN"

    # ── Orthogonal group scores (v2.2) ───────────────────────────────

    def _compute_group_scores(self, r: TAResult, c: float):
        """
        Build 5 orthogonal group scores, each -1 to +1.
        These replace the old correlated composite scoring.
        """
        # --- Group 1: TREND (multi-TF alignment + MA structure) ---
        t_map = {"BULLISH": 1.0, "NEUTRAL": 0.0, "BEARISH": -1.0}
        trend_raw = (
            t_map.get(r.trend_daily, 0.0)   * 0.5 +
            t_map.get(r.trend_weekly, 0.0)  * 0.3 +
            t_map.get(r.trend_monthly, 0.0) * 0.2
        )
        # MA structure: symmetric scoring
        ma_score = 0.0
        if r.ma_50 > 0 and r.ma_200 > 0:
            ma_score = np.clip((c - r.ma_200) / (r.atr * 5) if r.atr > 0 else 0.0, -1.0, 1.0) * 0.3
        cross_bonus = 0.0
        if r.golden_cross:  cross_bonus = 0.2
        if r.death_cross:   cross_bonus = -0.2
        r.group_trend = float(np.clip(trend_raw + ma_score + cross_bonus, -1.0, 1.0))

        # --- Group 2: MOMENTUM (RSI as continuous signal) ---
        # RSI 50 = neutral. Symmetric: 70 → +1, 30 → -1
        rsi_norm = (r.rsi - 50.0) / 20.0  # maps 30→-1, 50→0, 70→+1
        r.group_momentum = float(np.clip(rsi_norm, -1.0, 1.0))

        # --- Group 3: ACCELERATION (MACD histogram rate of change) ---
        # Normalise histogram by ATR to make it comparable across instruments
        if r.atr > 0:
            hist_norm = r.macd_hist / r.atr
        else:
            hist_norm = 0.0
        cross_boost = 0.0
        if r.macd_cross == "BULLISH_CROSS":  cross_boost = 0.3
        if r.macd_cross == "BEARISH_CROSS":  cross_boost = -0.3
        r.group_acceleration = float(np.clip(hist_norm * 2.0 + cross_boost, -1.0, 1.0))

        # --- Group 4: VOLATILITY (BB position + squeeze + regime) ---
        # BB %B: 0.5 = neutral, continuous both directions
        bb_signal = (r.bb_pct - 0.5) * 2.0  # maps 0→-1, 0.5→0, 1→+1
        squeeze_bonus = 0.0
        if r.bb_squeeze:
            # Squeeze = coiled energy, amplify the existing direction
            squeeze_bonus = bb_signal * 0.3
        # Regime adjustment: in HIGH vol, mean-reversion is stronger → invert signal
        if r.vol_regime == "HIGH":
            bb_signal *= -0.5  # dampen or partially invert
        r.group_volatility = float(np.clip(bb_signal + squeeze_bonus, -1.0, 1.0))

        # --- Group 5: STRUCTURE (S/R only — v2.3: removed patterns + Fibonacci) ---
        struct_score = 0.0
        # Proximity to support vs resistance (symmetric)
        if c > 0:
            dist_to_support = (c - r.nearest_support) / c
            dist_to_resist  = (r.nearest_resistance - c) / c
            if dist_to_resist > 0 and dist_to_support > 0:
                # Closer to support → bullish; closer to resistance → bearish
                sr_bias = (dist_to_resist - dist_to_support) / (dist_to_resist + dist_to_support)
                struct_score += np.clip(sr_bias, -0.5, 0.5)

        # v2.3: Removed Fibonacci confluence — no statistical edge
        # v2.3: Removed pattern bonuses — heuristic detection has no demonstrated edge

        r.group_structure = float(np.clip(struct_score, -1.0, 1.0))

    # ── Composite score (v2.2 — debiased) ────────────────────────────

    def _composite_score(self, r: TAResult):
        """
        Equal-weighted average of 5 orthogonal group scores.
        Regime-adaptive: in HIGH vol, reduce trend weight and increase
        volatility (mean-reversion) weight.
        """
        if r.vol_regime in ("HIGH", "EXTREME"):
            # Dampen trend-following, amplify mean-reversion
            weights = {
                "trend": 0.10, "momentum": 0.25, "acceleration": 0.15,
                "volatility": 0.30, "structure": 0.20,
            }
        elif r.vol_regime == "LOW":
            # Low vol: trend-following works better, MR less useful
            weights = {
                "trend": 0.30, "momentum": 0.15, "acceleration": 0.25,
                "volatility": 0.10, "structure": 0.20,
            }
        else:
            # Normal: equal weighting
            weights = {
                "trend": 0.20, "momentum": 0.20, "acceleration": 0.20,
                "volatility": 0.20, "structure": 0.20,
            }

        raw = (
            r.group_trend        * weights["trend"] +
            r.group_momentum     * weights["momentum"] +
            r.group_acceleration * weights["acceleration"] +
            r.group_volatility   * weights["volatility"] +
            r.group_structure    * weights["structure"]
        )

        # Map from [-1, +1] to [0, 1] for composite score
        r.composite_score = float(np.clip(0.5 + raw * 0.5, 0.0, 1.0))

        # Direction: continuous, no cliff edges. Use 0.60/0.40 thresholds.
        if r.composite_score >= 0.60:   r.direction = 1
        elif r.composite_score <= 0.40: r.direction = -1
        else:                           r.direction = 0

        # Confidence = how far from neutral, scaled to 0-1
        r.confidence = float(abs(r.composite_score - 0.5) * 2.0)

        # Label
        s = r.composite_score
        if s >= 0.75:   r.signal = "STRONG_BUY"
        elif s >= 0.60: r.signal = "BUY"
        elif s <= 0.25: r.signal = "STRONG_SELL"
        elif s <= 0.40: r.signal = "SELL"
        else:           r.signal = "NEUTRAL"
