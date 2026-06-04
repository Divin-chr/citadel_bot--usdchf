#!/usr/bin/env python3
"""
Database Analytics for Citadel Quant Bot (No Matplotlib Version)
Performance analysis and queries without visualization dependencies
"""

import asyncio
from datetime import datetime, timedelta, timezone
from database_manager import init_database, close_database, db_manager

class DatabaseAnalytics:
    """Analytics queries without matplotlib dependencies"""

    async def get_signal_performance_analysis(self, days: int = 30):
        """Analyze signal performance over time period"""
        async with db_manager.connection() as conn:
            results = await conn.fetch("""
                SELECT
                    DATE_TRUNC('day', sl.timestamp_utc) as date,
                    i.symbol,
                    COUNT(*) as total_signals,
                    SUM(CASE WHEN sl.signal_emitted THEN 1 ELSE 0 END) as emitted_signals,
                    ROUND(AVG(sl.composite_score)::numeric, 4) as avg_composite_score,
                    ROUND(AVG(sl.confidence)::numeric, 4) as avg_confidence,
                    COUNT(CASE WHEN sl.rejection_gate != '' THEN 1 END) as rejected_signals
                FROM signal_logs sl
                JOIN instruments i ON sl.instrument_id = i.instrument_id
                WHERE sl.timestamp_utc >= NOW() - INTERVAL '%s days'
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
                    ROUND(AVG(tl.realized_pnl_usd)::numeric, 2) as avg_pnl,
                    ROUND(SUM(tl.realized_pnl_usd)::numeric, 2) as total_pnl,
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
                    ROUND(bc.best_sharpe::numeric, 4) as best_sharpe,
                    ROUND(bc.p_value::numeric, 4) as p_value,
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
                        ROUND(AVG(sl.composite_score)::numeric, 4) as avg_score,
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
                print("⚠️  Simple volatility regime analysis skipped: vol_regime column not present or unsupported schema.")
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
                    ROUND(AVG(sl.composite_score)::numeric, 4) as avg_score_at_rejection
                FROM signal_logs sl
                WHERE sl.timestamp_utc >= NOW() - INTERVAL '%s days'
                  AND sl.rejection_gate != ''
                GROUP BY sl.rejection_gate
                ORDER BY count DESC
            """, days)

            return [dict(row) for row in results]

async def print_table(headers, rows, title=""):
    """Print data in a simple table format"""
    if title:
        print(f"\n{title}")
        print("=" * len(title))

    if not rows:
        print("No data found.")
        return

    # Calculate column widths
    col_widths = {}
    for header in headers:
        col_widths[header] = len(header)

    for row in rows:
        for header in headers:
            if header in row:
                col_widths[header] = max(col_widths[header], len(str(row[header])))

    # Print header
    header_line = " | ".join(h.ljust(col_widths[h]) for h in headers)
    print(header_line)
    print("-" * len(header_line))

    # Print rows
    for row in rows:
        row_line = " | ".join(str(row.get(h, "")).ljust(col_widths[h]) for h in headers)
        print(row_line)

async def run_analytics():
    """Run comprehensive analytics"""
    print("Citadel Quant Bot - Database Analytics")
    print("=" * 50)

    try:
        await init_database()
        print("Database connected successfully!")

        analytics = DatabaseAnalytics()

        # 1. Signal Performance Analysis
        signal_perf = await analytics.get_signal_performance_analysis(30)
        if signal_perf:
            print_table(
                ["date", "symbol", "total_signals", "emitted_signals", "avg_composite_score"],
                signal_perf[:10],  # Show first 10
                "1. Signal Performance (Last 30 days)"
            )

        # 2. Trade Performance by Instrument
        trade_perf = await analytics.get_trade_performance_by_instrument()
        if trade_perf:
            print_table(
                ["symbol", "category", "total_trades", "win_rate_pct", "total_pnl"],
                trade_perf,
                "2. Trade Performance by Instrument"
            )

        # 3. Buffer Delay Effectiveness
        buffer_eff = await analytics.get_buffer_delay_effectiveness()
        if buffer_eff:
            print_table(
                ["symbol", "delay_minutes", "best_sharpe", "p_value", "is_significant"],
                buffer_eff,
                "3. Buffer Delay Effectiveness"
            )

        # 4. Volatility Regime Analysis
        vol_analysis = await analytics.get_volatility_regime_analysis(90)
        if vol_analysis:
            print_table(
                ["volatility_regime", "signal_count", "avg_score", "emission_rate_pct"],
                vol_analysis,
                "4. Performance by Volatility Regime"
            )

        # 5. Rejection Gate Analysis
        rejection_analysis = await analytics.get_rejection_gate_analysis(30)
        if rejection_analysis:
            print_table(
                ["rejection_gate", "count", "percentage", "avg_score_at_rejection"],
                rejection_analysis,
                "5. Signal Rejection Analysis"
            )

        print("\nAnalytics complete!")

    except Exception as e:
        print(f"Analytics failed: {e}")
        import traceback
        traceback.print_exc()

    finally:
        await close_database()

if __name__ == "__main__":
    asyncio.run(run_analytics())