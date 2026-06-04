"""Check Citadel database connectivity and expected schema."""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from citadel_bot.database.database_manager import close_database, db_manager, init_database


EXPECTED_TABLES = [
    "instruments",
    "market_data",
    "signal_logs",
    "trade_ledger",
    "buffer_calibration",
]


def _safe_config():
    config = db_manager.config.copy()
    if config.get("password"):
        config["password"] = "***"
    if config.get("dsn"):
        config["dsn"] = "***"
    return config


async def main() -> int:
    print("Database config:", _safe_config())
    try:
        await init_database()
        async with db_manager.connection() as conn:
            version = await conn.fetchval("SELECT version()")
            print("Connected:", version.split(",", 1)[0])

            rows = await conn.fetch(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = ANY($1::text[])
                ORDER BY table_name
                """,
                EXPECTED_TABLES,
            )
            found = {row["table_name"] for row in rows}
            missing = [table for table in EXPECTED_TABLES if table not in found]
            if missing:
                print("Missing tables:", ", ".join(missing))
                return 2

            instrument_count = await conn.fetchval("SELECT COUNT(*) FROM instruments")
            print("Schema OK")
            print("Instrument rows:", instrument_count)
            if instrument_count == 0:
                print("Warning: instruments table is empty; run the migration/seed step.")
        return 0
    except Exception as exc:
        print("Database check failed:", exc)
        return 1
    finally:
        await close_database()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
