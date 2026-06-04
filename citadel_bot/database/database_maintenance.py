#!/usr/bin/env python3
"""
Database Maintenance Queries for Citadel Quant Bot
Cleanup, optimization, and administrative operations
"""

import asyncio
from database_manager import init_database, close_database, db_manager

class DatabaseMaintenance:
    """Database maintenance and administrative operations"""

    async def cleanup_old_data(self, days_to_keep: int = 365):
        """Clean up old market data and logs"""
        async with db_manager.connection() as conn:
            # Clean up old market data
            market_deleted = await conn.fetchval("""
                DELETE FROM market_data
                WHERE timestamp_utc < NOW() - INTERVAL '%s days'
                RETURNING COUNT(*)
            """, days_to_keep)

            # Clean up old signal logs (keep more history for analysis)
            signal_deleted = await conn.fetchval("""
                DELETE FROM signal_logs
                WHERE timestamp_utc < NOW() - INTERVAL '%s days'
                RETURNING COUNT(*)
            """, days_to_keep * 2)

            # Clean up old trade ledger (keep all trade history)
            # trade_deleted = await conn.fetchval("SELECT 0")  # Keep all

            print(f"Cleaned up: {market_deleted} market data rows, {signal_deleted} signal log rows")
            return market_deleted, signal_deleted

    async def optimize_tables(self):
        """Run table optimization (VACUUM ANALYZE)"""
        async with db_manager.connection() as conn:
            tables = ['market_data', 'signal_logs', 'trade_ledger', 'buffer_calibration']

            for table in tables:
                print(f"Optimizing {table}...")
                await conn.execute(f"VACUUM ANALYZE {table}")

            print("✅ Table optimization complete")

    async def get_database_stats(self):
        """Get comprehensive database statistics"""
        async with db_manager.connection() as conn:
            # Table sizes
            table_stats = await conn.fetch("""
                SELECT
                    schemaname,
                    tablename,
                    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) as size,
                    pg_total_relation_size(schemaname||'.'||tablename) as size_bytes
                FROM pg_tables
                WHERE schemaname = 'public'
                ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC
            """)

            # Index usage
            index_stats = await conn.fetch("""
                SELECT
                    schemaname,
                    tablename,
                    indexname,
                    pg_size_pretty(pg_relation_size(indexrelid)) as size
                FROM pg_stat_user_indexes
                WHERE schemaname = 'public'
                ORDER BY pg_relation_size(indexrelid) DESC
            """)

            # Data age analysis
            age_stats = await conn.fetch("""
                SELECT
                    'market_data' as table_name,
                    MIN(timestamp_utc) as oldest_record,
                    MAX(timestamp_utc) as newest_record,
                    COUNT(*) as total_records
                FROM market_data
                UNION ALL
                SELECT
                    'signal_logs' as table_name,
                    MIN(timestamp_utc) as oldest_record,
                    MAX(timestamp_utc) as newest_record,
                    COUNT(*) as total_records
                FROM signal_logs
                UNION ALL
                SELECT
                    'trade_ledger' as table_name,
                    MIN(timestamp_utc) as oldest_record,
                    MAX(timestamp_utc) as newest_record,
                    COUNT(*) as total_records
                FROM trade_ledger
            """)

            return {
                'table_stats': [dict(row) for row in table_stats],
                'index_stats': [dict(row) for row in index_stats],
                'age_stats': [dict(row) for row in age_stats]
            }

    async def create_performance_indexes(self):
        """Create additional indexes for better query performance"""
        async with db_manager.connection() as conn:
            indexes = [
                # Composite indexes for common queries
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_signal_logs_symbol_time ON signal_logs(instrument_id, timestamp_utc DESC)",
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_market_data_symbol_timeframe_time ON market_data(instrument_id, timeframe, timestamp_utc DESC)",
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_trade_ledger_symbol_event_time ON trade_ledger(instrument_id, event_type, timestamp_utc DESC)",

                # Partial indexes for frequent filters
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_signal_logs_emitted ON signal_logs(signal_emitted) WHERE signal_emitted = true",
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_trade_ledger_closed_positions ON trade_ledger(event_type) WHERE event_type = 'POSITION_CLOSED'",

                # JSON/path indexes if using JSON columns (future expansion)
                # "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_signal_logs_patterns ON signal_logs USING GIN (patterns)",
            ]

            for index_sql in indexes:
                try:
                    print(f"Creating index: {index_sql.split('ON')[1].split('(')[0].strip()}")
                    await conn.execute(index_sql)
                except Exception as e:
                    print(f"Index creation failed (might already exist): {e}")

            print("✅ Performance indexes created/verified")

    async def backup_database_schema(self):
        """Generate database schema backup"""
        async with db_manager.connection() as conn:
            # Get all table definitions
            tables = await conn.fetch("""
                SELECT tablename
                FROM pg_tables
                WHERE schemaname = 'public'
                ORDER BY tablename
            """)

            schema_dump = []
            for table in tables:
                table_name = table['tablename']

                # Get table structure
                columns = await conn.fetch("""
                    SELECT column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = $1
                    ORDER BY ordinal_position
                """, table_name)

                # Get indexes
                indexes = await conn.fetch("""
                    SELECT indexname, indexdef
                    FROM pg_indexes
                    WHERE schemaname = 'public' AND tablename = $1
                """, table_name)

                schema_dump.append({
                    'table': table_name,
                    'columns': [dict(col) for col in columns],
                    'indexes': [dict(idx) for idx in indexes]
                })

            return schema_dump

async def run_maintenance():
    """Run database maintenance tasks"""
    print("Citadel Quant Bot - Database Maintenance")
    print("=" * 50)

    try:
        await init_database()

        maintenance = DatabaseMaintenance()

        # 1. Get current stats
        print("\n1. Current Database Statistics:")
        stats = await maintenance.get_database_stats()

        print("Table Sizes:")
        for table in stats['table_stats']:
            print(f"  {table['tablename']}: {table['size']}")

        print("\nData Age Analysis:")
        for age in stats['age_stats']:
            if age['total_records'] > 0:
                print(f"  {age['table_name']}: {age['total_records']} records, "
                      f"from {age['oldest_record'].date() if age['oldest_record'] else 'N/A'} "
                      f"to {age['newest_record'].date() if age['newest_record'] else 'N/A'}")

        # 2. Create performance indexes
        print("\n2. Creating Performance Indexes...")
        await maintenance.create_performance_indexes()

        # 3. Optimize tables
        print("\n3. Optimizing Tables...")
        await maintenance.optimize_tables()

        # 4. Optional: Cleanup old data (commented out by default)
        # print("\n4. Cleaning up old data...")
        # market_deleted, signal_deleted = await maintenance.cleanup_old_data(365)
        # print(f"   Cleaned up {market_deleted} market data rows, {signal_deleted} signal rows")

        print("\n✅ Maintenance complete!")

    except Exception as e:
        print(f"❌ Maintenance failed: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await close_database()

if __name__ == "__main__":
    asyncio.run(run_maintenance())