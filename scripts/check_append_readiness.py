"""Check whether Citadel Bot can append live data to PostgreSQL/Neon."""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from citadel_bot.config import BotConfig
from citadel_bot.database.database_manager import close_database, db_manager, init_database


TABLES = [
    "instruments",
    "market_data",
    "signal_logs",
    "trade_ledger",
    "buffer_calibration",
]


def safe_db_target() -> str:
    config = db_manager.config
    if config.get("dsn"):
        dsn = config["dsn"]
        if "@" in dsn:
            return "***@" + dsn.split("@", 1)[1]
        return "***"
    return f"{config.get('user')}@{config.get('host')}:{config.get('port')}/{config.get('database')}"


async def main() -> int:
    bot_config = BotConfig.from_file("config.yaml")
    try:
        await init_database(
            {
                "database_url": bot_config.database_url,
                "host": bot_config.database_host,
                "port": bot_config.database_port,
                "database": bot_config.database_name,
                "user": bot_config.database_user,
                "password": bot_config.database_password,
            }
        )
        print("Database target:", safe_db_target())
        print("Configured symbols:", ", ".join(bot_config.instruments))

        async with db_manager.connection() as conn:
            for table in TABLES:
                count = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")
                print(f"{table}: {count}")

            print("\nConfigured symbol mapping:")
            missing = []
            for symbol in bot_config.instruments:
                instrument_id = await conn.fetchval(
                    "SELECT instrument_id FROM instruments WHERE symbol = $1",
                    symbol,
                )
                if instrument_id:
                    print(f"  {symbol}: instrument_id={instrument_id}")
                else:
                    print(f"  {symbol}: MISSING")
                    missing.append(symbol)

            column = await conn.fetchrow(
                """
                SELECT is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'market_data'
                  AND column_name = 'metaapi_account_id'
                """
            )
            print("\nmarket_data.metaapi_account_id:")
            if column:
                print(f"  nullable={column['is_nullable']} default={column['column_default']}")
            else:
                print("  MISSING")
                missing.append("market_data.metaapi_account_id")

            unique_indexes = await conn.fetch(
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = 'public'
                  AND tablename = 'market_data'
                  AND indexdef ILIKE '%instrument_id%'
                  AND indexdef ILIKE '%timestamp_utc%'
                  AND indexdef ILIKE '%timeframe%'
                  AND indexdef ILIKE '%metaapi_account_id%'
                ORDER BY indexname
                """
            )
            print("\nmarket_data account-scoped indexes:")
            for row in unique_indexes:
                print(f"  {row['indexname']}: {row['indexdef']}")
            if not unique_indexes:
                print("  MISSING")
                missing.append("market_data account-scoped unique/index")

        if missing:
            print("\nAppend readiness: FAILED")
            print("Missing or invalid:", ", ".join(missing))
            return 2

        print("\nAppend readiness: OK")
        return 0
    except Exception as exc:
        print("Append readiness check failed:", exc)
        return 1
    finally:
        await close_database()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
