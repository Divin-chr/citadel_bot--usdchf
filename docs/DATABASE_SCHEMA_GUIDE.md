# Citadel Bot Database Schema Guide

This file is a plain-English map of the PostgreSQL database used by the Citadel Quant Bot.
The source-of-truth DDL lives in `citadel_bot/database/database_schema.sql`.

## Big Picture

The database is built around instruments. Most operational tables store an `instrument_id`
foreign key back to `instruments`.

```text
instruments
  |-- market_data
  |-- signal_logs
  |-- trade_ledger
  |-- buffer_calibration

economic_calendar
bot_config_history
```

Core flow:

1. `instruments` defines tradable symbols such as `NDAQ`, `US500`, or `EURUSD`.
2. `market_data` stores OHLCV bars for each instrument and timeframe.
3. `signal_logs` stores every analysis pass and whether a trade signal was emitted.
4. `trade_ledger` stores position/order events after signals become trades.
5. `buffer_calibration` stores adaptive delay calculations per instrument.
6. `economic_calendar` stores macro events that may affect trading.
7. `bot_config_history` stores snapshots of bot settings for audit/debugging.

## Tables

### `instruments`

Master catalog of all tradable instruments.

Primary key:

- `instrument_id`

Important columns:

- `symbol`: Unique trading symbol, for example `NDAQ`.
- `display_name`: Human-friendly name.
- `category`: One of `indices`, `forex`, `commodities`, `crypto`.
- `base_currency`, `quote_currency`: Currency metadata.
- `multiplier`: Contract/price multiplier used by risk and sizing logic.
- `exchange`: Venue or exchange label.
- `session`: Trading session label.
- `typical_spread`: Expected spread.
- `aliases`: PostgreSQL text array of alternate symbol names.
- `created_at`, `updated_at`: Audit timestamps.

Used by:

- Nearly every other table through `instrument_id`.
- `DatabaseManager.get_instrument_id()`
- `DatabaseManager.get_instrument_info()`
- `DatabaseManager.get_all_instruments()`

Indexes:

- `idx_instruments_symbol`
- `idx_instruments_category`

### `market_data`

Time series OHLCV bars used by analysis and backfilling.

Primary key:

- `id`

Foreign key:

- `instrument_id -> instruments.instrument_id`

Important columns:

- `timestamp_utc`: Bar timestamp.
- `timeframe`: One of `m1`, `m5`, `h1`, `d1`, `w1`.
- `open_price`, `high_price`, `low_price`, `close_price`: OHLC prices.
- `volume`: Bar volume.
- `created_at`: Insert timestamp.

Uniqueness rule:

- One bar per `instrument_id + timestamp_utc + timeframe`.
- Inserts use upsert behavior in `DatabaseManager.insert_market_data()`, so a repeated bar updates OHLCV values instead of creating a duplicate.

Used by:

- `data_pipeline.py` to save and read bars.
- `DatabaseManager.get_market_data()`
- `DatabaseManager.get_latest_market_data()`

Indexes:

- `idx_market_data_instrument_time`
- `idx_market_data_timeframe`

### `signal_logs`

Detailed record of the bot's analysis decisions.

Primary key:

- `signal_id`

Foreign key:

- `instrument_id -> instruments.instrument_id`

Important column groups:

- Timestamp: `timestamp_utc`, `created_at`
- Technical score groups: `score_trend`, `score_momentum`, `score_acceleration`, `score_volatility`, `score_structure`
- Raw indicators: `trend_daily`, `trend_weekly`, `trend_monthly`, `rsi`, `macd_hist`, `macd_cross`, `bb_pct`, `bb_squeeze`, `atr`, `atr_pct`, `volume_ratio`
- Structure/pattern context: `nearest_support`, `nearest_resistance`, `patterns`, `vol_regime`
- Decision fields: `composite_score`, `confidence`, `direction`, `signal_emitted`, `rejection_gate`
- Real-time alignment: `rt_momentum`, `delta_aligned`, `alignment_score`
- Trade setup: `entry_price`, `stop_loss`, `tp1`, `tp2`, `rr_ratio`

Decision meanings:

- `direction`: `1` long, `-1` short, `0` neutral/no direction.
- `signal_emitted`: `true` when the bot accepted the setup.
- `rejection_gate`: Reason a signal was rejected, when applicable.

Used by:

- `signal_logger.py`
- `dashboard_service.py`
- Analytics and query scripts in `citadel_bot/database/`

Indexes:

- `idx_signal_logs_timestamp`
- `idx_signal_logs_instrument`
- `idx_signal_logs_composite_score`
- `idx_signal_logs_signal_emitted`

### `trade_ledger`

Audit ledger of order and position events.

Primary key:

- `trade_id`

Foreign key:

- `instrument_id -> instruments.instrument_id`

Important columns:

- `timestamp_utc`: Event time.
- `event_type`: One of `ENTRY_FILL`, `EXIT_FILL`, `POSITION_CLOSED`, `MODIFY`.
- `mode`: `paper` or `live`.
- `parent_order_id`, `order_id`: Broker/order references.
- `direction`: `LONG` or `SHORT`.
- `qty_delta`: Quantity change for this event.
- `qty_open`: Remaining open quantity after this event.
- `fill_price`: Fill price when available.
- `pnl_delta_usd`: P&L change for the event.
- `realized_pnl_usd`: Realized P&L after close/exit events.
- `status`, `note`: Free-form state/debug context.

Used by:

- `execution_engine.py`
- `dashboard_service.py`
- Analytics scripts.

Indexes:

