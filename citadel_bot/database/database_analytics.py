#!/usr/bin/env python3
"""
Custom Database Analytics for Citadel Quant Bot
Example queries and analysis functions
"""

import asyncio
import pandas as pd
from datetime import datetime, timedelta, timezone
from database_manager import init_database, close_database, db_manager
import matplotlib.pyplot as plt

class DatabaseAnalytics:
    """Custom analytics queries for the Citadel Quant Bot database"""

    async def get_signal_performance_analysis(self, days: int = 30):
        """Analyze signal performance over time period"""
        async with db_manager.connection() as conn:
            results = await conn.fetch("""
                SELECT
                    DATE_TRUNC('day', sl.timestamp_utc) as date,
                    i.symbol,
                    COUNT(*) as total_signals,
                    SUM(CASE WHEN sl.signal_emitted THEN 1 ELSE 0 END) as emitted_signals,
                    AVG(sl.composite_score) as avg_composite_score,
                    AVG(sl.confidence) as avg_confidence,
                    COUNT(CASE WHEN sl.rejection_gate != '' THEN 1 END) as rejected_signals
                FROM signal_logs sl
                JOIN instruments i ON sl.instrument_id = i.instrument_id
                WHERE sl.timestamp_utc >= NOW() - $1 * INTERVAL '1 day'
                GROUP BY DATE_TRUNC('day', sl.timestamp_utc), i.symbol
                ORDER BY date DESC, symbol
            """, days)

            return [dict(row) for row in results]

    async def get_trade_performance_by_instrument(self):
        """Get P&L performance by instrument"""
        async with db_manager.connection() as conn:
            results = await conn.fetch("""
                SELECT
                    i.symbol,
                    i.category,
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN tl.realized_pnl_usd > 0 THEN 1 ELSE 0 END) as winning_trades,
                    SUM(CASE WHEN tl.realized_pnl_usd < 0 THEN 1 ELSE 0 END) as losing_trades,
                    ROUND(AVG(tl.realized_pnl_usd), 2) as avg_pnl,
                    ROUND(SUM(tl.realized_pnl_usd), 2) as total_pnl,
                    ROUND(
                        SUM(CASE WHEN tl.realized_pnl_usd > 0 THEN 1 ELSE 0 END)::numeric /
                        NULLIF(COUNT(*), 0) * 100, 1
                    ) as win_rate_pct
                FROM trade_ledger tl
                JOIN instruments i ON tl.instrument_id = i.instrument_id
                WHERE tl.event_type = 'POSITION_CLOSED'
                GROUP BY i.symbol, i.category
                ORDER BY total_pnl DESC
            """)

            return [dict(row) for row in results]

    async def get_buffer_delay_effectiveness(self):
        """Analyze how buffer delays correlate with performance"""
        async with db_manager.connection() as conn:
            results = await conn.fetch("""
                SELECT
                    bc.optimal_delay_min as delay_minutes,
                    bc.best_sharpe,
                    bc.p_value,
                    bc.is_significant,
                    i.symbol,
                    bc.n_windows,
                    bc.run_timestamp
                FROM buffer_calibration bc
                JOIN instruments i ON bc.instrument_id = i.instrument_id
                WHERE bc.is_significant = true
                ORDER BY bc.best_sharpe DESC
            """)

            return [dict(row) for row in results]

    async def get_volatility_regime_analysis(self, days: int = 90):
        """Analyze performance across volatility regimes"""
        async with db_manager.connection() as conn:
            try:
                results = await conn.fetch("""
                    SELECT
                        CASE
                            WHEN sl.vol_regime = 'EXTREME' THEN 'EXTREME'
                            WHEN sl.vol_regime = 'HIGH' THEN 'HIGH'
                            WHEN sl.vol_regime = 'LOW' THEN 'LOW'
                            ELSE 'NORMAL'
                        END as volatility_regime,
                        COUNT(*) as signal_count,
                        AVG(sl.composite_score) as avg_score,
                        SUM(CASE WHEN sl.signal_emitted THEN 1 ELSE 0 END) as emitted_signals,
                        ROUND(
                            SUM(CASE WHEN sl.signal_emitted THEN 1 ELSE 0 END)::numeric /
                            NULLIF(COUNT(*), 0) * 100, 1
                        ) as emission_rate_pct
                    FROM signal_logs sl
                    WHERE sl.timestamp_utc >= NOW() - $1 * INTERVAL '1 day'
                    GROUP BY
                        CASE
                            WHEN sl.vol_regime = 'EXTREME' THEN 'EXTREME'
                            WHEN sl.vol_regime = 'HIGH' THEN 'HIGH'
                            WHEN sl.vol_regime = 'LOW' THEN 'LOW'
                            ELSE 'NORMAL'
                        END
                    ORDER BY avg_score DESC
                """, days)

                return [dict(row) for row in results]
            except Exception as exc:
                print("⚠️  Volatility regime analysis skipped: vol_regime column not present or unsupported schema.")
                print(f"    Reason: {exc}")
                return []

    async def get_rejection_gate_analysis(self, days: int = 30):
        """Analyze why signals are being rejected"""
        async with db_manager.connection() as conn:
            results = await conn.fetch("""
                SELECT
                    sl.rejection_gate,
                    COUNT(*) as count,
                    ROUND(COUNT(*)::numeric / SUM(COUNT(*)) OVER() * 100, 1) as percentage,
                    AVG(sl.composite_score) as avg_score_at_rejection
                FROM signal_logs sl
                WHERE sl.timestamp_utc >= NOW() - $1 * INTERVAL '1 day'
                  AND sl.rejection_gate != ''
                GROUP BY sl.rejection_gate
                ORDER BY count DESC
            """, days)

            return [dict(row) for row in results]

    async def get_correlation_matrix(self, symbols: list = None):
        """Calculate correlation matrix for instruments"""
        if not symbols:
            symbols = ['NDAQ', 'US30', 'US500', 'EURUSD', 'GBPUSD']

        async with db_manager.connection() as conn:
            # Get instrument IDs
            symbol_ids = await conn.fetch("""
                SELECT instrument_id, symbol FROM instruments
                WHERE symbol = ANY($1)
            """, symbols)

            if not symbol_ids:
                return None

            # Get recent returns for correlation calculation
            returns_data = {}
            for row in symbol_ids:
                instrument_id = row['instrument_id']
                symbol = row['symbol']

                # Get daily returns for last 60 days
                data = await conn.fetch("""
                    SELECT
                        DATE_TRUNC('day', timestamp_utc) as date,
                        close_price
                    FROM market_data
                    WHERE instrument_id = $1
                      AND timeframe = 'm1'
                      AND timestamp_utc >= NOW() - INTERVAL '60 days'
                    ORDER BY timestamp_utc
                """, instrument_id)

                if len(data) > 1:
                    df = pd.DataFrame([dict(row) for row in data])
                    df['date'] = pd.to_datetime(df['date'])
                    df = df.groupby('date')['close_price'].last().pct_change().dropna()
                    returns_data[symbol] = df

            if len(returns_data) < 2:
                return None

            # Calculate correlation matrix
            returns_df = pd.DataFrame(returns_data)
            corr_matrix = returns_df.corr()

            return corr_matrix.to_dict()

