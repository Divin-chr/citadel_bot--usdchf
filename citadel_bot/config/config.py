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
    # ─── Database connection ───────────────────────────────────────────────────────────────────
    database_url: str = ""
    database_host: str = ""
    database_port: int = 5432
    database_name: str = ""
    database_user: str = ""
    database_password: str = ""

    # ── MetaApi connection ───────────────────────────────────────────────
    metaapi_token: str = ""
    metaapi_account_id: str = ""
    mode: str = ""            # "paper" | "live"

    # ── Instruments ──────────────────────────────────────────────────
    # Populated from dashboard or config.yaml.
    # All instrument metadata (multiplier, exchange, session) is
    # looked up dynamically from instrument_catalog.py.
    instruments: List[str] = field(default_factory=lambda: ["US30", "US500", "NDAQ", "USOUSD"])

    # Per-instrument overrides (optional — catalog defaults are used when absent)
    instrument_exchange: Dict[str, str] = field(default_factory=dict)
    instrument_currency: Dict[str, str] = field(default_factory=dict)
    instrument_multiplier: Dict[str, float] = field(default_factory=dict)
    instrument_session: Dict[str, str] = field(default_factory=dict)

    # Per-instrument settings overrides
    per_instrument: Dict[str, Dict[str, float]] = field(default_factory=dict)

    # ── Data / buffer ────────────────────────────────────────────────
    bar_size: str = "1 min"
    history_bars: int = 400
    auto_calibrate: bool = True
    calibration_window_days: int = 90
    calibration_step_min: int = 2
    buffer_min_delay_min: int = 4
    buffer_max_delay_min: int = 40

    # ── Signal generation ────────────────────────────────────────────
    min_confidence: float = 0.62
    min_rr_ratio: float = 1.8
    delta_threshold: float = 0.55

    # ── Risk management ──────────────────────────────────────────────
    max_risk_per_trade_pct: float = 0.015
    max_daily_drawdown_pct: float = 0.04
    max_concurrent_positions: int = 2
    atr_sl_multiplier: float = 1.8
    tp1_rr: float = 1.5
    tp2_rr: float = 3.0
    tp1_size_pct: float = 0.5

    # ── Session filters (defaults; overridden per instrument via catalog) ──
    trade_session_start: str = "09:30"
    trade_session_end: str = "16:00"
    avoid_first_minutes: int = 5
    macro_halt_minutes_before: int = 30
    macro_halt_minutes_after: int = 60

    # ── Technical analysis ───────────────────────────────────────────
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    bb_period: int = 20
    bb_std: float = 2.0
    ma_periods: List[int] = field(default_factory=lambda: [50, 100, 200])
    atr_period: int = 14
    volume_ma_period: int = 20

    # ── Signal cooldown ──────────────────────────────────────────────
    signal_cooldown_bars: int = 30       # min bars between signals per symbol

    # ── Kelly sizing ─────────────────────────────────────────────────
    use_kelly_sizing: bool = True
    kelly_fraction: float = 0.5          # half-Kelly
    kelly_cap_pct: float = 0.02          # max 2% per trade under Kelly
    portfolio_heat_cap_pct: float = 0.06 # max 6% total portfolio risk
    kelly_lookback_trades: int = 50      # rolling window for win-rate estimation

    # ── Volatility regime ────────────────────────────────────────────
    vol_regime_lookback_days: int = 60   # ATR percentile window
    vol_regime_high_pct: float = 0.75    # above this: dampen trend, amplify MR
    vol_regime_halt_pct: float = 0.95    # above this: halt trading
    vol_regime_reduce_risk_pct: float = 0.85  # above this: halve risk

    # ── Backtest cost model ──────────────────────────────────────────
    backtest_spread_pts: float = 0.0     # 0 = use catalog typical_spread
    backtest_slippage_pts: float = 1.0   # 1 tick slippage per side
    backtest_commission_per_lot: float = 0.0

    # ── Trailing stop ────────────────────────────────────────────────
    trailing_stop_after_tp1: bool = True # move SL to breakeven after TP1

    # ── Signal logging ───────────────────────────────────────────────
    signal_logging: bool = True          # log all signal attempts to CSV

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
