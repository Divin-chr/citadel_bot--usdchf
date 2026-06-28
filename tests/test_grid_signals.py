"""Regression tests for GridSignalGenerator (dead zones, range-break, kill switch)."""

import os
import tempfile

import numpy as np
import pandas as pd
import pytest

from citadel_bot.grid_engine import (
    EmitterPerformanceTracker,
    GridCalibrator,
    GridSignalGenerator,
)


@pytest.fixture
def isolated_tracker(base_config, tmp_path):
    """A tracker that persists to a throwaway directory so tests don't pollute data/."""
    base_config.data_dir = str(tmp_path)
    return EmitterPerformanceTracker(base_config)


def _make_df_at_position(base_price: float, eps: float, target_position: float, n: int = 60) -> pd.DataFrame:
    """Build a DataFrame whose last close sits at base_price + target_position*eps."""
    rng = np.random.default_rng(1)
    closes = base_price + rng.normal(0, eps * 0.01, n)
    closes[-1] = base_price + target_position * eps
    return pd.DataFrame({
        "open": closes,
        "high": closes + eps * 0.01,
        "low": closes - eps * 0.01,
        "close": closes,
        "volume": np.ones(n) * 100,
    }, index=pd.date_range("2024-01-02 09:30", periods=n, freq="1min"))


def test_mean_revert_lower_half_emits_long(base_config, isolated_tracker):
    cal = GridCalibrator(base_config)
    cal.epsilons["US500"] = 10.0
    cal.diagnostics["US500"] = {"best_cov": -1.0}
    gen = GridSignalGenerator(base_config, cal, isolated_tracker)
    # Position 0.3 = lower half between edge (0.05) and mid dead zone (0.40)
    df = _make_df_at_position(1000.0, 10.0, 0.3, n=600)
    # Cooldown should be cold on first generate
    signal, gate = gen.generate("US500", df)
    assert signal is not None, f"Expected MEAN_REVERT LONG, got rejection={gate}"
    assert signal.signal_label == "MEAN_REVERT"
    assert signal.direction == "LONG"


def test_mean_revert_dead_zone_at_midpoint(base_config, isolated_tracker):
    cal = GridCalibrator(base_config)
    cal.epsilons["US500"] = 10.0
    cal.diagnostics["US500"] = {"best_cov": -1.0}
    gen = GridSignalGenerator(base_config, cal, isolated_tracker)
    df = _make_df_at_position(1000.0, 10.0, 0.50, n=600)
    signal, gate = gen.generate("US500", df)
    assert signal is None
    assert gate == "DEAD_ZONE"


def test_emitter_kill_switch_disables_after_losses(isolated_tracker):
    sym = "US500"
    mode = "MEAN_REVERT"
    assert isolated_tracker.is_enabled(sym, mode)
    for _ in range(10):
        isolated_tracker.record(sym, mode, -50.0)
    assert not isolated_tracker.is_enabled(sym, mode)
    # Other emitter on the same sym must still be enabled.
    assert isolated_tracker.is_enabled(sym, "RANGE_BREAK")


def test_emitter_below_min_trades_does_not_kill(isolated_tracker):
    sym = "US500"
    mode = "MEAN_REVERT"
    # 9 trades of -50 each => -450 total but below the 10-trade floor.
    for _ in range(9):
        isolated_tracker.record(sym, mode, -50.0)
    assert isolated_tracker.is_enabled(sym, mode)


def test_emitter_kill_releases_when_pnl_recovers(isolated_tracker):
    sym = "US500"
    mode = "MEAN_REVERT"
    for _ in range(10):
        isolated_tracker.record(sym, mode, -50.0)
    assert not isolated_tracker.is_enabled(sym, mode)
    # Big enough wins to flip the rolling sum back above the threshold.
    for _ in range(20):
        isolated_tracker.record(sym, mode, 30.0)
    assert isolated_tracker.is_enabled(sym, mode)


def test_range_break_requires_confirmation_closes(base_config, isolated_tracker):
    """A single bar crossing the grid line should NOT fire range-break."""
    cal = GridCalibrator(base_config)
    cal.epsilons["US500"] = 10.0
    cal.diagnostics["US500"] = {"best_cov": -1.0}
    base_config.range_break_confirmation_closes = 3
    base_config.range_break_lookback = 15
    gen = GridSignalGenerator(base_config, cal, isolated_tracker)

    # Build a window: 12 bars in the lower regime, then 1 single bar above the line,
    # then back below — only one bar of confirmation, should be rejected.
    closes = [1003.0] * 12 + [1011.0] + [1002.0] + [1003.5]
    df = pd.DataFrame({
        "open": closes, "high": [c + 0.1 for c in closes], "low": [c - 0.1 for c in closes],
        "close": closes, "volume": [100.0] * len(closes),
    }, index=pd.date_range("2024-01-02 09:30", periods=len(closes), freq="1min"))
    signal, gate = gen.generate("US500", df)
    assert signal is None or signal.signal_label != "RANGE_BREAK"


def test_reset_suspensions_clears_state(base_config, isolated_tracker):
    cal = GridCalibrator(base_config)
    gen = GridSignalGenerator(base_config, cal, isolated_tracker)
    gen._suspended.add("US500")
    gen._suspended.add("NDAQ")
    gen.reset_suspensions()
    assert gen._suspended == set()


def test_generate_does_not_arm_cooldown(base_config, isolated_tracker):
    """Cooldown is armed by mark_executed(), not by generate(). A risk-rejected
    signal must not block subsequent ticks during e.g. macro halts."""
    cal = GridCalibrator(base_config)
    cal.epsilons["US500"] = 10.0
    cal.diagnostics["US500"] = {"best_cov": -1.0}
    gen = GridSignalGenerator(base_config, cal, isolated_tracker)
    # Lower-half mean-revert location -> signal is returned
    df = _make_df_at_position(1000.0, 10.0, 0.3, n=600)
    gen.tick("US500")  # bar 1
    signal, _ = gen.generate("US500", df)
    assert signal is not None
    # generate() must NOT have armed the cooldown
    assert "US500" not in gen._last_signal_bar

    # Caller arms the cooldown after a successful order
    gen.mark_executed("US500")
    assert gen._last_signal_bar["US500"] == gen._bar_counter["US500"]


def test_mark_executed_blocks_subsequent_signals(base_config, isolated_tracker):
    cal = GridCalibrator(base_config)
    cal.epsilons["US500"] = 10.0
    cal.diagnostics["US500"] = {"best_cov": -1.0}
    base_config.signal_cooldown_bars = 5
    gen = GridSignalGenerator(base_config, cal, isolated_tracker)
    df = _make_df_at_position(1000.0, 10.0, 0.3, n=600)

    gen.tick("US500")
    signal, _ = gen.generate("US500", df)
    assert signal is not None
    gen.mark_executed("US500")

    # Next tick — still within cooldown window
    gen.tick("US500")
    signal2, gate = gen.generate("US500", df)
    assert signal2 is None
    assert gate == "COOLDOWN"