- `idx_trade_ledger_timestamp`
- `idx_trade_ledger_instrument`
- `idx_trade_ledger_mode`
- `idx_trade_ledger_order_id`

### `buffer_calibration`

Stores adaptive buffer-delay calibration results.

Primary key:

- `calibration_id`

Foreign key:

- `instrument_id -> instruments.instrument_id`

Important columns:

- `run_timestamp`: When the calibration was run.
- `min_delay_min`, `max_delay_min`, `step_min`: Candidate delay search settings.
- `calibration_window_days`: Historical window used.
- `optimal_delay_min`: Winning delay.
- `best_sharpe`: Best validation Sharpe score.
- `p_value`: Statistical significance check.
- `is_significant`: Whether the result should be trusted.
- `n_bars`, `n_windows`: Dataset/window diagnostics.
- `candidates`: Integer array of tested delay values.
- `delay_mean_val_sharpe`: Decimal array of validation Sharpe by delay.
- `window_winners`: Integer array of winning delays across windows.

Used by:

- `buffer_engine.py`
- `DatabaseManager.get_optimal_buffer_delay()`
- `DatabaseManager.save_buffer_calibration()`

Default behavior:

- If no significant calibration exists, the bot falls back to `12` minutes.

Indexes:

- `idx_buffer_calibration_instrument`
- `idx_buffer_calibration_run_timestamp`

### `economic_calendar`

External macro/event calendar.

Primary key:

- `event_id`

Important columns:

- `timestamp_utc`: Event time.
- `currency`: Affected currency, for example `USD`.
- `event_name`: Event description.
- `importance`: `low`, `medium`, or `high`.
- `forecast`, `previous`, `actual`: Numeric event values when available.
- `created_at`: Insert timestamp.

Indexes:

- `idx_economic_calendar_timestamp`
- `idx_economic_calendar_currency`

### `bot_config_history`

Configuration audit trail.

Primary key:

- `config_id`

Important columns:

- `timestamp_utc`: Snapshot time.
- `mode`: `paper` or `live`.
- `instruments`: PostgreSQL array of active symbols.
- `min_confidence`, `min_rr_ratio`, `max_risk_per_trade_pct`: Key trading thresholds.
- `use_kelly_sizing`, `signal_logging`: Feature flags.
- `config_hash`: Optional hash for change detection.
- `created_at`: Insert timestamp.

## Views

### `latest_market_data`

Returns the newest bar per `instrument_id + timeframe`, joined with `instruments.symbol`.

Good for:

- Checking current data freshness.
- Dashboard status panels.

### `signal_performance`

Returns signal logs joined with instrument symbols and a derived `outcome` field.

`outcome` is:

- `SIGNAL` when `signal_emitted = true`.
- Otherwise `rejection_gate`, falling back to `UNKNOWN`.

Good for:

- Reviewing why signals were accepted or rejected.

### `trade_summary`

Returns trade ledger rows joined with instrument symbol, display name, and category.

Good for:

- Trade review.
- P&L summaries by instrument/category.

## Utility Functions

### `get_optimal_buffer_delay(p_instrument_id INTEGER)`

Returns the latest significant `optimal_delay_min` for an instrument.

Fallback:

- Returns `12` if no significant result exists.

### `cleanup_old_market_data(days_to_keep INTEGER DEFAULT 365)`

Deletes old rows from `market_data` and returns the deleted row count.

## Common Joins

Recent signals with symbols:

```sql
SELECT
    sl.timestamp_utc,
    i.symbol,
    sl.direction,
    sl.composite_score,
    sl.confidence,
    sl.signal_emitted,
    sl.rejection_gate
FROM signal_logs sl
JOIN instruments i ON i.instrument_id = sl.instrument_id
ORDER BY sl.timestamp_utc DESC
LIMIT 50;
```

Latest market data by symbol:

```sql
SELECT
    symbol,
    timeframe,
    timestamp_utc,
    close_price,
    volume
FROM latest_market_data
ORDER BY symbol, timeframe;
```

Trade P&L by instrument:

```sql
SELECT
    i.symbol,
    COUNT(*) AS events,
    SUM(tl.realized_pnl_usd) AS realized_pnl_usd
FROM trade_ledger tl
JOIN instruments i ON i.instrument_id = tl.instrument_id
WHERE tl.event_type = 'POSITION_CLOSED'
GROUP BY i.symbol
ORDER BY realized_pnl_usd DESC NULLS LAST;
```

Most recent buffer settings:

```sql
SELECT DISTINCT ON (i.symbol)
    i.symbol,
    bc.run_timestamp,
    bc.optimal_delay_min,
    bc.best_sharpe,
    bc.p_value,
    bc.is_significant
FROM buffer_calibration bc
JOIN instruments i ON i.instrument_id = bc.instrument_id
ORDER BY i.symbol, bc.run_timestamp DESC;
```

## Code Entry Points

- Schema DDL: `citadel_bot/database/database_schema.sql`
- Connection and CRUD methods: `citadel_bot/database/database_manager.py`
- Database setup: `scripts/setup_database.py`, `scripts/setup_postgres.py`
- Migration: `scripts/migrate_to_postgres.py`
- Query examples: `docs/HOW_TO_RUN_QUERIES.md`, `docs/database_query_examples.md`
- Dashboard database reads: `citadel_bot/dashboard_service.py`

## Mental Model

Use `instruments.symbol` for human-facing work, but join and store by `instrument_id`.
Use `timestamp_utc` for event/bar time, and `created_at` for when the row was inserted.
Use `signal_logs` to understand decisions, and `trade_ledger` to understand execution outcomes.
