"""
data_pipeline.py — Real-time and historical data feed via MetaApi
Now with PostgreSQL persistence for zero-downtime operation
"""

import asyncio
import logging
from pathlib import Path
from typing import Dict, Optional, List, Tuple

import pandas as pd

from citadel_bot.config import BotConfig
from citadel_bot.database.database_manager import db_manager
from citadel_bot.utils.logger import get_logger

log = get_logger("pipeline")

SUPPORTED_TIMEFRAMES = [
    ("1m", "m1"),
    ("5m", "m5"),
    ("1d", "d1"),
    ("1w", "w1"),
]


class DataPipeline:
    """
    Pulls 1-minute bars from MT5 for each configured instrument and keeps
    a rolling DataFrame with `config.history_bars` rows.

    Now includes PostgreSQL persistence for:
    - Real-time data storage
    - Historical data retrieval
    - Zero-downtime operation
    """

    def __init__(self, config: BotConfig, account, connection):
        self.config = config
        self.account = account
        self.connection = connection
        self._bars: Dict[str, pd.DataFrame] = {}
        self._persisted_m1: Dict[str, pd.DataFrame] = {}
        self._data_dir = Path(self.config.data_dir) / "market_data"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        # Feed a deeper context window to TA/calibration when local history exists.
        self._analysis_window_bars = max(self.config.history_bars, 5000)
        self._db_available = False
        self._pending_tasks: List[asyncio.Task] = []
        self._last_timeframe_refresh: Dict[Tuple[str, str], object] = {}
        self._timeframe_refresh_seconds = {
            "1m": 60,
            "5m": 300,
            "1h": 3600,
            "1d": 21600,
            "1w": 86400,
        }

    async def get_realtime(self, sym: str) -> Optional[pd.DataFrame]:
        """Return latest rolling bars and refresh from MT5 on each call."""
        await self._refresh_symbol(sym)
        df = self._bars.get(sym)
        if df is not None and not df.empty:
            return df
        # Fallback to persisted history if MT5 refresh returns nothing.
        persisted = self._load_persisted_m1(sym)
        if persisted is not None and not persisted.empty:
            self._bars[sym] = persisted.tail(self._analysis_window_bars)
            return self._bars[sym]

        # Try loading from database as final fallback
        if self._db_available:
            return await self._load_from_database(sym)
        return None

    async def start_feeds(self):
        """Warm up data windows for all configured symbols."""
        # Check database availability
        self._db_available = await db_manager.health_check()
        if self._db_available:
            log.debug("Database available for data persistence")
        else:
            log.warning("⚠️  Database not available, using CSV fallback only")

        for sym in self.config.instruments:
            persisted = self._load_persisted_m1(sym)
            if persisted is not None and not persisted.empty:
                self._persisted_m1[sym] = persisted
                self._bars[sym] = persisted.tail(self._analysis_window_bars)

            # Try loading additional history from database
            if self._db_available:
                await self._load_from_database(sym)

            # Symbol selection not needed in MetaApi
            await self._refresh_symbol(sym)
            df = self._bars.get(sym, pd.DataFrame())
            log.debug("[%s] History loaded: %s bars", sym, len(df))

        await self.flush_pending_persistence()

    async def flush_pending_persistence(self):
        """Wait for queued market-data persistence tasks to finish."""
        if not self._pending_tasks:
            return
        pending = [task for task in self._pending_tasks if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._pending_tasks = [task for task in self._pending_tasks if not task.done()]

    async def _load_from_database(self, sym: str) -> Optional[pd.DataFrame]:
        """Load historical data from database"""
        if not self._db_available:
            return None

        try:
            # Get instrument ID
            instrument_id = await db_manager.get_instrument_id(sym)
            if not instrument_id:
                return None

            # Load recent data from database
            market_data = await db_manager.get_market_data(
                sym,
                'm1',
                self._analysis_window_bars,
                metaapi_account_id=self.config.metaapi_account_id,
                data_source='historical_candles',
            )
            if not market_data:
                return None

            # Convert to DataFrame
            df = pd.DataFrame([
                {
                    'datetime': row['timestamp_utc'],
                    'open': row['open_price'],
                    'high': row['high_price'],
                    'low': row['low_price'],
                    'close': row['close_price'],
                    'volume': row['volume']
                }
                for row in market_data
            ])

            df['datetime'] = pd.to_datetime(df['datetime'], utc=True)
            df = df.set_index('datetime').sort_index()
            df = self._normalize_index(df)
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["open", "high", "low", "close"]).astype(float)

            # Merge with existing data
            existing = self._bars.get(sym)
            if existing is not None and not existing.empty:
                combined = pd.concat([existing, df])
                combined = combined[~combined.index.duplicated(keep='last')]
                self._bars[sym] = combined.sort_index().tail(self._analysis_window_bars)
            else:
                self._bars[sym] = df

            log.debug("[%s] Loaded %d bars from database", sym, len(df))
            return self._bars[sym]

        except Exception as e:
            log.warning("[%s] Failed to load from database: %s", sym, e)
            return None

    async def _refresh_symbol(self, sym: str):
        from datetime import datetime

        end_time = datetime.utcnow()
        fetched_any = False

        for metaapi_timeframe, db_timeframe in SUPPORTED_TIMEFRAMES:
            if not self._should_refresh_timeframe(sym, metaapi_timeframe, end_time):
                continue

            start_time = self._timeframe_start_time(end_time, metaapi_timeframe)
            try:
                candles = await self.account.get_historical_candles(
                    sym,
                    metaapi_timeframe,
                    start_time,
                    self.config.history_bars,
                )
            except Exception as exc:
                self._last_timeframe_refresh[(sym, metaapi_timeframe)] = end_time
                log.error(
                    "[%s] MetaApi historical candles failed timeframe=%s db_timeframe=%s start=%s limit=%s error_type=%s error=%s details=%s",
                    sym,
                    metaapi_timeframe,
                    db_timeframe,
                    start_time.isoformat(),
                    self.config.history_bars,
                    type(exc).__name__,
                    exc,
                    self._exception_details(exc),
                )
                continue

            self._last_timeframe_refresh[(sym, metaapi_timeframe)] = end_time

            if candles is None or len(candles) == 0:
                log.debug(
                    "[%s] MetaApi returned no candles timeframe=%s start=%s limit=%s",
                    sym,
                    metaapi_timeframe,
                    start_time.isoformat(),
                    self.config.history_bars,
                )
                continue

            fetched_any = True
            df = self._candles_to_dataframe(candles)
            if df.empty:
                log.debug("[%s] MetaApi candles converted to empty dataframe timeframe=%s", sym, metaapi_timeframe)
                continue

            log.debug(
                "[%s] MetaApi %s candles fetched: %d bars %s -> %s",
                sym,
                db_timeframe,
                len(df),
                df.index[0].isoformat(),
                df.index[-1].isoformat(),
            )
            self._persist_symbol_data(sym, df, db_timeframe)

        if fetched_any:
            merged = self._persisted_m1.get(sym)
            if merged is not None and not merged.empty:
                self._bars[sym] = merged.tail(self._analysis_window_bars)
            else:
                self._bars[sym] = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    def _should_refresh_timeframe(self, sym: str, metaapi_timeframe: str, now) -> bool:
        last_refresh = self._last_timeframe_refresh.get((sym, metaapi_timeframe))
        if last_refresh is None:
            return True
        interval = self._timeframe_refresh_seconds.get(metaapi_timeframe, 60)
        return (now - last_refresh).total_seconds() >= interval

    def _timeframe_start_time(self, end_time, metaapi_timeframe: str):
        from datetime import timedelta

        if metaapi_timeframe in ("1m", "5m"):
            minutes = self.config.history_bars * (5 if metaapi_timeframe == "5m" else 1)
            return end_time - timedelta(minutes=minutes)
        if metaapi_timeframe == "1h":
            return end_time - timedelta(hours=self.config.history_bars)
        if metaapi_timeframe == "1d":
            return end_time - timedelta(days=self.config.history_bars)
        return end_time - timedelta(weeks=self.config.history_bars)

    def _candles_to_dataframe(self, candles) -> pd.DataFrame:
        df = pd.DataFrame(candles)
        if df.empty:
            return df

        df["datetime"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.floor("min")
        df = df.set_index("datetime").sort_index()
        df = df.rename(columns={"tickVolume": "volume"}) if "tickVolume" in df else df

        for col in ["open", "high", "low", "close", "volume"]:
            if col not in df.columns:
                df[col] = 0.0

        return self._normalize_index(df[["open", "high", "low", "close", "volume"]].astype(float))

    def _normalize_index(self, df: pd.DataFrame) -> pd.DataFrame:
        """Normalize index to UTC minute boundaries for alignment and storage."""
        if df is None or df.empty:
            return df
        try:
            index = pd.to_datetime(df.index, utc=True, errors="coerce")
        except Exception:
            return df
        if not isinstance(index, pd.DatetimeIndex) or index.empty:
            return df
        if index.tz is None:
            index = index.tz_localize("UTC", ambiguous='infer', nonexistent='shift_forward')
        else:
            index = index.tz_convert("UTC")
        index = index.floor("min")
        df.index = index
        df = df[~df.index.isna()]
        return df.sort_index()

    def _persist_symbol_data(self, sym: str, new_df: pd.DataFrame, timeframe: str):
        """
        Persist candles for a specific timeframe to both CSV and database.
        """
        new_df = self._normalize_index(new_df)
        csv_path = self._data_dir / f"{sym}_{timeframe}.csv"

        if sym not in self._persisted_m1:
            self._persisted_m1[sym] = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        persisted = self._load_persisted_frame(sym, timeframe)
        if persisted is None or persisted.empty:
            persisted = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        timeframe_delta = {
            "m1": pd.Timedelta(minutes=1),
            "m5": pd.Timedelta(minutes=5),
            "h1": pd.Timedelta(hours=1),
            "d1": pd.Timedelta(days=1),
            "w1": pd.Timedelta(weeks=1),
        }.get(timeframe, pd.Timedelta(minutes=1))

        if not persisted.empty:
            last_dt = persisted.index[-1] - timeframe_delta
            new_filtered = new_df[new_df.index >= last_dt]
        else:
            new_filtered = new_df

        merged = pd.concat([persisted, new_filtered])
        merged = self._normalize_index(merged)
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        merged = merged.tail(self._analysis_window_bars)

        merged.to_csv(csv_path, date_format="%Y-%m-%dT%H:%M:%SZ")

        if timeframe == "m1":
            self._persisted_m1[sym] = merged
            derived_m5 = self._derive_timeframe_from_m1(merged, "5min")
            if derived_m5 is not None and not derived_m5.empty:
                self._persist_symbol_data(sym, derived_m5, "m5")
            derived_h1 = self._derive_timeframe_from_m1(merged, "1h")
            if derived_h1 is not None and not derived_h1.empty:
                self._persist_symbol_data(sym, derived_h1, "h1")

        if self._db_available and not new_filtered.empty:
            self._create_background_task(
                lambda sym=sym, df=new_filtered, tf=timeframe: self._persist_to_database(sym, df, tf),
                f"persist_market_data_{sym}_{timeframe}"
            )

    def _derive_timeframe_from_m1(self, df: pd.DataFrame, freq: str) -> Optional[pd.DataFrame]:
        """Build higher timeframe OHLCV bars from fresh m1 data."""
        if df is None or df.empty:
            return None
        try:
            source = self._normalize_index(df)
            aggregated = source.resample(freq, label="left", closed="left").agg({
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            })
            aggregated = aggregated.dropna(subset=["open", "high", "low", "close"])

            # Keep only completed bars. Current partial higher-timeframe bars can distort TA/logs.
            latest_m1 = source.index[-1]
            if latest_m1.tzinfo is None:
                latest_m1 = latest_m1.tz_localize("UTC")
            cutoff = latest_m1.floor(freq)
            aggregated = aggregated[aggregated.index < cutoff]
            return aggregated.astype(float)
        except Exception as exc:
            log.warning("Could not derive %s bars from m1 data: %s", freq, exc)
            return None

    async def _persist_to_database(self, sym: str, df: pd.DataFrame, timeframe: str):
        """Persist market data to database asynchronously"""
        try:
            instrument_id = await db_manager.get_instrument_id(sym)
            if not instrument_id:
                log.warning("[%s] Instrument not found in database, skipping persistence", sym)
                return

            # Insert each bar
            for timestamp, row in df.iterrows():
                await db_manager.insert_market_data(
                    instrument_id=instrument_id,
                    timeframe=timeframe,
                    timestamp_utc=timestamp.to_pydatetime(),
                    open_price=float(row['open']),
                    high_price=float(row['high']),
                    low_price=float(row['low']),
                    close_price=float(row['close']),
                    volume=int(row['volume']),
                    metaapi_account_id=self.config.metaapi_account_id,
                )

            log.debug(
                "[%s] Persisted %d %s bars to database: %s -> %s",
                sym,
                len(df),
                timeframe,
                df.index[0].isoformat(),
                df.index[-1].isoformat(),
            )

        except Exception as e:
            first_ts = df.index[0].isoformat() if df is not None and not df.empty else ""
            last_ts = df.index[-1].isoformat() if df is not None and not df.empty else ""
            log.error(
                "[%s] Database persistence failed timeframe=%s rows=%s range=%s -> %s error_type=%s error=%s details=%s",
                sym,
                timeframe,
                0 if df is None else len(df),
                first_ts,
                last_ts,
                type(e).__name__,
                e,
                self._exception_details(e),
            )

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

        task = asyncio.create_task(_task_wrapper())
        self._pending_tasks.append(task)
        return task

    @staticmethod
    def _exception_details(exc) -> object:
        details = getattr(exc, "details", None)
        if details is not None:
            return details
        response = getattr(exc, "response", None)
        if response is not None:
            return response
        return getattr(exc, "__dict__", {}) or None

    def _load_persisted_frame(self, sym: str, timeframe: str) -> Optional[pd.DataFrame]:
        path = self._data_dir / f"{sym}_{timeframe}.csv"
        if not path.exists():
            return None
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            if df.empty:
                return None
            df = self._normalize_index(df)
            for col in ["open", "high", "low", "close", "volume"]:
                if col not in df.columns:
                    df[col] = 0.0
            return df[["open", "high", "low", "close", "volume"]].astype(float)
        except Exception as exc:
            log.warning("[%s] Could not load persisted %s CSV: %s", sym, timeframe, exc)
            return None

    def _load_persisted_m1(self, sym: str) -> Optional[pd.DataFrame]:
        return self._load_persisted_frame(sym, "m1")
