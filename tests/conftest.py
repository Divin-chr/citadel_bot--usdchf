"""Shared pytest fixtures."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from citadel_bot.config import BotConfig


@pytest.fixture
def base_config() -> BotConfig:
    """Default BotConfig with predictable settings for tests."""
    cfg = BotConfig()
    cfg.instruments = ["US500"]
    cfg.mode = "paper"
    cfg.grid_correction_method = "bonferroni"
    cfg.grid_min_significance = 0.05
    cfg.regime_monitor_enabled = False  # off by default for tests; opt-in per test
    cfg.regime_monitor_window = 500
    cfg.regime_break_threshold = 0.5
    cfg.range_break_lookback = 15
    cfg.range_break_confirmation_closes = 2
    cfg.signal_cooldown_bars = 5
    cfg.emitter_kill_threshold_usd = -100.0
    cfg.emitter_min_trades_for_kill = 10
    cfg.atr_period_for_stops = 14
    cfg.atr_sl_buffer = 0.25
    cfg.trailing_distance_atr = 1.5
    cfg.trailing_activation_atr = 1.0
    return cfg


@pytest.fixture
def random_walk_closes() -> np.ndarray:
    """A pure random walk — should produce ~zero Cov^mod at any ε."""
    rng = np.random.default_rng(42)
    return 1000.0 + np.cumsum(rng.normal(0, 1.0, 2000))


@pytest.fixture
def grid_pull_closes() -> np.ndarray:
    """
    Synthetic series that bounces inside [iε, (i+1)ε] regimes with ε=100.
    Should produce strongly negative Cov^mod at ε=100, ~zero at off-grid candidates.
    """
    rng = np.random.default_rng(7)
    n = 2000
    closes = np.empty(n)
    closes[0] = 1000.0
    pull = 0.05
    for i in range(1, n):
        prev = closes[i - 1]
        closes[i] = prev + rng.normal(0, 1.0) - pull * ((prev % 100.0) - 50.0)
    return closes


@pytest.fixture
def random_walk_df(random_walk_closes) -> pd.DataFrame:
    """OHLCV DataFrame around random_walk_closes for tests that need ATR / OHLC."""
    closes = random_walk_closes
    return pd.DataFrame({
        "open": closes,
        "high": closes + 0.5,
        "low": closes - 0.5,
        "close": closes,
        "volume": np.ones_like(closes) * 100.0,
    }, index=pd.date_range("2024-01-02 09:30", periods=len(closes), freq="1min"))


@pytest.fixture
def grid_pull_df(grid_pull_closes) -> pd.DataFrame:
    closes = grid_pull_closes
    return pd.DataFrame({
        "open": closes,
        "high": closes + 0.5,
        "low": closes - 0.5,
        "close": closes,
        "volume": np.ones_like(closes) * 100.0,
    }, index=pd.date_range("2024-01-02 09:30", periods=len(closes), freq="1min"))
