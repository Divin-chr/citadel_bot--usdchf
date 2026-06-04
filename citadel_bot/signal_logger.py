"""
signal_logger.py — Signal quality logger for data-driven iteration

Logs EVERY signal attempt (approved and rejected) with full TA indicator
values, delta alignment details, gate rejection reason, and entry price.

Now supports dual persistence:
- PostgreSQL database (primary) for real-time analytics
- CSV fallback (data/signal_log.csv) for compatibility

After 200+ entries, enables offline analysis:
  - Information Coefficient (IC) per indicator
  - Feature importance via random forest
  - Gate rejection rate analysis
"""

import asyncio
import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from citadel_bot.config import BotConfig
from citadel_bot.database.database_manager import db_manager

log = logging.getLogger("signal_log")


class SignalLogger:

    HEADERS = [
        "timestamp_utc",
        "sym",
        # TA group scores (orthogonalised)
        "score_trend",
        "score_momentum",
        "score_acceleration",
        "score_volatility",
        "score_structure",
        # Raw indicators
        "trend_daily",
        "trend_weekly",
        "trend_monthly",
        "rsi",
        "macd_hist",
        "macd_cross",
        "bb_pct",
        "bb_squeeze",
        "atr",
        "atr_pct",
        "volume_ratio",
        "nearest_support",
        "nearest_resistance",
        "patterns",
        # Composite
        "composite_score",
        "confidence",
        "direction",
        # Delta
        "rt_momentum",
        "delta_aligned",
        "alignment_score",
        # Signal outcome
        "signal_emitted",
        "rejection_gate",
        "entry_price",
        "stop_loss",
        "tp1",
        "tp2",
        "rr_ratio",
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
        """Check database availability"""
        self._db_available = await db_manager.health_check()
        if self._db_available:
            log.info("✅ Signal logger database ready")
        else:
            log.warning("⚠️  Signal logger database not available, using CSV only")

    def log_signal(
        self,
        sym: str,
        ta_result,
        prediction=None,
        delta=None,
        signal=None,
        rejection_gate: str = "",
    ):
        """Log a signal attempt with all available context."""
        if not self.config.signal_logging:
            return

        try:
            timestamp_utc = datetime.now(timezone.utc)

            # Prepare data for both CSV and database
            signal_data = {
                'timestamp_utc': timestamp_utc,
                'instrument_id': None,  # Will be set if DB available
                'score_trend': round(getattr(ta_result, "group_trend", 0.0), 4),
                'score_momentum': round(getattr(ta_result, "group_momentum", 0.0), 4),
                'score_acceleration': round(getattr(ta_result, "group_acceleration", 0.0), 4),
                'score_volatility': round(getattr(ta_result, "group_volatility", 0.0), 4),
                'score_structure': round(getattr(ta_result, "group_structure", 0.0), 4),
                'trend_daily': str(ta_result.trend_daily).upper() if ta_result.trend_daily else 'NEUTRAL',
                'trend_weekly': str(ta_result.trend_weekly).upper() if ta_result.trend_weekly else 'NEUTRAL',
                'trend_monthly': str(ta_result.trend_monthly).upper() if ta_result.trend_monthly else 'NEUTRAL',
                'rsi': round(ta_result.rsi, 2),
                'macd_hist': round(ta_result.macd_hist, 5),
                'macd_cross': str(ta_result.macd_cross).upper() if ta_result.macd_cross else 'NONE',
                'bb_pct': round(ta_result.bb_pct, 4),
                'bb_squeeze': bool(ta_result.bb_squeeze),
                'atr': round(ta_result.atr, 5),
                'atr_pct': round(ta_result.atr_pct, 6),
                'volume_ratio': round(ta_result.volume_ratio, 3),
                'nearest_support': round(ta_result.nearest_support, 5),
                'nearest_resistance': round(ta_result.nearest_resistance, 5),
                'patterns': ta_result.patterns if ta_result.patterns else [],
                'composite_score': round(ta_result.composite_score, 4),
                'confidence': round(ta_result.confidence, 4),
                'direction': ta_result.direction,
                'rt_momentum': round(delta.rt_momentum, 6) if delta is not None else None,
                'delta_aligned': bool(delta.aligned) if delta is not None else None,
                'alignment_score': round(delta.alignment_score, 4) if delta is not None else None,
                'signal_emitted': signal is not None,
                'rejection_gate': rejection_gate,
                'vol_regime': ta_result.vol_regime,
                'entry_price': round(signal.entry, 5) if signal else None,
                'stop_loss': round(signal.stop_loss, 5) if signal else None,
                'tp1': round(signal.tp1, 5) if signal else None,
                'tp2': round(signal.tp2, 5) if signal else None,
                'rr_ratio': round(signal.rr_ratio, 3) if signal else None,
            }

            # Write to CSV (fallback)
            row = [
                timestamp_utc.isoformat(),
                sym,
                signal_data['score_trend'],
                signal_data['score_momentum'],
                signal_data['score_acceleration'],
                signal_data['score_volatility'],
                signal_data['score_structure'],
                signal_data['trend_daily'],
                signal_data['trend_weekly'],
                signal_data['trend_monthly'],
                signal_data['rsi'],
                signal_data['macd_hist'],
                signal_data['macd_cross'],
                signal_data['bb_pct'],
                signal_data['bb_squeeze'],
                signal_data['atr'],
                signal_data['atr_pct'],
                signal_data['volume_ratio'],
                signal_data['nearest_support'],
                signal_data['nearest_resistance'],
                "|".join(signal_data['patterns']),
                signal_data['composite_score'],
                signal_data['confidence'],
                signal_data['direction'],
                signal_data['rt_momentum'] if signal_data['rt_momentum'] is not None else "",
                signal_data['delta_aligned'] if signal_data['delta_aligned'] is not None else "",
                signal_data['alignment_score'] if signal_data['alignment_score'] is not None else "",
                signal_data['signal_emitted'],
                signal_data['rejection_gate'],
                signal_data['entry_price'] or "",
                signal_data['stop_loss'] or "",
                signal_data['tp1'] or "",
                signal_data['tp2'] or "",
                signal_data['rr_ratio'] or "",
            ]
            with self._path.open("a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)

            # Write to database (primary) - async background task
            if self._db_available:
                self._create_background_task(
                    lambda signal_data=signal_data, sym=sym: self._log_to_database(signal_data, sym),
                    f"signal_log_{sym}"
                )

        except Exception as exc:
            log.error("Failed to log signal: %s", exc)

    async def _log_to_database(self, signal_data: dict, symbol: str):
        """Log signal to database asynchronously"""
        try:
            # Get instrument ID
            instrument_id = await db_manager.get_instrument_id(symbol)
            if not instrument_id:
                log.warning("[%s] Instrument not found in database, skipping signal log", symbol)
                return

            # Create a copy of signal_data with proper type conversions for database
            db_signal_data = signal_data.copy()
            
            # Convert fields that need to be strings for database (VARCHAR fields)
            string_fields = ['trend_daily', 'trend_weekly', 'trend_monthly', 'macd_cross']
            for field in string_fields:
                if field in db_signal_data and db_signal_data[field] is not None:
                    db_signal_data[field] = str(db_signal_data[field]).upper()
            
            # Convert signal_emitted to proper boolean for database (it's BOOLEAN in schema)
            if 'signal_emitted' in db_signal_data and db_signal_data['signal_emitted'] is not None:
                if isinstance(db_signal_data['signal_emitted'], str):
                    db_signal_data['signal_emitted'] = db_signal_data['signal_emitted'].upper() == 'TRUE'
                else:
                    db_signal_data['signal_emitted'] = bool(db_signal_data['signal_emitted'])
            
            # Keep bb_squeeze and delta_aligned as booleans (they are BOOLEAN in schema)
            # No conversion needed for these
            
            db_signal_data['instrument_id'] = instrument_id
            await db_manager.insert_signal_log(db_signal_data)

        except Exception as e:
            log.error("[%s] Failed to log signal to database: %s", symbol, e)

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

        asyncio.create_task(_task_wrapper())
