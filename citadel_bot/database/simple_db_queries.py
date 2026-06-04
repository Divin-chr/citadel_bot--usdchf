#!/usr/bin/env python3
"""
Simple Database Query Examples for Citadel Quant Bot
Run these to test your database connection and queries
"""

import asyncio
import sys
from database_manager import init_database, close_database, db_manager

async def run_simple_queries():
    """Run basic database queries without matplotlib"""
    print("Citadel Quant Bot - Simple Database Queries")
    print("=" * 50)

    try:
        # Initialize database connection
        await init_database()
        print("✅ Database connected successfully!")

        # Query 1: Check database health
        print("\n1. Database Health Check:")
        stats = await db_manager.get_stats()
        for key, value in stats.items():
            print(f"   {key}: {value}")

        # Query 2: List all instruments
        print("\n2. Available Instruments:")
        instruments = await db_manager.get_all_instruments()
        for inst in instruments:
            print(f"   {inst['symbol']}: {inst['display_name']} ({inst['category']})")

        # Query 3: Recent signals
        print("\n3. Recent Signals (last 5):")
        async with db_manager.connection() as conn:
            signals = await conn.fetch("""
                SELECT sl.timestamp_utc, i.symbol, sl.composite_score,
                       sl.confidence, sl.signal_emitted, sl.rejection_gate
                FROM signal_logs sl
                JOIN instruments i ON sl.instrument_id = i.instrument_id
                ORDER BY sl.timestamp_utc DESC
                LIMIT 5
            """)

            for signal in signals:
                emitted = "YES" if signal['signal_emitted'] else "NO"
                print(f"   {signal['timestamp_utc']} {signal['symbol']}: "
                      f"score={signal['composite_score']:.3f}, "
                      f"conf={signal['confidence']:.3f}, "
                      f"emitted={emitted}")

        # Query 4: Trade performance
        print("\n4. Trade Performance Summary:")
        async with db_manager.connection() as conn:
            trades = await conn.fetch("""
                SELECT i.symbol,
                       COUNT(*) as total_trades,
                       ROUND(AVG(tl.realized_pnl_usd), 2) as avg_pnl,
                       ROUND(SUM(tl.realized_pnl_usd), 2) as total_pnl
                FROM trade_ledger tl
                JOIN instruments i ON tl.instrument_id = i.instrument_id
                WHERE tl.event_type = 'POSITION_CLOSED'
                GROUP BY i.symbol
                ORDER BY total_pnl DESC
                LIMIT 5
            """)

            for trade in trades:
                print(f"   {trade['symbol']}: {trade['total_trades']} trades, "
                      f"avg_pnl=${trade['avg_pnl']}, total_pnl=${trade['total_pnl']}")

        # Query 5: Buffer calibration results
        print("\n5. Buffer Calibration Results:")
        async with db_manager.connection() as conn:
            buffers = await conn.fetch("""
                SELECT i.symbol, bc.optimal_delay_min, bc.best_sharpe,
                       bc.p_value, bc.is_significant
                FROM buffer_calibration bc
                JOIN instruments i ON bc.instrument_id = i.instrument_id
                ORDER BY bc.best_sharpe DESC
                LIMIT 3
            """)

            for buffer in buffers:
                sig = "YES" if buffer['is_significant'] else "NO"
                print(f"   {buffer['symbol']}: {buffer['optimal_delay_min']}min delay, "
                      f"sharpe={buffer['best_sharpe']:.3f}, significant={sig}")

        print("\n✅ All queries executed successfully!")

    except Exception as e:
        print(f"❌ Query execution failed: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        await close_database()

    return True

async def run_custom_query():
    """Example of running a custom query"""
    print("\nCustom Query Example:")
    print("-" * 30)

    try:
        await init_database()

        async with db_manager.connection() as conn:
            # Example: Find best performing signals
            results = await conn.fetch("""
                SELECT i.symbol, AVG(sl.composite_score) as avg_score, COUNT(*) as signal_count
                FROM signal_logs sl
                JOIN instruments i ON sl.instrument_id = i.instrument_id
                WHERE sl.signal_emitted = true
                GROUP BY i.symbol
                HAVING COUNT(*) > 10
                ORDER BY avg_score DESC
                LIMIT 5
            """)

            for row in results:
                print(f"   {row['symbol']}: avg_score={row['avg_score']:.3f}, "
                      f"signals={row['signal_count']}")

    finally:
        await close_database()

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "custom":
        asyncio.run(run_custom_query())
    else:
        asyncio.run(run_simple_queries())