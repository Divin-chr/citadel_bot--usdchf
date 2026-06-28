"""Property tests for GridCalibrator (Cov^mod + permutation + Bonferroni)."""

import asyncio

import numpy as np
import pytest

from citadel_bot.grid_engine import GridCalibrator


def test_cov_mod_random_walk_near_zero(base_config, random_walk_closes):
    """A random walk has no S/R structure -> cov_mod close to zero at any ε."""
    cal = GridCalibrator(base_config)
    cov = cal._compute_cov_mod(random_walk_closes, eps=100.0)
    # Random walks can produce small non-zero cov by chance; sanity bound only.
    assert abs(cov) < 5.0, f"cov_mod on random walk={cov} too large (sigma drift?)"


def test_cov_mod_grid_pull_strongly_negative(base_config, grid_pull_closes):
    """ε=100 grid-pull series should produce a clearly negative Cov^mod at ε=100."""
    cal = GridCalibrator(base_config)
    cov_true = cal._compute_cov_mod(grid_pull_closes, eps=100.0)
    assert cov_true < -0.1, f"Expected strongly negative cov at ε=100, got {cov_true}"


def test_cov_mod_grid_pull_at_true_eps_dominates(base_config, grid_pull_closes):
    """ε=100 (the true grid) should yield more negative Cov^mod than an off-grid ε."""
    cal = GridCalibrator(base_config)
    cov_true = cal._compute_cov_mod(grid_pull_closes, eps=100.0)
    cov_off  = cal._compute_cov_mod(grid_pull_closes, eps=37.0)
    assert cov_true < cov_off, f"True ε cov={cov_true} should be < off-grid cov={cov_off}"


def test_bonferroni_threshold_tightens(base_config):
    """Bonferroni divides α by the number of candidates."""
    base_config.grid_correction_method = "bonferroni"
    cal = GridCalibrator(base_config)
    assert cal._effective_threshold(7) == pytest.approx(0.05 / 7)
    assert cal._effective_threshold(5) == pytest.approx(0.05 / 5)


def test_no_correction_leaves_threshold_unchanged(base_config):
    base_config.grid_correction_method = "none"
    cal = GridCalibrator(base_config)
    assert cal._effective_threshold(7) == pytest.approx(0.05)


def test_permutation_test_random_walk_high_pvalue(base_config, random_walk_closes):
    """On a random walk, the permutation p-value should NOT pass Bonferroni."""
    cal = GridCalibrator(base_config)
    cov = cal._compute_cov_mod(random_walk_closes, eps=100.0)
    if cov >= 0:
        pytest.skip("Random walk happened to have positive cov; nothing to test")
    p = asyncio.run(cal._permutation_test(random_walk_closes, eps=100.0, observed_cov=cov, n=200))
    # Bonferroni-corrected threshold for 7 candidates is 0.05/7 ≈ 0.0071.
    assert p > base_config.grid_min_significance / 7, (
        f"Random walk cov should fail Bonferroni at p={p}"
    )


def test_permutation_test_grid_pull_passes_bonferroni(base_config, grid_pull_closes):
    """Grid-pull series should pass Bonferroni at the true ε."""
    cal = GridCalibrator(base_config)
    cov = cal._compute_cov_mod(grid_pull_closes, eps=100.0)
    p = asyncio.run(cal._permutation_test(grid_pull_closes, eps=100.0, observed_cov=cov, n=200))
    assert p < base_config.grid_min_significance / 7, (
        f"Grid-pull cov should pass Bonferroni; got p={p}"
    )


def test_calibrate_sets_last_calibration_at(base_config, grid_pull_closes):
    """last_calibration_at must be populated after calibrate() completes so the
    main loop can decide when to recalibrate."""
    import pandas as pd
    from citadel_bot.grid_engine import GridCalibrator

    base_config.instruments = ["US500"]
    base_config.grid_candidates_indices = [50.0, 100.0, 250.0]

    df = pd.DataFrame({
        "open": grid_pull_closes,
        "high": grid_pull_closes + 0.5,
        "low": grid_pull_closes - 0.5,
        "close": grid_pull_closes,
        "volume": [100.0] * len(grid_pull_closes),
    }, index=pd.date_range("2024-01-02 09:30", periods=len(grid_pull_closes), freq="1min"))

    class _Pipeline:
        async def get_realtime(self, sym): return df

    cal = GridCalibrator(base_config)
    assert cal.last_calibration_at is None
    asyncio.run(cal.calibrate(_Pipeline()))
    assert cal.last_calibration_at is not None
