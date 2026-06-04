-- Citadel Quant Bot - PostgreSQL Schema (Fully Normalized)
-- Created: 2026-04-23

-- =================================================================================
-- INSTRUMENT CATALOG (Master Data)
-- =================================================================================

CREATE TABLE instruments (
    instrument_id SERIAL PRIMARY KEY,
    symbol VARCHAR(10) NOT NULL UNIQUE,
    display_name VARCHAR(50) NOT NULL,
    category VARCHAR(20) NOT NULL CHECK (category IN ('indices', 'forex', 'commodities', 'crypto')),
    base_currency VARCHAR(3),
    quote_currency VARCHAR(3) NOT NULL,
    multiplier DECIMAL(10,4) NOT NULL DEFAULT 1.0,
    exchange VARCHAR(20),
    session VARCHAR(20) NOT NULL,
    description TEXT,
    typical_spread DECIMAL(8,4),
    -- PostgreSQL array type for aliases
    aliases TEXT[],
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_instruments_symbol ON instruments(symbol);
CREATE INDEX idx_instruments_category ON instruments(category);

-- =================================================================================
-- MARKET DATA (Time Series - High Volume)
-- =================================================================================

CREATE TABLE market_data (
    id BIGSERIAL PRIMARY KEY,
    instrument_id INTEGER NOT NULL REFERENCES instruments(instrument_id),
    metaapi_account_id VARCHAR(128) NOT NULL DEFAULT '',
    timestamp_utc TIMESTAMP WITH TIME ZONE NOT NULL,
    timeframe VARCHAR(5) NOT NULL DEFAULT 'm1' CHECK (timeframe IN ('m1', 'm5', 'h1', 'd1', 'w1')),
    open_price DECIMAL(12,5) NOT NULL,
    high_price DECIMAL(12,5) NOT NULL,
    low_price DECIMAL(12,5) NOT NULL,
    close_price DECIMAL(12,5) NOT NULL,
    volume BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(instrument_id, timestamp_utc, timeframe, metaapi_account_id)
);

-- Partitioning by instrument and time for performance
CREATE INDEX idx_market_data_instrument_time ON market_data(instrument_id, timestamp_utc DESC);
CREATE INDEX idx_market_data_timeframe ON market_data(timeframe);
CREATE INDEX idx_market_data_account ON market_data(metaapi_account_id);

-- =================================================================================
-- SIGNAL LOGS (Detailed Analysis Data)
-- =================================================================================

CREATE TABLE signal_logs (
    signal_id BIGSERIAL PRIMARY KEY,
    timestamp_utc TIMESTAMP WITH TIME ZONE NOT NULL,
    instrument_id INTEGER NOT NULL REFERENCES instruments(instrument_id),

    -- TA Scores (orthogonal groups)
    score_trend DECIMAL(6,4),
    score_momentum DECIMAL(6,4),
    score_acceleration DECIMAL(6,4),
    score_volatility DECIMAL(6,4),
    score_structure DECIMAL(6,4),

    -- Raw TA Indicators
    trend_daily VARCHAR(10),
    trend_weekly VARCHAR(10),
    trend_monthly VARCHAR(10),
    rsi DECIMAL(6,2),
    macd_hist DECIMAL(10,5),
    macd_cross VARCHAR(20),
    bb_pct DECIMAL(6,4),
    bb_squeeze BOOLEAN,
    atr DECIMAL(10,5),
    atr_pct DECIMAL(8,6),
    volume_ratio DECIMAL(8,3),
    nearest_support DECIMAL(12,5),
    nearest_resistance DECIMAL(12,5),
    patterns TEXT[],
    vol_regime VARCHAR(10),

    -- Composite Analysis
    composite_score DECIMAL(6,4) NOT NULL,
    confidence DECIMAL(6,4) NOT NULL,
    direction INTEGER NOT NULL CHECK (direction IN (-1, 0, 1)),

    -- Real-time Delta Analysis
    rt_momentum DECIMAL(10,6),
    delta_aligned BOOLEAN,
    alignment_score DECIMAL(6,4),

    -- Signal Decision
    signal_emitted BOOLEAN NOT NULL DEFAULT FALSE,
    rejection_gate VARCHAR(50),

    -- Trade Parameters (if signal generated)
    entry_price DECIMAL(12,5),
    stop_loss DECIMAL(12,5),
    tp1 DECIMAL(12,5),
    tp2 DECIMAL(12,5),
    rr_ratio DECIMAL(6,2),

    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_signal_logs_timestamp ON signal_logs(timestamp_utc DESC);
CREATE INDEX idx_signal_logs_instrument ON signal_logs(instrument_id);
CREATE INDEX idx_signal_logs_composite_score ON signal_logs(composite_score);
CREATE INDEX idx_signal_logs_signal_emitted ON signal_logs(signal_emitted);

-- =================================================================================
-- TRADE LEDGER (Position Management)
-- =================================================================================

CREATE TABLE trade_ledger (
    trade_id BIGSERIAL PRIMARY KEY,
    timestamp_utc TIMESTAMP WITH TIME ZONE NOT NULL,
    event_type VARCHAR(20) NOT NULL CHECK (event_type IN ('ENTRY_FILL', 'EXIT_FILL', 'POSITION_CLOSED', 'MODIFY')),
    mode VARCHAR(10) NOT NULL CHECK (mode IN ('paper', 'live')),
    instrument_id INTEGER NOT NULL REFERENCES instruments(instrument_id),

    -- Order References
    parent_order_id BIGINT,
    order_id BIGINT,

    -- Position Data
    direction VARCHAR(5) NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    qty_delta DECIMAL(12,4) NOT NULL,
    qty_open DECIMAL(12,4) NOT NULL,
    fill_price DECIMAL(12,5),

    -- P&L
    pnl_delta_usd DECIMAL(12,2),
    realized_pnl_usd DECIMAL(12,2),

    -- Status
    status VARCHAR(50),
    note TEXT,

    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_trade_ledger_timestamp ON trade_ledger(timestamp_utc DESC);
CREATE INDEX idx_trade_ledger_instrument ON trade_ledger(instrument_id);
CREATE INDEX idx_trade_ledger_mode ON trade_ledger(mode);
CREATE INDEX idx_trade_ledger_order_id ON trade_ledger(order_id);

-- =================================================================================
-- BUFFER CALIBRATION (Adaptive Delay System)
-- =================================================================================

CREATE TABLE buffer_calibration (
    calibration_id SERIAL PRIMARY KEY,
    instrument_id INTEGER NOT NULL REFERENCES instruments(instrument_id),
    run_timestamp TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    -- Calibration Parameters
    min_delay_min INTEGER NOT NULL,
    max_delay_min INTEGER NOT NULL,
    step_min INTEGER NOT NULL,
    calibration_window_days INTEGER NOT NULL,

    -- Results
    optimal_delay_min INTEGER NOT NULL,
    best_sharpe DECIMAL(8,4) NOT NULL,
    p_value DECIMAL(6,4) NOT NULL,
    is_significant BOOLEAN NOT NULL,

    -- Diagnostics
    n_bars INTEGER NOT NULL,
    n_windows INTEGER NOT NULL,
    candidates INTEGER[] NOT NULL,
    delay_mean_val_sharpe DECIMAL(8,4)[] NOT NULL,
    window_winners INTEGER[] NOT NULL,

    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_buffer_calibration_instrument ON buffer_calibration(instrument_id);
CREATE INDEX idx_buffer_calibration_run_timestamp ON buffer_calibration(run_timestamp DESC);

-- =================================================================================
-- ECONOMIC CALENDAR (External Events)
-- =================================================================================

CREATE TABLE economic_calendar (
    event_id SERIAL PRIMARY KEY,
    timestamp_utc TIMESTAMP WITH TIME ZONE NOT NULL,
    currency VARCHAR(3),
    event_name VARCHAR(200) NOT NULL,
    importance VARCHAR(10) CHECK (importance IN ('low', 'medium', 'high')),
    forecast DECIMAL(10,4),
    previous DECIMAL(10,4),
    actual DECIMAL(10,4),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_economic_calendar_timestamp ON economic_calendar(timestamp_utc);
CREATE INDEX idx_economic_calendar_currency ON economic_calendar(currency);

-- =================================================================================
-- BOT CONFIGURATION HISTORY (Audit Trail)
-- =================================================================================

CREATE TABLE bot_config_history (
    config_id SERIAL PRIMARY KEY,
    timestamp_utc TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    mode VARCHAR(10) NOT NULL,
    instruments VARCHAR(10)[] NOT NULL,
    min_confidence DECIMAL(4,2),
    min_rr_ratio DECIMAL(4,2),
    max_risk_per_trade_pct DECIMAL(5,4),
    use_kelly_sizing BOOLEAN,
    signal_logging BOOLEAN,
    config_hash VARCHAR(64), -- For change detection
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- =================================================================================
-- VIEWS (For Easy Querying)
-- =================================================================================

-- Latest market data view
CREATE VIEW latest_market_data AS
SELECT DISTINCT ON (instrument_id, timeframe)
    md.*,
    i.symbol
FROM market_data md
JOIN instruments i ON md.instrument_id = i.instrument_id
ORDER BY instrument_id, timeframe, timestamp_utc DESC;

-- Signal performance view
CREATE VIEW signal_performance AS
SELECT
    sl.*,
    i.symbol,
    CASE
        WHEN sl.signal_emitted THEN 'SIGNAL'
        ELSE COALESCE(sl.rejection_gate, 'UNKNOWN')
    END as outcome
FROM signal_logs sl
JOIN instruments i ON sl.instrument_id = i.instrument_id
ORDER BY sl.timestamp_utc DESC;

-- Trade summary view
CREATE VIEW trade_summary AS
SELECT
    tl.*,
    i.symbol,
    i.display_name,
    i.category
FROM trade_ledger tl
JOIN instruments i ON tl.instrument_id = i.instrument_id
ORDER BY tl.timestamp_utc DESC;

-- =================================================================================
-- FUNCTIONS (Utility)
-- =================================================================================

-- Function to get latest buffer delay for an instrument
CREATE OR REPLACE FUNCTION get_optimal_buffer_delay(p_instrument_id INTEGER)
RETURNS INTEGER AS $$
DECLARE
    delay INTEGER;
BEGIN
    SELECT bc.optimal_delay_min INTO delay
    FROM buffer_calibration bc
    WHERE bc.instrument_id = p_instrument_id
      AND bc.is_significant = true
    ORDER BY bc.run_timestamp DESC
    LIMIT 1;

    RETURN COALESCE(delay, 12); -- Default fallback
END;
$$ LANGUAGE plpgsql;

-- Function to clean old market data (keep last N days)
CREATE OR REPLACE FUNCTION cleanup_old_market_data(days_to_keep INTEGER DEFAULT 365)
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM market_data
    WHERE timestamp_utc < NOW() - INTERVAL '1 day' * days_to_keep;

    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

-- =================================================================================
-- TRIGGERS (Auto-maintenance)
-- =================================================================================

-- Updated timestamp trigger
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_instruments_updated_at
    BEFORE UPDATE ON instruments
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
