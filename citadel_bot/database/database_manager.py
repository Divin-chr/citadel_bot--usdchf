"""
Database connection manager for Citadel Quant Bot
Provides async database operations with connection pooling
"""

import asyncio
import asyncpg
import json
import logging
import os
import yaml
import ssl
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
from pathlib import Path

log = logging.getLogger("database")

class DatabaseManager:
    """
    Centralized database connection manager with connection pooling.
    Handles all database operations for the Citadel Quant Bot.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = self._build_config(config)
        self.pool: Optional[asyncpg.Pool] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def configure(self, config: Optional[Dict[str, Any]] = None):
        """Refresh settings without replacing the global manager object."""
        self.config = self._build_config(config)

    def _build_config(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        file_config = self._load_config_file()
        merged = {}
        if file_config:
            merged.update(file_config)
        if config:
            merged.update(config)

        pool_settings = {
            'min_size': int(os.environ.get('DATABASE_POOL_MIN_SIZE') or merged.get('min_size') or 1),
            'max_size': int(os.environ.get('DATABASE_POOL_MAX_SIZE') or merged.get('max_size') or 5),
            'max_queries': int(os.environ.get('DATABASE_MAX_QUERIES') or merged.get('max_queries') or 50000),
            'max_inactive_connection_lifetime': float(
                os.environ.get('DATABASE_MAX_INACTIVE_CONNECTION_LIFETIME')
                or merged.get('max_inactive_connection_lifetime')
                or 300.0
            ),
        }

        dsn = (
            os.environ.get("DATABASE_URL")
            or os.environ.get("CITADEL_DATABASE_URL")
            or merged.get("database_url")
        )
        if dsn:
            # Ensure Render's SSL requirement is met
            if '?' not in dsn:
                dsn += '?sslmode=require'
            elif 'sslmode' not in dsn:
                dsn += '&sslmode=require'
            # Build SSL context based on environment/config to allow
            # custom CA or opt-out for self-signed certificates.
            ssl_option = self._build_ssl_option(merged)
            return {'dsn': dsn, **pool_settings, 'ssl': ssl_option}

        return {
            'host': (
                os.environ.get('DATABASE_HOST')
                or os.environ.get('CITADEL_DATABASE_HOST')
                or merged.get('host')
                or merged.get('database_host')
                or 'localhost'
            ),
            'port': int(
                os.environ.get('DATABASE_PORT')
                or os.environ.get('CITADEL_DATABASE_PORT')
                or merged.get('port')
                or merged.get('database_port')
                or 5432
            ),
            'user': (
                os.environ.get('DATABASE_USER')
                or os.environ.get('CITADEL_DATABASE_USER')
                or merged.get('user')
                or merged.get('database_user')
                or 'postgres'
            ),
            'password': (
                os.environ.get('DATABASE_PASSWORD')
                or os.environ.get('CITADEL_DATABASE_PASSWORD')
                or merged.get('password')
                or merged.get('database_password')
                or ''
            ),
            'database': (
                os.environ.get('DATABASE_NAME')
                or os.environ.get('CITADEL_DATABASE_NAME')
                or merged.get('database')
                or merged.get('database_name')
                or 'citadel_bot'
            ),
            'ssl': self._build_ssl_option(merged),
            **pool_settings,
        }

    def _build_ssl_option(self, merged: Dict[str, Any]):
        """Return an object suitable for asyncpg's `ssl` parameter.

        Precedence:
        - `DATABASE_SSL` / config `ssl` set to false → disable TLS
        - `DATABASE_SSL_ROOT_CERT` / config `sslrootcert` → verify using a CA bundle
        - `DATABASE_SSL_NO_VERIFY` / config `ssl_no_verify` → enable TLS without certificate verification
        - otherwise → use TLS without verification by default

        Render's managed PostgreSQL commonly presents a self-signed certificate chain.
        A verified default causes connection startup to fail with:
        `[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate`.
        """
        ssl_env = os.environ.get('DATABASE_SSL')
        if ssl_env is None:
            ssl_env = merged.get('ssl')

        # Hard disable gate: if explicitly disabled, never attempt TLS.
        if ssl_env is not None and str(ssl_env).lower() in ('false', 'disable', '0', 'no'):
            return False

        # Some callers/hosts may set DATABASE_SSL=false implicitly via DATABASE_SSL_NO_VERIFY.
        # If the user explicitly wants no verification, keep TLS but relax verification.

        # IMPORTANT:
        # For local Postgres (SSL disabled) and hosted Postgres (SSL required), behavior
        # depends on provider.
        # Default logic:
        # - If ssl_env unset and we *don't* have an explicit dsn, do NOT force TLS off.
        #   Let asyncpg/driver determine based on server requirement.
        # - If DSN is present but missing sslmode, earlier code appends sslmode=require.



        root = (
            os.environ.get('DATABASE_SSL_ROOT_CERT')
            or merged.get('sslrootcert')
            or merged.get('ssl_root_cert')
        )
        if root:
            try:
                return ssl.create_default_context(cafile=root)
            except Exception as exc:
                log.warning("Failed to load DATABASE_SSL_ROOT_CERT (%s); falling back to non-verifying TLS: %s", root, exc)

        no_verify = os.environ.get('DATABASE_SSL_NO_VERIFY') or merged.get('ssl_no_verify')
        if str(no_verify).lower() in ('1', 'true', 'yes', 'on'):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx

        # Default to non-verifying TLS so hosted DBs with self-signed chains work out of the box.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _load_config_file(self) -> Dict[str, Any]:
        for path in (Path("config.yaml"), Path("citadel_bot/config/config.yaml")):
            if not path.exists():
                continue
            try:
                with path.open(encoding="utf-8-sig") as f:
                    return yaml.safe_load(f) or {}
            except Exception as exc:
                log.debug("Could not load database settings from %s: %s", path, exc)
        return {}

    async def connect(self):
        """Initialize connection pool"""
        current_loop = asyncio.get_running_loop()
        if self.pool:
            if self._loop is current_loop:
                return
            log.warning("Database pool was created on a different event loop; reconnecting pool on current loop")
            await self.disconnect()

        try:
            self._loop = current_loop
            # Log connection details (without exposing password)
            if 'dsn' in self.config:
                # Mask password in DSN
                dsn_masked = self.config['dsn']
                if '@' in dsn_masked:
                    before_at = dsn_masked.split('@')[0]
                    after_at = dsn_masked.split('@')[1]
                    if ':' in before_at:
                        user = before_at.split(':')[0]
                        dsn_masked = f"{user}:***@{after_at}"
                log.info(f"📡 Connecting to database (DSN): {dsn_masked}")
            else:
                log.info(f"📡 Connecting to database: {self.config.get('user')}@{self.config.get('host')}:{self.config.get('port')}/{self.config.get('database')}")
            
            self.pool = await asyncpg.create_pool(**self.config)
            log.info("✅ Database connection pool initialized")
        except Exception as e:
            self._loop = None
            log.error(f"❌ Failed to initialize database pool: {e}")
            raise

    async def disconnect(self):
        """Close connection pool"""
        if self.pool:
            try:
                if self._loop is not None and self._loop is not asyncio.get_running_loop():
                    future = asyncio.run_coroutine_threadsafe(self.pool.close(), self._loop)
                    future.result(timeout=5)
                else:
                    await self.pool.close()
            except Exception as exc:
                log.warning("Failed to close database pool: %s", exc)
            finally:
                self.pool = None
                self._loop = None
                log.info("✅ Database connection pool closed")

    async def initialize_schema(self):
        """Run database_schema.sql if tables don't exist"""
        async with self.connection() as conn:
            # Check if instruments table exists
            exists = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_schema = 'public' 
                    AND table_name = 'instruments'
                );
            """)
            if not exists:
                log.info("Database schema not found. Initializing...")
                schema_path = Path(__file__).parent / "database_schema.sql"
                try:
                    with open(schema_path, "r", encoding="utf-8") as f:
                        schema_sql = f.read()
                    await conn.execute(schema_sql)
                    log.info("✅ Database schema initialized successfully")
                    
                except Exception as e:
                    log.error(f"❌ Failed to initialize schema: {e}")
            await self._upgrade_schema(conn)
            await self._seed_instruments(conn)

    async def _upgrade_schema(self, conn):
        """Apply small idempotent upgrades needed by current write paths."""
        market_data_exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'market_data'
            );
        """)
        if not market_data_exists:
            return

        column_exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'market_data'
                  AND column_name = 'metaapi_account_id'
            );
        """)
        if not column_exists:
            await conn.execute(
                "ALTER TABLE market_data ADD COLUMN IF NOT EXISTS metaapi_account_id VARCHAR(128) NOT NULL DEFAULT '';"
            )
        else:
            await conn.execute("UPDATE market_data SET metaapi_account_id = '' WHERE metaapi_account_id IS NULL;")
            await conn.execute("ALTER TABLE market_data ALTER COLUMN metaapi_account_id SET DEFAULT '';")
            await conn.execute("ALTER TABLE market_data ALTER COLUMN metaapi_account_id SET NOT NULL;")

        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_market_data_account ON market_data (instrument_id, timestamp_utc, timeframe, metaapi_account_id);"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_data_account ON market_data(metaapi_account_id);"
        )
        data_source_exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'market_data'
                  AND column_name = 'data_source'
            );
        """)
        if not data_source_exists:
            await conn.execute(
                "ALTER TABLE market_data ADD COLUMN IF NOT EXISTS data_source VARCHAR(30) NOT NULL DEFAULT 'historical_candles';"
            )
        else:
            await conn.execute("UPDATE market_data SET data_source = 'historical_candles' WHERE data_source IS NULL;")
            await conn.execute("ALTER TABLE market_data ALTER COLUMN data_source SET DEFAULT 'historical_candles';")
            await conn.execute("ALTER TABLE market_data ALTER COLUMN data_source SET NOT NULL;")

        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_data_source ON market_data(data_source);"
        )
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS terminal_position_prices (
                snapshot_id BIGSERIAL PRIMARY KEY,
                instrument_id INTEGER NOT NULL REFERENCES instruments(instrument_id),
                metaapi_account_id VARCHAR(128) NOT NULL DEFAULT '',
                timestamp_utc TIMESTAMP WITH TIME ZONE NOT NULL,
                position_id VARCHAR(64),
                direction VARCHAR(5) CHECK (direction IN ('LONG', 'SHORT')),
                volume DECIMAL(12,4),
                open_price DECIMAL(12,5),
                current_price DECIMAL(12,5) NOT NULL,
                profit DECIMAL(12,2),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_terminal_position_prices_instrument_time
            ON terminal_position_prices(instrument_id, timestamp_utc DESC);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_terminal_position_prices_account
            ON terminal_position_prices(metaapi_account_id);
        """)

        # ── Grid strategy tables (Teeple 2025) ───────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS grid_calibration (
                calibration_id SERIAL PRIMARY KEY,
                instrument_id INTEGER NOT NULL REFERENCES instruments(instrument_id),
                run_timestamp TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                candidates DOUBLE PRECISION[] NOT NULL,
                cov_mod_by_candidate DOUBLE PRECISION[] NOT NULL,
                pvalue_by_candidate DOUBLE PRECISION[] NOT NULL,
                epsilon DOUBLE PRECISION NOT NULL,
                cov_mod DOUBLE PRECISION NOT NULL,
                p_value DOUBLE PRECISION NOT NULL,
                is_significant BOOLEAN NOT NULL,
                n_bars INTEGER NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_grid_calibration_instrument
                ON grid_calibration(instrument_id);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_grid_calibration_run_timestamp
                ON grid_calibration(run_timestamp DESC);
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS grid_signal_logs (
                signal_id BIGSERIAL PRIMARY KEY,
                timestamp_utc TIMESTAMP WITH TIME ZONE NOT NULL,
                instrument_id INTEGER NOT NULL REFERENCES instruments(instrument_id),
                epsilon DOUBLE PRECISION,
                grid_below DOUBLE PRECISION,
                grid_above DOUBLE PRECISION,
                midpoint DOUBLE PRECISION,
                regime_position DOUBLE PRECISION,
                cov_mod DOUBLE PRECISION,
                cov_mod_pvalue DOUBLE PRECISION,
                signal_emitted BOOLEAN NOT NULL DEFAULT FALSE,
                signal_mode VARCHAR(20),
                rejection_gate VARCHAR(50),
                direction VARCHAR(5),
                confidence DECIMAL(6,4),
                entry_price DECIMAL(12,5),
                stop_loss DECIMAL(12,5),
                tp1 DECIMAL(12,5),
                tp2 DECIMAL(12,5),
                rr_ratio DECIMAL(6,2),
                atr DECIMAL(12,5),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_grid_signal_logs_timestamp
                ON grid_signal_logs(timestamp_utc DESC);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_grid_signal_logs_instrument
                ON grid_signal_logs(instrument_id);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_grid_signal_logs_signal_emitted
                ON grid_signal_logs(signal_emitted);
        """)

    async def _seed_instruments(self, conn):
        """Seed or repair instrument catalog rows even when schema already exists."""
        instruments_exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'instruments'
            );
        """)
        if not instruments_exists:
            return

        from citadel_bot.utils.instrument_catalog import CATALOG

        inserted_or_updated = 0
        for sym, inst in CATALOG.items():
            result = await conn.execute("""
                INSERT INTO instruments (
                    symbol, display_name, category, base_currency, quote_currency,
                    multiplier, exchange, session, description, typical_spread, aliases
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (symbol) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    category = EXCLUDED.category,
                    base_currency = EXCLUDED.base_currency,
                    quote_currency = EXCLUDED.quote_currency,
                    multiplier = EXCLUDED.multiplier,
                    exchange = EXCLUDED.exchange,
                    session = EXCLUDED.session,
                    description = EXCLUDED.description,
                    typical_spread = EXCLUDED.typical_spread,
                    aliases = EXCLUDED.aliases
            """,
            sym,
            inst.display_name,
            inst.category,
            inst.base_currency,
            inst.quote_currency,
            inst.multiplier,
            inst.exchange,
            inst.session,
            inst.description,
            inst.typical_spread,
            inst.aliases,
            )
            if result in {"INSERT 0 1", "UPDATE 1"}:
                inserted_or_updated += 1

        instrument_count = await conn.fetchval("SELECT COUNT(*) FROM instruments")
        log.info("✅ Instrument catalog ready: %s rows (%s touched)", instrument_count, inserted_or_updated)

    @asynccontextmanager
    async def connection(self):
        """Get a connection from the pool"""
        if not self.pool:
            raise RuntimeError("Database pool not initialized")
        if self._loop is not asyncio.get_running_loop():
            log.warning("Database pool loop mismatch detected; reconnecting pool on current event loop")
            await self.disconnect()
            await self.connect()
        async with self.pool.acquire() as conn:
            yield conn

    # =================================================================================
    # INSTRUMENT OPERATIONS
    # =================================================================================

    async def get_instrument_id(self, symbol: str) -> Optional[int]:
        """Get instrument ID by symbol"""
        async with self.connection() as conn:
            row = await conn.fetchrow(
                "SELECT instrument_id FROM instruments WHERE symbol = $1",
                symbol
            )
            return row['instrument_id'] if row else None

    async def get_instrument_info(self, symbol: str) -> Optional[Dict]:
        """Get full instrument information"""
        async with self.connection() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM instruments WHERE symbol = $1
            """, symbol)
            return dict(row) if row else None

    async def get_all_instruments(self) -> List[Dict]:
        """Get all instruments"""
        async with self.connection() as conn:
            rows = await conn.fetch("SELECT * FROM instruments ORDER BY symbol")
            return [dict(row) for row in rows]

    # =================================================================================
    # MARKET DATA OPERATIONS
    # =================================================================================

    async def insert_market_data(
        self,
        instrument_id: int,
        timeframe: str,
        timestamp_utc,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        volume: int,
        metaapi_account_id: Optional[str] = None,
        data_source: str = "historical_candles",
    ):
        """Insert market data bar"""
        account_id = metaapi_account_id or ''
        source = data_source or "historical_candles"
        async with self.connection() as conn:
            await conn.execute("""
                INSERT INTO market_data (
                    instrument_id, timestamp_utc, timeframe, data_source,
                    open_price, high_price, low_price, close_price, volume,
                    metaapi_account_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (instrument_id, timestamp_utc, timeframe, metaapi_account_id) DO UPDATE SET
                    data_source = EXCLUDED.data_source,
                    open_price = EXCLUDED.open_price,
                    high_price = EXCLUDED.high_price,
                    low_price = EXCLUDED.low_price,
                    close_price = EXCLUDED.close_price,
                    volume = EXCLUDED.volume
            """,
            instrument_id, timestamp_utc, timeframe,
            source, open_price, high_price, low_price, close_price, volume,
            account_id
            )

    async def insert_terminal_position_price(
        self,
        instrument_id: int,
        timestamp_utc,
        current_price: float,
        metaapi_account_id: Optional[str] = None,
        position_id: Optional[str] = None,
        direction: Optional[str] = None,
        volume: Optional[float] = None,
        open_price: Optional[float] = None,
        profit: Optional[float] = None,
    ):
        """Persist a raw live position price snapshot from MetaApi terminal state."""
        account_id = metaapi_account_id or ''
        async with self.connection() as conn:
            await conn.execute("""
                INSERT INTO terminal_position_prices (
                    instrument_id, metaapi_account_id, timestamp_utc,
                    position_id, direction, volume, open_price, current_price, profit
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            instrument_id,
            account_id,
            timestamp_utc,
            position_id,
            direction,
            volume,
            open_price,
            current_price,
            profit,
            )

    async def upsert_terminal_position_market_data(
        self,
        instrument_id: int,
        timestamp_utc,
        current_price: float,
        metaapi_account_id: Optional[str] = None,
    ):
        """Build a terminal-derived m1 OHLC row without overwriting broker candles."""
        account_id = metaapi_account_id or ''
        async with self.connection() as conn:
            await conn.execute("""
                INSERT INTO market_data (
                    instrument_id, timestamp_utc, timeframe, data_source,
                    open_price, high_price, low_price, close_price, volume,
                    metaapi_account_id
                ) VALUES ($1, $2, 'm1', 'terminal_position', $3, $3, $3, $3, 1, $4)
                ON CONFLICT (instrument_id, timestamp_utc, timeframe, metaapi_account_id) DO UPDATE SET
                    high_price = GREATEST(market_data.high_price, EXCLUDED.high_price),
                    low_price = LEAST(market_data.low_price, EXCLUDED.low_price),
                    close_price = EXCLUDED.close_price,
                    volume = market_data.volume + 1,
                    data_source = 'terminal_position'
                WHERE market_data.data_source = 'terminal_position'
            """,
            instrument_id,
            timestamp_utc,
            current_price,
            account_id,
            )
    async def insert_market_data_legacy(
        self,
        instrument_id: int,
        timeframe: str,
        timestamp_utc,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        volume: int,
    ):
        """Insert market data into older schemas that predate account scoping."""
        async with self.connection() as conn:
            await conn.execute("""
                    INSERT INTO market_data (
                        instrument_id, timestamp_utc, timeframe,
                        open_price, high_price, low_price, close_price, volume
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (instrument_id, timestamp_utc, timeframe) DO UPDATE SET
                        open_price = EXCLUDED.open_price,
                        high_price = EXCLUDED.high_price,
                        low_price = EXCLUDED.low_price,
                        close_price = EXCLUDED.close_price,
                        volume = EXCLUDED.volume
                """,
                instrument_id, timestamp_utc, timeframe,
                open_price, high_price, low_price, close_price, volume
                )

    async def get_market_data(
        self,
        symbol: str,
        timeframe: str = 'm1',
        limit: int = 400,
        metaapi_account_id: Optional[str] = None,
        data_source: Optional[str] = None,
    ) -> Optional[List[Dict]]:
        """Get recent market data for symbol"""
        instrument_id = await self.get_instrument_id(symbol)
        if not instrument_id:
            return None

        async with self.connection() as conn:
            if metaapi_account_id is not None:
                try:
                    rows = await conn.fetch("""
                        SELECT * FROM market_data
                        WHERE instrument_id = $1 AND timeframe = $2 AND metaapi_account_id = $4
                          AND ($5::varchar IS NULL OR data_source = $5)
                        ORDER BY timestamp_utc DESC
                        LIMIT $3
                    """, instrument_id, timeframe, limit, metaapi_account_id, data_source)
                except Exception:
                    rows = await conn.fetch("""
                        SELECT * FROM market_data
                        WHERE instrument_id = $1 AND timeframe = $2
                        ORDER BY timestamp_utc DESC
                        LIMIT $3
                    """, instrument_id, timeframe, limit)
            else:
                rows = await conn.fetch("""
                    SELECT * FROM market_data
                    WHERE instrument_id = $1 AND timeframe = $2
                      AND ($4::varchar IS NULL OR data_source = $4)
                    ORDER BY timestamp_utc DESC
                    LIMIT $3
                """, instrument_id, timeframe, limit, data_source)

            return [dict(row) for row in rows]
    async def get_latest_market_data(
        self,
        symbol: str,
        timeframe: str = 'm1',
        metaapi_account_id: Optional[str] = None,
    ) -> Optional[Dict]:
        """Get latest market data bar for symbol"""
        instrument_id = await self.get_instrument_id(symbol)
        if not instrument_id:
            return None

        async with self.connection() as conn:
            if metaapi_account_id is not None:
                try:
                    row = await conn.fetchrow("""
                        SELECT * FROM market_data
                        WHERE instrument_id = $1 AND timeframe = $2 AND metaapi_account_id = $3
                        ORDER BY timestamp_utc DESC
                        LIMIT 1
                    """, instrument_id, timeframe, metaapi_account_id)
                except Exception:
                    row = await conn.fetchrow("""
                        SELECT * FROM market_data
                        WHERE instrument_id = $1 AND timeframe = $2
                        ORDER BY timestamp_utc DESC
                        LIMIT 1
                    """, instrument_id, timeframe)
            else:
                row = await conn.fetchrow("""
                    SELECT * FROM market_data
                    WHERE instrument_id = $1 AND timeframe = $2
                    ORDER BY timestamp_utc DESC
                    LIMIT 1
                """, instrument_id, timeframe)

            return dict(row) if row else None

    # =================================================================================
    # SIGNAL LOG OPERATIONS
    # =================================================================================

    async def insert_signal_log(self, signal_data: Dict):
        """Insert signal log entry"""
        async with self.connection() as conn:
            await conn.execute("""
                INSERT INTO signal_logs (
                    timestamp_utc, instrument_id, score_trend, score_momentum,
                    score_acceleration, score_volatility, score_structure,
                    trend_daily, trend_weekly, trend_monthly, rsi, macd_hist,
                    macd_cross, bb_pct, bb_squeeze, atr, atr_pct, volume_ratio,
                    nearest_support, nearest_resistance, patterns,
                    composite_score, confidence, direction, rt_momentum,
                    delta_aligned, alignment_score, signal_emitted, rejection_gate,
                    entry_price, stop_loss, tp1, tp2, rr_ratio, vol_regime
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                    $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24,
                    $25, $26, $27, $28, $29, $30, $31, $32, $33, $34, $35
                )
            """,
            signal_data['timestamp_utc'],
            signal_data['instrument_id'],
            signal_data.get('score_trend'),
            signal_data.get('score_momentum'),
            signal_data.get('score_acceleration'),
            signal_data.get('score_volatility'),
            signal_data.get('score_structure'),
            signal_data.get('trend_daily'),
            signal_data.get('trend_weekly'),
            signal_data.get('trend_monthly'),
            signal_data.get('rsi'),
            signal_data.get('macd_hist'),
            signal_data.get('macd_cross'),
            signal_data.get('bb_pct'),
            signal_data.get('bb_squeeze'),
            signal_data.get('atr'),
            signal_data.get('atr_pct'),
            signal_data.get('volume_ratio'),
            signal_data.get('nearest_support'),
            signal_data.get('nearest_resistance'),
            signal_data.get('patterns', []),
            signal_data['composite_score'],
            signal_data['confidence'],
            signal_data['direction'],
            signal_data.get('rt_momentum'),
            signal_data.get('delta_aligned'),
            signal_data.get('alignment_score'),
            signal_data['signal_emitted'],
            signal_data.get('rejection_gate'),
            signal_data.get('entry_price'),
            signal_data.get('stop_loss'),
            signal_data.get('tp1'),
            signal_data.get('tp2'),
            signal_data.get('rr_ratio'),
            signal_data.get('vol_regime', 'NORMAL')
            )

    # =================================================================================
    # TRADE LEDGER OPERATIONS
    # =================================================================================

    async def insert_trade_ledger_entry(self, trade_data: Dict):
        """Insert trade ledger entry"""
        async with self.connection() as conn:
            await conn.execute("""
                INSERT INTO trade_ledger (
                    timestamp_utc, event_type, mode, instrument_id,
                    parent_order_id, order_id, direction, qty_delta,
                    qty_open, fill_price, pnl_delta_usd, realized_pnl_usd,
                    status, note
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            """,
            trade_data['timestamp_utc'],
            trade_data['event_type'],
            trade_data['mode'],
            trade_data['instrument_id'],
            trade_data.get('parent_order_id'),
            trade_data.get('order_id'),
            trade_data['direction'],
            trade_data['qty_delta'],
            trade_data['qty_open'],
            trade_data.get('fill_price'),
            trade_data.get('pnl_delta_usd'),
            trade_data.get('realized_pnl_usd'),
            trade_data.get('status'),
            trade_data.get('note')
            )

    # =================================================================================
    # BUFFER CALIBRATION OPERATIONS
    # =================================================================================

    # =================================================================================
    # GRID CALIBRATION (Teeple 2025)
    # =================================================================================

    async def get_optimal_grid_spacing(self, symbol: str) -> float:
        """Return the most-recent significant ε for symbol, or 0.0 if none."""
        instrument_id = await self.get_instrument_id(symbol)
        if not instrument_id:
            return 0.0
        async with self.connection() as conn:
            row = await conn.fetchrow("""
                SELECT epsilon
                FROM grid_calibration
                WHERE instrument_id = $1 AND is_significant = true
                ORDER BY run_timestamp DESC
                LIMIT 1
            """, instrument_id)
            return float(row['epsilon']) if row else 0.0

    async def save_grid_calibration(self, data: Dict):
        async with self.connection() as conn:
            await conn.execute("""
                INSERT INTO grid_calibration (
                    instrument_id, run_timestamp,
                    candidates, cov_mod_by_candidate, pvalue_by_candidate,
                    epsilon, cov_mod, p_value, is_significant, n_bars
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            data['instrument_id'],
            data['run_timestamp'],
            data['candidates'],
            data['cov_mod_by_candidate'],
            data['pvalue_by_candidate'],
            data['epsilon'],
            data['cov_mod'],
            data['p_value'],
            data['is_significant'],
            data['n_bars'])

    async def insert_grid_signal_log(self, row: Dict):
        async with self.connection() as conn:
            await conn.execute("""
                INSERT INTO grid_signal_logs (
                    timestamp_utc, instrument_id,
                    epsilon, grid_below, grid_above, midpoint, regime_position,
                    cov_mod, cov_mod_pvalue,
                    signal_emitted, signal_mode, rejection_gate,
                    direction, confidence,
                    entry_price, stop_loss, tp1, tp2, rr_ratio, atr
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20)
            """,
            row['timestamp_utc'],
            row['instrument_id'],
            row.get('epsilon'),
            row.get('grid_below'),
            row.get('grid_above'),
            row.get('midpoint'),
            row.get('regime_position'),
            row.get('cov_mod'),
            row.get('cov_mod_pvalue'),
            bool(row.get('signal_emitted', False)),
            row.get('signal_mode') or None,
            row.get('rejection_gate') or None,
            row.get('direction') or None,
            row.get('confidence'),
            row.get('entry_price'),
            row.get('stop_loss'),
            row.get('tp1'),
            row.get('tp2'),
            row.get('rr_ratio'),
            row.get('atr'))

    # =================================================================================
    # LEGACY BUFFER CALIBRATION — table retained for history; no longer written.
    # =================================================================================

    async def get_optimal_buffer_delay(self, symbol: str) -> int:
        """Deprecated: legacy buffer-delay strategy. Kept for backwards compat."""
        instrument_id = await self.get_instrument_id(symbol)
        if not instrument_id:
            return 12  # Default fallback

        async with self.connection() as conn:
            row = await conn.fetchrow("""
                SELECT optimal_delay_min
                FROM buffer_calibration
                WHERE instrument_id = $1 AND is_significant = true
                ORDER BY run_timestamp DESC
                LIMIT 1
            """, instrument_id)

            return row['optimal_delay_min'] if row else 12

    async def save_buffer_calibration(self, calibration_data: Dict):
        """Save buffer calibration results"""
        async with self.connection() as conn:
            await conn.execute("""
                INSERT INTO buffer_calibration (
                    instrument_id, run_timestamp, min_delay_min, max_delay_min,
                    step_min, calibration_window_days, optimal_delay_min,
                    best_sharpe, p_value, is_significant, n_bars, n_windows,
                    candidates, delay_mean_val_sharpe, window_winners
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            """,
            calibration_data['instrument_id'],
            calibration_data['run_timestamp'],
            calibration_data['min_delay_min'],
            calibration_data['max_delay_min'],
            calibration_data['step_min'],
            calibration_data['calibration_window_days'],
            calibration_data['optimal_delay_min'],
            calibration_data['best_sharpe'],
            calibration_data['p_value'],
            calibration_data['is_significant'],
            calibration_data['n_bars'],
            calibration_data['n_windows'],
            calibration_data['candidates'],
            calibration_data['delay_mean_val_sharpe'],
            calibration_data['window_winners']
            )

    # =================================================================================
    # UTILITY METHODS
    # =================================================================================

    async def health_check(self) -> bool:
        """Check database connectivity"""
        try:
            async with self.connection() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False

    async def get_stats(self) -> Dict:
        """Get database statistics"""
        async with self.connection() as conn:
            stats = {}

            # Table counts
            for table in ['instruments', 'market_data', 'signal_logs', 'trade_ledger', 'buffer_calibration']:
                count = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")
                stats[f'{table}_count'] = count

            return stats

# Global database manager instance
db_manager = DatabaseManager()

async def init_database(config: Optional[Dict] = None) -> DatabaseManager:
    """Initialize global database manager"""
    db_manager.configure(config)
    await db_manager.connect()
    await db_manager.initialize_schema()
    return db_manager

async def close_database():
    """Close global database manager"""
    await db_manager.disconnect()
