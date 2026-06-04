# How to Run Database Queries - Complete Guide

## Method 1: Using the Simple Query Script (Easiest)

```bash
# Run basic queries to test everything
python simple_db_queries.py

# Run custom queries
python simple_db_queries.py custom
```

**What it does:**
- Tests database connection
- Shows database statistics
- Lists all instruments
- Shows recent signals
- Displays trade performance
- Shows buffer calibration results

## Method 2: Using psql Command Line

```bash
# Connect to database (adjust path if needed)
"C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -d postgres

# Or if psql is in PATH:
psql -U postgres -d postgres
```

**Common psql commands:**
```sql
-- List tables
\dt

-- Describe table structure
\d signal_logs
\d market_data

-- Run queries
SELECT COUNT(*) FROM signal_logs;

-- Exit psql
\q
```

## Method 3: Using Python Scripts Directly

Create a custom query script:

```python
# custom_query.py
import asyncio
from database_manager import init_database, close_database, db_manager

async def my_query():
    await init_database()

    # Your query here
    async with db_manager.connection() as conn:
        results = await conn.fetch("""
            SELECT i.symbol, COUNT(*) as signal_count
            FROM signal_logs sl
            JOIN instruments i ON sl.instrument_id = i.instrument_id
            GROUP BY i.symbol
            ORDER BY signal_count DESC
        """)

        for row in results:
            print(f"{row['symbol']}: {row['signal_count']} signals")

    await close_database()

asyncio.run(my_query())
```

Run it:
```bash
python custom_query.py
```

## Method 4: Using Database GUIs

### Option A: DBeaver (Recommended)
1. Download DBeaver: https://dbeaver.io/
2. Create new PostgreSQL connection
3. Host: localhost, Port: 5432, Database: postgres
4. Run queries in the SQL editor

### Option B: pgAdmin
1. pgAdmin comes with PostgreSQL
2. Open pgAdmin
3. Add server connection
4. Browse tables and run queries

## Method 5: Using Jupyter Notebook (For Analysis)

```python
# Install Jupyter
pip install jupyter

# Create notebook
jupyter notebook

# In notebook cell:
import pandas as pd
import asyncpg

async def run_query():
    conn = await asyncpg.connect(
        user='postgres',
        database='postgres',
        host='localhost'
    )

    # Run query
    df = pd.read_sql("""
        SELECT * FROM signal_logs
        WHERE timestamp_utc >= NOW() - INTERVAL '1 day'
    """, conn)

    print(df.head())
    await conn.close()

await run_query()
```

## Method 6: Using pandas from Command Line

```python
# pandas_query.py
import pandas as pd
import asyncpg
import asyncio

async def pandas_query():
    conn = await asyncpg.connect(database='postgres')

    # Load data into pandas
    df = pd.read_sql("""
        SELECT sl.timestamp_utc, i.symbol, sl.composite_score,
               sl.confidence, sl.signal_emitted
        FROM signal_logs sl
        JOIN instruments i ON sl.instrument_id = i.instrument_id
        ORDER BY sl.timestamp_utc DESC
        LIMIT 1000
    """, conn)

    # Analysis
    print("Signal Statistics:")
    print(df.describe())

    print("\nSignals by Symbol:")
    print(df.groupby('symbol')['composite_score'].mean())

    await conn.close()

asyncio.run(pandas_query())
```

## Quick Reference - Common Query Patterns

### 1. Check Database Status
```sql
SELECT COUNT(*) FROM signal_logs;
SELECT COUNT(*) FROM trade_ledger;
SELECT COUNT(*) FROM market_data;
```

### 2. Recent Signals
```sql
SELECT sl.timestamp_utc, i.symbol, sl.composite_score,
       sl.signal_emitted, sl.rejection_gate
FROM signal_logs sl
JOIN instruments i ON sl.instrument_id = i.instrument_id
ORDER BY sl.timestamp_utc DESC
LIMIT 10;
```

### 3. Trade Performance
```sql
SELECT i.symbol, COUNT(*) as trades,
       AVG(tl.realized_pnl_usd) as avg_pnl,
       SUM(tl.realized_pnl_usd) as total_pnl
FROM trade_ledger tl
JOIN instruments i ON tl.instrument_id = i.instrument_id
WHERE tl.event_type = 'POSITION_CLOSED'
GROUP BY i.symbol
ORDER BY total_pnl DESC;
```

### 4. Export to CSV
```sql
\COPY (
    SELECT * FROM signal_logs
    WHERE timestamp_utc >= NOW() - INTERVAL '7 days'
) TO 'weekly_signals.csv' WITH CSV HEADER;
```

## Troubleshooting

### Connection Issues
1. **Password authentication failed**: Update `database_manager.py` with correct password
2. **Database doesn't exist**: Run `setup_database.py` or create manually
3. **PostgreSQL not running**: Start PostgreSQL service

### Query Issues
1. **Table doesn't exist**: Run migration script `migrate_to_postgres.py`
2. **No data**: Check if migration completed successfully
3. **Permission denied**: Check PostgreSQL user permissions

## Database Configuration

Edit `database_manager.py` to update connection settings:

```python
DB_CONFIG = {
    'host': 'localhost',
    'port': 5432,
    'user': 'postgres',
    'password': 'your_password_here',  # Update this
    'database': 'postgres'
}
```

## Start Here:
1. **First**: Run `python simple_db_queries.py` to test connection
2. **Then**: Use `psql` for quick queries
3. **Advanced**: Use DBeaver or Jupyter for complex analysis

Happy querying! 🚀