# Citadel Quant Bot - PostgreSQL Migration Guide

This guide covers the migration from CSV/JSON file storage to PostgreSQL database for improved performance, reliability, and analytics capabilities.

## 🚀 Quick Start

### 1. Database Setup
```bash
# Create database (run as postgres user)
# Use default postgres database - no need to create

# Or create with specific user:
# Use default postgres database
```

### 2. Install Dependencies
```bash
pip install asyncpg
```

### 3. Run Migration
```bash
# Migrate existing data from CSV/JSON files
python migrate_to_postgres.py
```

> If the database already exists from an older bot schema, the migration script will now detect and add the `metaapi_account_id` field to `market_data` and ensure the new account-scoped index.

### 4. Test Integration
```bash
# Test database connectivity and operations
python test_database_integration.py
```

### 5. Start Bot
```bash
# Bot will automatically use database if available
python main.py
```

## 📋 Database Schema

The database uses a fully normalized schema with the following tables:

### Core Tables
- **`instruments`** - Master catalog of all tradable instruments
- **`market_data`** - Time-series OHLCV data (partitioned by instrument/timeframe)
- **`signal_logs`** - Detailed signal analysis and rejection tracking
- **`trade_ledger`** - Complete trade execution history
- **`buffer_calibration`** - Adaptive buffer delay calibration results

### Supporting Tables
- **`economic_calendar`** - External economic events
- **`bot_config_history`** - Configuration change audit trail

### Views
- **`latest_market_data`** - Most recent bars per instrument
- **`signal_performance`** - Signal analysis with outcomes
- **`trade_summary`** - Trade data with instrument metadata

## 🔧 Configuration

Update database connection settings in your code or environment:

```python
DB_CONFIG = {
    'host': 'localhost',
    'port': 5432,
    'user': 'postgres',
    'password': 'your_password',
    'database': 'postgres'
}
```

## 📊 Data Migration

### What Gets Migrated
- ✅ All market data (1m, 5m, 1h, 1d, 1w timeframes)
- ✅ Signal logs with full TA indicator values
- ✅ Trade ledger with execution details
- ✅ Buffer calibration diagnostics
- ✅ Instrument catalog

### Migration Process
1. **Backup existing files** (automatic - originals preserved)
2. **Create database schema** (runs SQL from `database_schema.sql`)
3. **Populate instruments** (from `instrument_catalog.py`)
4. **Migrate market data** (all CSV files in `data/market_data/`)
5. **Migrate signal logs** (from `data/signal_log.csv`)
6. **Migrate trade ledger** (from `data/trade_ledger.csv`)
7. **Migrate buffer calibrations** (from JSON files)

### Zero-Downtime Operation
- **CSV files maintained** as fallback during migration
- **Database writes** happen in background (non-blocking)
- **Graceful degradation** if database unavailable
- **Automatic recovery** on next startup

## 🔍 Database Operations

### Market Data
```python
# Get latest bars
data = await db_manager.get_market_data('NDAQ', 'm1', limit=400)

# Insert new bar
await db_manager.insert_market_data(
    instrument_id=1,
    timeframe='m1',
    timestamp_utc=datetime.now(timezone.utc),
    open_price=88.50,
    high_price=88.75,
    low_price=88.25,
    close_price=88.60,
    volume=1500
)
```

### Signal Logging
```python
# Log signal analysis
await db_manager.insert_signal_log({
    'timestamp_utc': datetime.now(timezone.utc),
    'instrument_id': instrument_id,
    'composite_score': 0.75,
    'confidence': 0.8,
    'direction': 1,
    'signal_emitted': True,
    # ... other fields
})
```

### Trade Ledger
```python
# Record trade execution
await db_manager.insert_trade_ledger_entry({
    'timestamp_utc': datetime.now(timezone.utc),
    'event_type': 'ENTRY_FILL',
    'instrument_id': instrument_id,
    'direction': 'LONG',
    'qty_delta': 1000,
    'fill_price': 88.50,
    # ... other fields
})
```

## 📈 Performance Benefits

### Before (CSV/JSON)
- File I/O bottlenecks with large datasets
- Limited querying capabilities
- No concurrent access control
- Manual data aggregation
- Difficult analytics

### After (PostgreSQL)
- **10-100x faster** data retrieval with proper indexing
- **SQL analytics** for complex queries
- **Concurrent access** via connection pooling
- **Real-time aggregation** with views
- **Advanced analytics** ready for external tools

## 🔧 Maintenance

### Database Cleanup
```sql
-- Remove old market data (keep last 365 days)
SELECT cleanup_old_market_data(365);
```

### Monitoring
```sql
-- Check table sizes
SELECT schemaname, tablename, pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) as size
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;
```

### Backup
```bash
# Full database backup
pg_dump postgres > postgres_backup.sql

# Restore
psql postgres < postgres_backup.sql
```

## 🐛 Troubleshooting

### Database Connection Issues
```bash
# Check PostgreSQL service
sudo systemctl status postgresql

# Test connection
psql -U postgres -d postgres -c "SELECT 1;"
```

### Migration Failures
- Check database permissions
- Verify PostgreSQL version (14+ recommended)
- Ensure sufficient disk space
- Check log files for specific errors

### Performance Issues
- Monitor connection pool usage
- Check query execution plans
- Consider adding indexes for frequent queries
- Evaluate partitioning for large tables

## 🔄 Rollback Plan

If database issues occur:
1. **Database unavailable**: Bot continues with CSV files
2. **Data corruption**: Restore from CSV backups
3. **Performance issues**: Switch back to CSV-only mode
4. **Complete rollback**: Drop database, bot runs on CSV files only

## 📚 Advanced Usage

### Custom Queries
```python
# Complex signal analysis
async with db_manager.connection() as conn:
    results = await conn.fetch("""
        SELECT
            i.symbol,
            AVG(sl.composite_score) as avg_score,
            COUNT(*) as signal_count,
            SUM(CASE WHEN sl.signal_emitted THEN 1 ELSE 0 END) as emitted_count
        FROM signal_logs sl
        JOIN instruments i ON sl.instrument_id = i.instrument_id
        WHERE sl.timestamp_utc >= NOW() - INTERVAL '7 days'
        GROUP BY i.symbol
        ORDER BY avg_score DESC
    """)
```

### External Analytics
Connect Tableau, Power BI, or Python analytics tools directly to the PostgreSQL database for advanced reporting and visualization.

## 📞 Support

For issues with the database migration:
1. Check the test script: `python test_database_integration.py`
2. Review bot logs for database-related errors
3. Verify PostgreSQL configuration and permissions
4. Check database connectivity from Python

---

**Migration completed successfully!** 🎉

Your Citadel Quant Bot now has enterprise-grade data persistence with PostgreSQL while maintaining backward compatibility with CSV files.