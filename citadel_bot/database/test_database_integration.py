#!/usr/bin/env python3
"""
Database Integration Test Script
Tests the PostgreSQL integration for Citadel Quant Bot
"""

import asyncio
import sys
from datetime import datetime, timezone
from database_manager import init_database, close_database, db_manager

async def test_database_integration():
    """Test all database operations"""
    print("Testing Citadel Quant Bot Database Integration")
    print("=" * 60)

    try:
        # Initialize database
        await init_database()
        print("Database initialized successfully")

        # Test health check
        healthy = await db_manager.health_check()
        print(f"Health check: {'PASS' if healthy else 'FAIL'}")

        # Test instrument operations
        instruments = await db_manager.get_all_instruments()
        print(f"Loaded {len(instruments)} instruments from catalog")

        # Test market data operations (using NDAQ as example)
        ndaq_data = await db_manager.get_market_data('NDAQ', limit=5)
        if ndaq_data:
            print(f"Retrieved {len(ndaq_data)} market data bars for NDAQ")
        else:
            print("No market data found for NDAQ (expected if migration not run)")

        # Test signal logging
        test_signal = {
            'timestamp_utc': datetime.now(timezone.utc),
            'instrument_id': await db_manager.get_instrument_id('NDAQ'),
            'score_trend': -0.3,
            'score_momentum': -0.7,
            'score_acceleration': -0.7,
            'score_volatility': -0.9,
            'score_structure': -0.9,
            'trend_daily': 'NEUTRAL',
            'trend_weekly': 'NEUTRAL',
            'trend_monthly': 'NEUTRAL',
            'rsi': 35.28,
            'macd_hist': -0.022,
            'macd_cross': 'NONE',
            'bb_pct': 0.059,
            'bb_squeeze': False,
            'atr': 0.065,
            'atr_pct': 0.0007,
            'volume_ratio': 3.116,
            'nearest_support': 88.495,
            'nearest_resistance': 88.692,
            'patterns': ['LOWER_HIGHS_LOWS'],
            'composite_score': 0.153,
            'confidence': 0.693,
            'direction': -1,
            'rt_momentum': -3.896,
            'delta_aligned': True,
            'alignment_score': 1.0,
            'signal_emitted': True,
            'rejection_gate': '',
            'entry_price': 88.61,
            'stop_loss': 88.727,
            'tp1': 88.435,
            'tp2': 88.259,
            'rr_ratio': 3.0,
        }

        if test_signal['instrument_id']:
            await db_manager.insert_signal_log(test_signal)
            print("Test signal logged successfully")
        else:
            print("Could not log test signal - instrument not found")

        # Test trade ledger
        test_trade = {
            'timestamp_utc': datetime.now(timezone.utc),
            'event_type': 'ENTRY_FILL',
            'mode': 'paper',
            'instrument_id': await db_manager.get_instrument_id('NDAQ'),
            'parent_order_id': 12345,
            'order_id': 12346,
            'direction': 'LONG',
            'qty_delta': 1124.0,
            'qty_open': 1124.0,
            'fill_price': 88.28,
            'pnl_delta_usd': 0.0,
            'realized_pnl_usd': 0.0,
            'status': 'retcode=10009',
            'note': 'Test trade entry'
        }

        if test_trade['instrument_id']:
            await db_manager.insert_trade_ledger_entry(test_trade)
            print("Test trade logged successfully")
        else:
            print("Could not log test trade - instrument not found")

        # Test buffer calibration
        test_calibration = {
            'instrument_id': await db_manager.get_instrument_id('NDAQ'),
            'run_timestamp': datetime.now(timezone.utc),
            'min_delay_min': 4,
            'max_delay_min': 40,
            'step_min': 2,
            'calibration_window_days': 90,
            'optimal_delay_min': 12,
            'best_sharpe': 0.45,
            'p_value': 0.02,
            'is_significant': True,
            'n_bars': 5000,
            'n_windows': 15,
            'candidates': [4, 6, 8, 10, 12, 14, 16, 18, 20],
            'delay_mean_val_sharpe': [0.12, 0.18, 0.25, 0.32, 0.45, 0.38, 0.29, 0.22, 0.15],
            'window_winners': [12, 10, 14, 12, 12, 12, 10, 12, 14, 12, 12, 12, 10, 12, 12]
        }

        if test_calibration['instrument_id']:
            await db_manager.save_buffer_calibration(test_calibration)
            print("Test buffer calibration saved successfully")
        else:
            print("Could not save test calibration - instrument not found")

        # Test buffer delay retrieval
        delay = await db_manager.get_optimal_buffer_delay('NDAQ')
        print(f"Retrieved optimal buffer delay for NDAQ: {delay} minutes")

        # Get database statistics
        stats = await db_manager.get_stats()
        print("Database Statistics:")
        for key, value in stats.items():
            print(f"   {key}: {value}")

        print("=" * 60)
        print("All database integration tests completed successfully!")
        print("\nNext steps:")
        print("1. Run migrate_to_postgres.py to migrate existing data")
        print("2. Start the bot to test live database integration")
        print("3. Monitor logs for any database-related issues")

    except Exception as e:
        print(f"Database integration test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        await close_database()

    return True

if __name__ == "__main__":
    success = asyncio.run(test_database_integration())
    sys.exit(0 if success else 1)