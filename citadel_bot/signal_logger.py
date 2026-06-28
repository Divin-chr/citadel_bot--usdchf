"""
signal_logger.py — Grid-strategy signal logger.

Logs EVERY signal attempt (emitted and rejected) with the grid context:
ε, nearest grid lines, regime position, signal mode (MEAN_REVERT |
RANGE_BREAK), Cov^mod statistics, and trade parameters when a signal was
emitted.

Dual persistence:
- CSV (data/signal_log.csv) — always written.
- PostgreSQL grid_signal_logs table — written when DB is reachable.

The legacy `signal_logs` table (TA-shaped) is left untouched and is no
longer written by this module.
"""

import asyncio
import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from citadel_bot.config import BotConfig
from citadel_bot.database.database_manager import db_manager
from citadel_bot.grid_engine import GridLocation, TradeSignal

log = logging.getLogger("signal_log")


class SignalLogger:

    HEADERS = [
        "timestamp_utc",
        "sym",
        # Grid state
        "epsilon",
        "grid_below",
        "grid_above",
        "midpoint",
        "regime_position",
        # Calibration stats
        "cov_mod",
        "cov_mod_pvalue",
        # Signal outcome
        "signal_emitted",
        "signal_mode",         # MEAN_REVERT | RANGE_BREAK
        "rejection_gate",
        "direction",
        "confidence",
        "entry_price",
        "stop_loss",
        "tp1",
        "tp2",
        "rr_ratio",
        "atr",
    ]

    def __init__(self, config: BotConfig):
        self.config = config
        self._path = Path(config.data_dir) / "signal_log.csv"
        self._ensure_file()
        self._db_available = False

    def _ensure_file(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            return
        with self._path.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(self.HEADERS)

    async def initialize_db(self):
        self._db_available = await db_manager.health_check()
        if self._db_available:
            log.info("Signal logger: database ready")
        else:
            log.warning("Signal logger: database unavailable, CSV only")

    def log_signal(
        self,
        sym: str,
        location: Optional[GridLocation],
        cov_mod: Optional[float],
        cov_mod_pvalue: Optional[float],
        signal: Optional[TradeSignal],
        rejection_gate: str = "",
    ):
        if not self.config.signal_logging:
            return

        try:
            timestamp_utc = datetime.now(timezone.utc)

            row = {
                "timestamp_utc": timestamp_utc,
                "sym": sym,
                "epsilon": round(location.eps, 6) if location else None,
                "grid_below": round(location.grid_below, 6) if location else None,
                "grid_above": round(location.grid_above, 6) if location else None,
                "midpoint": round(location.midpoint, 6) if location else None,
                "regime_position": round(location.regime_position, 4) if location else None,
                "cov_mod": round(cov_mod, 6) if cov_mod is not None else None,
                "cov_mod_pvalue": round(cov_mod_pvalue, 4) if cov_mod_pvalue is not None else None,
                "signal_emitted": signal is not None,
                "signal_mode": signal.signal_label if signal else "",
                "rejection_gate": rejection_gate,
                "direction": signal.direction if signal else "",
                "confidence": round(signal.confidence, 4) if signal else None,
                "entry_price": signal.entry if signal else None,
                "stop_loss": signal.stop_loss if signal else None,
                "tp1": signal.tp1 if signal else None,
                "tp2": signal.tp2 if signal else None,
                "rr_ratio": signal.rr_ratio if signal else None,
                "atr": signal.atr if signal else None,
            }

            csv_row = [
                timestamp_utc.isoformat(),
                sym,
                row["epsilon"] if row["epsilon"] is not None else "",
                row["grid_below"] if row["grid_below"] is not None else "",
                row["grid_above"] if row["grid_above"] is not None else "",
                row["midpoint"] if row["midpoint"] is not None else "",
                row["regime_position"] if row["regime_position"] is not None else "",
                row["cov_mod"] if row["cov_mod"] is not None else "",
                row["cov_mod_pvalue"] if row["cov_mod_pvalue"] is not None else "",
                row["signal_emitted"],
                row["signal_mode"],
                row["rejection_gate"],
                row["direction"],
                row["confidence"] if row["confidence"] is not None else "",
                row["entry_price"] if row["entry_price"] is not None else "",
                row["stop_loss"] if row["stop_loss"] is not None else "",
                row["tp1"] if row["tp1"] is not None else "",
                row["tp2"] if row["tp2"] is not None else "",
                row["rr_ratio"] if row["rr_ratio"] is not None else "",
                row["atr"] if row["atr"] is not None else "",
            ]
            with self._path.open("a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(csv_row)

            if self._db_available:
                self._create_background_task(
                    lambda row=row, sym=sym: self._log_to_database(row, sym),
                    f"signal_log_{sym}",
                )

        except Exception as exc:
            log.error("Failed to log signal: %s", exc)

    async def _log_to_database(self, row: dict, symbol: str):
        try:
            instrument_id = await db_manager.get_instrument_id(symbol)
            if not instrument_id:
                log.warning("[%s] Instrument missing in DB, skipping grid signal log", symbol)
                return
            payload = dict(row)
            payload["instrument_id"] = instrument_id
            await db_manager.insert_grid_signal_log(payload)
        except Exception as exc:
            log.error("[%s] Failed to log grid signal to database: %s", symbol, exc)

    def _create_background_task(self, coro_factory, name: str = ""):
        async def _task_wrapper():
            attempts = 0
            while attempts < 2:
                try:
                    await coro_factory()
                    return
                except Exception as exc:
                    attempts += 1
                    log.warning("Background task '%s' failed attempt %d: %s", name, attempts, exc)
                    if attempts >= 2:
                        log.error("Background task '%s' failed permanently", name)
                        return
                    await asyncio.sleep(1)

        try:
            asyncio.create_task(_task_wrapper())
        except RuntimeError:
            log.debug("No running loop; skipping background DB task '%s'", name)
