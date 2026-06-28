"""Tests for the backtest cost model and walk-forward fold construction."""

import asyncio

import numpy as np
import pandas as pd
import pytest

from citadel_bot.backtest import (
    _benchmark_buy_hold,
    _synthetic_grid_series,
    make_folds,
)


def test_make_folds_single_fold_uses_legacy_split():
    df = pd.DataFrame({"close": np.arange(1000)})
    folds = make_folds(df, n_folds=1)
    assert len(folds) == 1
    train, test = folds[0]
    assert len(train) == 300  # 30%
    assert len(test) == 700


def test_make_folds_expanding_window():
    df = pd.DataFrame({"close": np.arange(13000)})
    folds = make_folds(df, n_folds=4)
    assert len(folds) == 4
    # Each fold's train should grow; test segments don't overlap
    last_train = 0
    last_test_end = 0
    for train, test in folds:
        assert len(train) > last_train
        assert train.index[0] == 0
        # Test follows immediately after train and doesn't overlap with prior test
        assert test.index[0] >= last_test_end
        last_train = len(train)
        last_test_end = test.index[-1] + 1


def test_make_folds_falls_back_when_too_small():
    df = pd.DataFrame({"close": np.arange(800)})
    # 800 / (10 + 1) = 72 bars per fold — below the 500 floor, should shrink fold count.
    folds = make_folds(df, n_folds=10)
    assert len(folds) < 10


def test_buy_hold_benchmark_positive_on_uptrend():
    closes = np.linspace(100, 110, 500)
    df = pd.DataFrame({
        "open": closes, "high": closes + 0.1, "low": closes - 0.1,
        "close": closes, "volume": np.ones(500),
    }, index=pd.date_range("2024-01-02", periods=500, freq="1min"))

    class _Cfg:
        def get_multiplier(self, sym): return 1.0

    result = _benchmark_buy_hold("TEST", df, _Cfg())
    assert result["total_return_pct"] > 0
    assert result["final_equity"] > 100_000


def test_buy_hold_benchmark_zero_on_empty():
    class _Cfg:
        def get_multiplier(self, sym): return 1.0
    result = _benchmark_buy_hold("TEST", pd.DataFrame(), _Cfg())
    assert result["final_equity"] == 100_000.0


def test_synthetic_grid_series_shape():
    df = _synthetic_grid_series(n=1000, eps=10.0, p0=5000.0, sigma=1.0, pull=0.05)
    assert len(df) == 1000
    assert set(df.columns) >= {"open", "high", "low", "close", "volume"}
    # Highs always >= closes; lows always <= closes
    assert (df["high"] >= df["close"]).all()
    assert (df["low"] <= df["close"]).all()
