import asyncio
from citadel_bot.database.database_manager import db_manager, init_database

async def main():
    await init_database()
    sym = 'NDAQ'
    m = await db_manager.get_latest_market_data(sym)
    print('latest_market_data for', sym, '=>', m)
    await db_manager.disconnect()

if __name__ == '__main__':
    asyncio.run(main())


