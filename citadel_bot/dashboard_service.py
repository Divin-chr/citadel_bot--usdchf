"""
Dashboard service - MetaApi terminal-state and PostgreSQL integration layer.
"""

import asyncio
from typing import Dict, List, Optional

import pandas as pd

from citadel_bot.config.config import BotConfig
from citadel_bot.database.database_manager import db_manager, init_database
from citadel_bot.utils.logger import get_logger

log = get_logger("dashboard_service")


class DashboardService:
    """Read-only service layer for dashboard status and analytics."""

    def __init__(self):
        self.connection = None
        self._last_account_info = None
        self.db_connected = False
        from citadel_bot.database.database_manager import DatabaseManager
        self.db = DatabaseManager()

    def attach_connection(self, connection):
        self.connection = connection
        if connection is None:
            self._last_account_info = None

    def _cached_account_info(self, error: str) -> Dict:
        if self._last_account_info:
            cached = dict(self._last_account_info)
            cached["stale"] = True
            cached["stale_reason"] = error
            return cached
        return {"error": error}

    @staticmethod
    def _format_account_info(info) -> Dict:
        return {
            "login": info.get("login"),
            "server": info.get("server"),
            "balance": round(float(info.get("balance") or 0), 2),
            "equity": round(float(info.get("equity") or info.get("balance") or 0), 2),
            "profit": round(float(info.get("profit") or 0), 2),
            "margin": round(float(info.get("margin") or 0), 2),
            "margin_free": round(float(info.get("freeMargin") or info.get("marginFree") or 0), 2),
            "margin_level": round(float(info.get("marginLevel") or 0), 2),
            "currency": info.get("currency") or "",
            "company": info.get("broker") or info.get("company") or "",
            "stale": False,
        }

    async def ensure_database(self) -> bool:
        current_loop = asyncio.get_running_loop()
        if self.db_connected and self.db.pool is not None and getattr(self.db, '_loop', None) is current_loop:
            return True
        try:
            config = BotConfig.from_file("config.yaml")
            if not config.database_url and (not config.database_host or not config.database_name):
                log.debug("Dashboard database config not provided; skipping database access")
                return False

            self.db.configure({
                "database_url": config.database_url,
                "host": config.database_host,
                "port": config.database_port,
                "database": config.database_name,
                "user": config.database_user,
                "password": config.database_password,
            })

            await asyncio.wait_for(self.db.connect(), timeout=5)
            self.db_connected = await asyncio.wait_for(self.db.health_check(), timeout=3)
        except Exception as exc:
            log.debug("Dashboard database unavailable: %s", exc)
            self.db_connected = False
        return self.db_connected

    async def get_account_info(self) -> Dict:
        """Get account information from the attached MetaApi connection."""
        if self.connection is None:
            return {"error": "MetaApi connection not attached"}

        try:
            info = getattr(self.connection.terminal_state, "account_information", None)
            if not info:
                return self._cached_account_info("Account information not synchronized")
            self._last_account_info = self._format_account_info(info)
            return dict(self._last_account_info)
        except Exception as e:
            log.error("Error getting account info: %s", e)
            return self._cached_account_info(str(e))

    async def get_open_positions(self) -> List[Dict]:
        """Get open positions from the attached MetaApi terminal state."""
        if self.connection is None:
            return []

        try:
            positions = getattr(self.connection.terminal_state, "positions", []) or []
            result = []
            for pos in positions:
                result.append({
                    "ticket": pos.get("id") or pos.get("positionId"),
                    "symbol": pos.get("symbol"),
                    "type": "BUY" if pos.get("type") == "POSITION_TYPE_BUY" else "SELL",
                    "volume": pos.get("volume"),
                    "open_price": round(float(pos.get("openPrice") or 0), 5),
                    "current_price": round(float(pos.get("currentPrice") or 0), 5),
                    "profit": round(float(pos.get("profit") or 0), 2),
                    "open_time": pos.get("time"),
                })
            return result
        except Exception as e:
            log.error("Error getting open positions: %s", e)
            return []

    async def get_recent_signals(self, limit: int = 20) -> pd.DataFrame:
        if not await self.ensure_database():
            return pd.DataFrame()

        try:
            async with self.db.connection() as conn:
                rows = await conn.fetch("""
                    SELECT
                        sl.timestamp_utc,
                        i.symbol,
                        sl.confidence,
                        sl.direction,
                        sl.signal_emitted,
                        sl.composite_score,
                        sl.rejection_gate
                    FROM signal_logs sl
                    LEFT JOIN instruments i ON sl.instrument_id = i.instrument_id
                    ORDER BY sl.timestamp_utc DESC
                    LIMIT $1
                """, limit)
            return pd.DataFrame([{
                "Timestamp": row["timestamp_utc"],
                "Symbol": row["symbol"] or "Unknown",
                "Direction": row["direction"],
                "Confidence": round(float(row["confidence"] or 0), 4),
                "Score": round(float(row["composite_score"] or 0), 4),
                "Emitted": "Yes" if row["signal_emitted"] else "No",
                "Gate": row["rejection_gate"] or "",
            } for row in rows])
        except Exception as e:
            log.error("Error fetching signals: %s", e)
            return pd.DataFrame()

    async def get_trade_history(self, limit: int = 20) -> pd.DataFrame:
        """Get recent ledger events using the current event-ledger schema."""
        if not await self.ensure_database():
            return pd.DataFrame()

        try:
            async with self.db.connection() as conn:
                rows = await conn.fetch("""
                    SELECT
                        tl.timestamp_utc,
                        tl.event_type,
                        i.symbol,
                        tl.direction,
                        tl.qty_delta,
                        tl.qty_open,
                        tl.fill_price,
                        tl.realized_pnl_usd,
                        tl.status,
                        tl.note
                    FROM trade_ledger tl
                    LEFT JOIN instruments i ON tl.instrument_id = i.instrument_id
                    ORDER BY tl.timestamp_utc DESC
                    LIMIT $1
                """, limit)
            return pd.DataFrame([{
                "Timestamp": row["timestamp_utc"],
                "Event": row["event_type"],
                "Symbol": row["symbol"] or "Unknown",
                "Direction": row["direction"],
                "Qty Delta": float(row["qty_delta"] or 0),
                "Qty Open": float(row["qty_open"] or 0),
                "Fill": round(float(row["fill_price"] or 0), 5),
                "Realized P&L": round(float(row["realized_pnl_usd"] or 0), 2),
                "Status": row["status"] or "",
                "Note": row["note"] or "",
            } for row in rows])
        except Exception as e:
            log.error("Error fetching trades: %s", e)
            return pd.DataFrame()

    async def get_signal_stats(self) -> Dict:
        if not await self.ensure_database():
            return {}

        try:
            async with self.db.connection() as conn:
                row = await conn.fetchrow("""
                    SELECT
                        COUNT(*) AS total_signals,
                        COUNT(*) FILTER (WHERE signal_emitted = true) AS emitted_signals,
                        AVG(confidence) AS avg_confidence
                    FROM signal_logs
                    WHERE timestamp_utc > NOW() - INTERVAL '24 hours'
                """)
            return {
                "total_signals": row["total_signals"] or 0,
                "emitted_signals": row["emitted_signals"] or 0,
                "avg_confidence": round(float(row["avg_confidence"] or 0), 4),
            }
        except Exception as e:
            log.error("Error getting signal stats: %s", e)
            return {}

    async def get_trade_stats(self) -> Dict:
        if not await self.ensure_database():
            return {}

        try:
            async with self.db.connection() as conn:
                row = await conn.fetchrow("""
                    SELECT
                        COUNT(*) AS total_trades,
                        COUNT(*) FILTER (WHERE realized_pnl_usd > 0) AS winning_trades,
                        SUM(realized_pnl_usd) AS total_pnl
                    FROM trade_ledger
                    WHERE event_type = 'POSITION_CLOSED'
                      AND timestamp_utc > NOW() - INTERVAL '24 hours'
                """)
            total = row["total_trades"] or 0
            wins = row["winning_trades"] or 0
            win_rate = (wins / total * 100) if total else 0
            return {
                "total_trades": total,
                "winning_trades": wins,
                "win_rate": round(win_rate, 2),
                "total_pnl": round(float(row["total_pnl"] or 0), 2),
            }
        except Exception as e:
            log.error("Error getting trade stats: %s", e)
            return {}

    async def get_instrument_performance(self) -> pd.DataFrame:
        if not await self.ensure_database():
            return pd.DataFrame()

        try:
            async with self.db.connection() as conn:
                rows = await conn.fetch("""
                    SELECT
                        i.symbol,
                        COUNT(tl.trade_id) AS trades,
                        COUNT(tl.trade_id) FILTER (WHERE tl.realized_pnl_usd > 0) AS wins,
                        SUM(tl.realized_pnl_usd) AS total_pnl
                    FROM instruments i
                    LEFT JOIN trade_ledger tl ON i.instrument_id = tl.instrument_id
                        AND tl.event_type = 'POSITION_CLOSED'
                        AND tl.timestamp_utc > NOW() - INTERVAL '24 hours'
                    GROUP BY i.symbol
                    ORDER BY total_pnl DESC NULLS LAST
                """)
            data = []
            for row in rows:
                trades = row["trades"] or 0
                wins = row["wins"] or 0
                pnl = float(row["total_pnl"] or 0)
                win_rate = (wins / trades * 100) if trades else 0
                data.append({
                    "Symbol": row["symbol"],
                    "Trades": trades,
                    "Wins": wins,
                    "Win Rate": f"{win_rate:.1f}%",
                    "P&L": round(pnl, 2),
                })
            return pd.DataFrame(data)
        except Exception as e:
            log.error("Error getting instrument performance: %s", e)
            return pd.DataFrame()

    def disconnect(self):
        self.connection = None
        self._last_account_info = None


_service: Optional[DashboardService] = None


def get_dashboard_service() -> DashboardService:
    global _service
    if _service is None:
        _service = DashboardService()
    return _service
