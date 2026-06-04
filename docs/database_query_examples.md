# Database Query Examples for Citadel Quant Bot

## 1. Using psql (PostgreSQL Command Line)

```bash
# Connect to database
psql -U postgres -d postgres

# Or with password
psql -U postgres -d postgres -W
```

### Common Queries:

```sql
-- Check database size
SELECT pg_size_pretty(pg_database_size('postgres'));

-- List all tables
\dt

-- Get table structure
\d signal_logs
\d market_data
\d trade_ledger

-- Recent signals
SELECT sl.timestamp_utc, i.symbol, sl.composite_score, sl.confidence,
       sl.signal_emitted, sl.rejection_gate
FROM signal_logs sl
JOIN instruments i ON sl.instrument_id = i.instrument_id
ORDER BY sl.timestamp_utc DESC
LIMIT 10;

-- Trade performance summary
SELECT i.symbol,
       COUNT(*) as total_trades,
       ROUND(AVG(tl.realized_pnl_usd), 2) as avg_pnl,
       ROUND(SUM(tl.realized_pnl_usd), 2) as total_pnl,
       ROUND(SUM(CASE WHEN tl.realized_pnl_usd > 0 THEN 1 ELSE 0 END)::numeric / COUNT(*) * 100, 1) as win_rate
FROM trade_ledger tl
JOIN instruments i ON tl.instrument_id = i.instrument_id
WHERE tl.event_type = 'POSITION_CLOSED'
GROUP BY i.symbol
ORDER BY total_pnl DESC;

-- Buffer calibration results
SELECT i.symbol, bc.optimal_delay_min, bc.best_sharpe, bc.p_value, bc.is_significant
FROM buffer_calibration bc
JOIN instruments i ON bc.instrument_id = i.instrument_id
ORDER BY bc.best_sharpe DESC;

-- Export data to CSV
\COPY (
    SELECT * FROM signal_logs
    WHERE timestamp_utc >= NOW() - INTERVAL '7 days'
) TO 'signal_logs_week.csv' WITH CSV HEADER;
```

## 2. Using Python Scripts (database_analytics.py)

```python
# Run the analytics script
python database_analytics.py

# Or import and use in your own scripts
from database_analytics import DatabaseAnalytics
import asyncio

async def custom_query():
    analytics = DatabaseAnalytics()
    results = await analytics.get_signal_performance_analysis(7)
    for row in results:
        print(f"{row['symbol']}: {row['total_signals']} signals")

asyncio.run(custom_query())
```

## 3. Using Database GUIs

### DBeaver / pgAdmin / TablePlus
1. Connect to PostgreSQL database
2. Browse tables and run queries
3. Export results to CSV/Excel
4. Create charts and dashboards

### Example Query in GUI:
```sql
-- Daily performance summary
SELECT
    DATE_TRUNC('day', sl.timestamp_utc) as trade_date,
    i.symbol,
    COUNT(*) as signals_generated,
    SUM(CASE WHEN sl.signal_emitted THEN 1 ELSE 0 END) as signals_executed,
    AVG(sl.composite_score) as avg_score,
    AVG(sl.confidence) as avg_confidence
FROM signal_logs sl
JOIN instruments i ON sl.instrument_id = i.instrument_id
WHERE sl.timestamp_utc >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY DATE_TRUNC('day', sl.timestamp_utc), i.symbol
ORDER BY trade_date DESC, symbol;
```

## 4. Using pandas for Analysis

```python
import pandas as pd
import asyncpg
import asyncio

async def pandas_analysis():
    conn = await asyncpg.connect(
        user='postgres',
        password='your_password',
        database='postgres',
        host='localhost'
    )

    # Load signal data into pandas
    signals_df = pd.read_sql("""
        SELECT sl.*, i.symbol
        FROM signal_logs sl
        JOIN instruments i ON sl.instrument_id = i.instrument_id
        WHERE sl.timestamp_utc >= NOW() - INTERVAL '30 days'
    """, conn)

    # Analyze win rates by rejection gate
    rejection_analysis = signals_df.groupby('rejection_gate').agg({
        'composite_score': 'mean',
        'confidence': 'mean',
        'signal_emitted': 'count'
    }).round(3)

    print(rejection_analysis)

    await conn.close()

asyncio.run(pandas_analysis())
```

## 5. Using Jupyter Notebooks

```python
# In a Jupyter notebook cell
import pandas as pd
import asyncpg
import matplotlib.pyplot as plt
import seaborn as sns

# Connect and load data
conn = await asyncpg.connect(database='postgres')

# Daily performance chart
daily_perf = pd.read_sql("""
    SELECT DATE_TRUNC('day', timestamp_utc) as date,
           AVG(composite_score) as avg_score,
           COUNT(*) as signal_count
    FROM signal_logs
    WHERE timestamp_utc >= CURRENT_DATE - INTERVAL '30 days'
    GROUP BY DATE_TRUNC('day', timestamp_utc)
    ORDER BY date
""", conn)

# Create visualization
plt.figure(figsize=(12, 6))
plt.plot(daily_perf['date'], daily_perf['avg_score'])
plt.title('Daily Average Signal Score')
plt.xlabel('Date')
plt.ylabel('Composite Score')
plt.show()
```

## 6. Scheduled Queries (Cron Jobs)

Create a script for regular reports:

```bash
#!/bin/bash
# daily_report.sh

psql -d citadel_bot -c "
SELECT
    CURRENT_DATE as report_date,
    COUNT(*) as signals_today,
    AVG(composite_score) as avg_score_today,
    SUM(CASE WHEN signal_emitted THEN 1 ELSE 0 END) as executed_today
FROM signal_logs
WHERE DATE(timestamp_utc) = CURRENT_DATE;
" > daily_report_$(date +%Y%m%d).txt
```

Add to crontab:
```bash
# Run daily at 6 AM
0 6 * * * /path/to/daily_report.sh
```

## 7. REST API Queries (Future)

If you add a REST API layer:

```python
from fastapi import FastAPI
from database_manager import db_manager

app = FastAPI()

@app.get("/signals/performance/{days}")
async def get_signal_performance(days: int):
    analytics = DatabaseAnalytics()
    return await analytics.get_signal_performance_analysis(days)

@app.get("/trades/summary")
async def get_trade_summary():
    analytics = DatabaseAnalytics()
    return await analytics.get_trade_performance_by_instrument()
```

## Query Performance Tips

1. **Use EXPLAIN ANALYZE** to check query performance:
```sql
EXPLAIN ANALYZE SELECT * FROM signal_logs WHERE timestamp_utc >= NOW() - INTERVAL '1 day';
```

2. **Create indexes** for frequently queried columns:
```sql
CREATE INDEX idx_signal_logs_timestamp ON signal_logs(timestamp_utc DESC);
CREATE INDEX idx_signal_logs_symbol ON signal_logs(instrument_id);
```

3. **Use pagination** for large result sets:
```sql
SELECT * FROM signal_logs
ORDER BY timestamp_utc DESC
LIMIT 100 OFFSET 0;
```

4. **Archive old data** to separate tables for better performance:
```sql
CREATE TABLE signal_logs_2024 PARTITION OF signal_logs
    FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');
```

This gives you multiple ways to interact with your database depending on your needs!