async def run_analytics():
    """Run comprehensive analytics"""
    print("Citadel Quant Bot - Database Analytics")
    print("=" * 50)

    try:
        await init_database()

        analytics = DatabaseAnalytics()

        # 1. Signal Performance Analysis
        print("\n1. Signal Performance (Last 30 days):")
        signal_perf = await analytics.get_signal_performance_analysis(30)
        for row in signal_perf[:5]:  # Show first 5
            print(f"  {row['date'].date()} {row['symbol']}: {row['total_signals']} signals, "
                  f"{row['emitted_signals']} emitted, avg_score={row['avg_composite_score']:.3f}")

        # 2. Trade Performance by Instrument
        print("\n2. Trade Performance by Instrument:")
        trade_perf = await analytics.get_trade_performance_by_instrument()
        for row in trade_perf:
            print(f"  {row['symbol']} ({row['category']}): "
                  f"{row['total_trades']} trades, {row['win_rate_pct']:.1f}% win rate, "
                  f"total_pnl=${row['total_pnl']:.2f}")

        # 3. Buffer Delay Effectiveness
        print("\n3. Buffer Delay Effectiveness:")
        buffer_eff = await analytics.get_buffer_delay_effectiveness()
        for row in buffer_eff[:3]:
            print(f"  {row['symbol']}: {row['delay_minutes']}min delay, "
                  f"sharpe={row['best_sharpe']:.3f}, significant={row['is_significant']}")

        # 4. Volatility Regime Analysis
        print("\n4. Performance by Volatility Regime:")
        vol_analysis = await analytics.get_volatility_regime_analysis(90)
        for row in vol_analysis:
            print(f"  {row['volatility_regime']}: {row['signal_count']} signals, "
                  f"avg_score={row['avg_score']:.3f}, emission_rate={row['emission_rate_pct']:.1f}%")

        # 5. Rejection Gate Analysis
        print("\n5. Signal Rejection Analysis:")
        rejection_analysis = await analytics.get_rejection_gate_analysis(30)
        for row in rejection_analysis:
            print(f"  {row['rejection_gate']}: {row['count']} rejections "
                  f"({row['percentage']}%), avg_score={row['avg_score_at_rejection']:.3f}")

        print("\n✅ Analytics complete!")

    except Exception as e:
        print(f"❌ Analytics failed: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await close_database()

if __name__ == "__main__":
    asyncio.run(run_analytics())