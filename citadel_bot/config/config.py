"""
config.py — Bot configuration dataclass.
Edit config.yaml to change settings; this module loads + validates it.
"""

import os
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Optional
from dotenv import load_dotenv

load_dotenv()


@dataclass
class BotConfig:
    # ── Database connection ──────────────────────────────────────────
    database_url: str = ""
    database_host: str = ""
    database_port: int = 5432
    database_name: str = ""
    database_user: str = ""
    database_password: str = ""

    # ── MetaApi connection ───────────────────────────────────────────
    metaapi_token: str = ""
    metaapi_account_id: str = ""
    mode: str = ""            # "paper" | "live"

    # ── Instruments ──────────────────────────────────────────────────
    instruments: List[str] = field(default_factory=lambda: ["US30", "US500", "NDAQ", "USOUSD"])

    # Per-instrument overrides (optional — catalog defaults are used when absent)
    instrument_exchange: Dict[str, str] = field(default_factory=dict)
    instrument_currency: Dict[str, str] = field(default_factory=dict)
    instrument_multiplier: Dict[str, float] = field(default_factory=dict)
    instrument_session: Dict[str, str] = field(default_factory=dict)

    # Per-instrument settings overrides (risk_pct etc.)
    per_instrument: Dict[str, Dict[str, float]] = field(default_factory=dict)

    # ── Data ─────────────────────────────────────────────────────────
    bar_size: str = "1 min"
    history_bars: int = 400

    # ── Grid strategy (Teeple 2025) ──────────────────────────────────
    # Candidate ε values per asset class — calibrator scans these and picks
    # the most-negative significant Cov^mod for each instrument.
    grid_candidates_indices: List[float] = field(default_factory=lambda: [
        2.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0
    ])
    grid_candidates_forex: List[float] = field(default_factory=lambda: [
        0.0010, 0.0025, 0.0050, 0.0100, 0.0250
    ])
    grid_candidates_crypto: List[float] = field(default_factory=lambda: [
        10.0, 50.0, 100.0, 500.0, 1000.0, 5000.0
    ])
    grid_candidates_commodities: List[float] = field(default_factory=lambda: [
        0.10, 0.25, 0.50, 1.00, 5.00
    ])
    # Dead zone around the midpoint where mean-reversion does not trade
    # (model says nothing happens there). 0.10 = ±5% of ε around the midpoint.
    grid_dead_zone: float = 0.10
    # Dead zone at the edges (within this fraction of ε from a grid line) where
    # mean-reversion stands aside — breakouts are likely brewing.
    grid_edge_dead_zone: float = 0.05
    # Recalibrate ε every N days when the cached value is older than this.
    grid_recalibration_days: int = 30
    # Permutation-test threshold for accepting an ε. Bonferroni correction
    # divides this by the number of candidates per asset class.
    grid_min_significance: float = 0.05
    # Multiple-comparisons correction across the candidate ε sweep.
    # "bonferroni" → require p < grid_min_significance / N_candidates.
    # "none"       → use grid_min_significance as-is (the original behavior).
    grid_correction_method: str = "bonferroni"
    # Live regime monitor — recomputes Cov^mod on the last N bars after warmup.
    # If recent cov drifts above (regime_break_threshold * calibrated_cov), suspend
    # trading on the instrument until the next recalibration window.
    regime_monitor_enabled: bool = True
    regime_monitor_window: int = 500
    regime_break_threshold: float = 0.5
    # How many bars back to look when detecting a grid-line cross (range-break).
    range_break_lookback: int = 15
    # Require this many consecutive closes beyond the broken grid line before
    # firing a range-break signal. Suppresses single-bar noise crosses.
    range_break_confirmation_closes: int = 2
    # ATR period used purely to pad stop-losses (so SL isn't exactly at a grid line).
    atr_period_for_stops: int = 14
    # SL pad as a multiple of ATR beyond the protecting grid line.
    atr_sl_buffer: float = 0.25
    # Per-(sym, emitter) auto-disable. Both gates must be true to kill an emitter:
    #   trailing 30-day realised P&L < emitter_kill_threshold_usd
    #   AND closed trades in window >= emitter_min_trades_for_kill
    # Re-enables automatically once losses age out of the 30-day window.
    emitter_kill_threshold_usd: float = -100.0
    emitter_min_trades_for_kill: int = 10
    # Trailing stop (Phase 2.5). Replaces the static grid-anchored SL once the
    # trade has moved trailing_activation_atr in profit.
    trailing_distance_atr: float = 1.5
    trailing_activation_atr: float = 1.0
    # Minimum bars between signals on the same symbol.
    signal_cooldown_bars: int = 30
    # Master switch (matches the old API used by RiskManager).
    disable_signal_filters: bool = False

    # ── Signal acceptance ────────────────────────────────────────────
    min_rr_ratio: float = 1.0
    tp1_size_pct: float = 0.5

    # ── Risk management ──────────────────────────────────────────────
    max_risk_per_trade_pct: float = 0.015
    max_daily_drawdown_pct: float = 0.04
    max_concurrent_positions: int = 2
    max_correlation: float = 0.7
    max_correlated_positions: int = 1
    disable_risk_filters: bool = False

    # ── Session filters (defaults; per-instrument overrides via catalog) ──
    trade_session_start: str = "09:30"
    trade_session_end: str = "16:00"
    avoid_first_minutes: int = 5
    macro_halt_minutes_before: int = 30
    macro_halt_minutes_after: int = 60

    # ── Kelly sizing ─────────────────────────────────────────────────
    use_kelly_sizing: bool = True
    kelly_fraction: float = 0.5
    kelly_cap_pct: float = 0.02
    portfolio_heat_cap_pct: float = 0.06
    kelly_lookback_trades: int = 50

    # ── Backtest cost model ──────────────────────────────────────────
    backtest_spread_pts: float = 0.0
    backtest_slippage_pts: float = 1.0
    backtest_commission_per_lot: float = 0.0
    backtest_stop_slippage_multiplier: float = 2.0
    backtest_gap_probability: float = 0.05

    # ── Trailing stop ────────────────────────────────────────────────
    trailing_stop_after_tp1: bool = True

    # ── Signal logging ───────────────────────────────────────────────
    signal_logging: bool = True

    # ── MetaApi / diagnostics ────────────────────────────────────────
    log_metaapi_messages: bool = True

    # ── Misc ─────────────────────────────────────────────────────────
    loop_interval_sec: float = 30.0
    log_level: str = "INFO"
    log_dir: str = "logs"
    data_dir: str = "data"

    # ------------------------------------------------------------------
    @classmethod
    def from_file(cls, path: str = "config.yaml") -> "BotConfig":
        p = Path(path)
        if not p.exists():
            cfg = cls()
            cfg.save(path)
            return cfg
        with open(p, encoding="utf-8-sig") as f:
            data = yaml.safe_load(f) or {}
        cfg = cls()
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        cfg._apply_environment_overrides()
        cfg._hydrate_from_catalog()
        return cfg

    def _apply_environment_overrides(self):
        """Load secrets and deployment-specific settings from environment variables."""
        self.metaapi_token = os.getenv("CITADEL_METAAPI_TOKEN", self.metaapi_token)
        self.metaapi_account_id = os.getenv("CITADEL_METAAPI_ACCOUNT_ID", self.metaapi_account_id)
        self.mode = os.getenv("CITADEL_MODE", self.mode)
        self.database_url = os.getenv("DATABASE_URL", os.getenv("CITADEL_DATABASE_URL", self.database_url))
        self.database_host = os.getenv("CITADEL_DATABASE_HOST", os.getenv("DATABASE_HOST", self.database_host))
        self.database_port = int(os.getenv("CITADEL_DATABASE_PORT", os.getenv("DATABASE_PORT", self.database_port)))
        self.database_name = os.getenv("CITADEL_DATABASE_NAME", os.getenv("DATABASE_NAME", self.database_name))
        self.database_user = os.getenv("CITADEL_DATABASE_USER", os.getenv("DATABASE_USER", self.database_user))
        self.database_password = os.getenv("CITADEL_DATABASE_PASSWORD", os.getenv("DATABASE_PASSWORD", self.database_password))

    def validate_metaapi(self):
        """Fail fast with a useful error if MetaApi credentials are missing."""
        missing = []
        if not self.metaapi_token:
            missing.append("CITADEL_METAAPI_TOKEN")
        if not self.metaapi_account_id:
            missing.append("CITADEL_METAAPI_ACCOUNT_ID")
        if missing:
            raise RuntimeError(
                "MetaApi credentials are missing. Set these environment variables: "
                + ", ".join(missing)
            )

    def save(self, path: str = "config.yaml"):
        import dataclasses
        d = dataclasses.asdict(self)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(d, f, default_flow_style=False, sort_keys=True)

    # ------------------------------------------------------------------
    def _hydrate_from_catalog(self):
        """
        Fill in any missing per-instrument metadata from the catalog.
        Explicit overrides in config.yaml / set by the dashboard take precedence.
        """
        try:
            from ..utils.instrument_catalog import get_instrument
        except ImportError:
            return

        for sym in self.instruments:
            info = get_instrument(sym)
            if info is None:
                continue
            if sym not in self.instrument_exchange:
                self.instrument_exchange[sym] = info.exchange
            if sym not in self.instrument_currency:
                self.instrument_currency[sym] = info.quote_currency
            if sym not in self.instrument_multiplier:
                self.instrument_multiplier[sym] = info.multiplier
            if sym not in self.instrument_session:
                self.instrument_session[sym] = info.session

    def get_multiplier(self, sym: str) -> float:
        """Return point-value multiplier for a symbol."""
        if sym in self.instrument_multiplier:
            return self.instrument_multiplier[sym]
        try:
            from citadel_bot.utils.instrument_catalog import get_instrument
            info = get_instrument(sym)
            if info:
                return info.multiplier
        except ImportError:
            pass
        return 1.0

    def get_session(self, sym: str) -> str:
        """Return session key for a symbol."""
        if sym in self.instrument_session:
            return self.instrument_session[sym]
        try:
            from citadel_bot.utils.instrument_catalog import get_instrument
            info = get_instrument(sym)
            if info:
                return info.session
        except ImportError:
            pass
        return "us_equity"

    @property
    def is_live(self) -> bool:
        return self.mode == "live"
