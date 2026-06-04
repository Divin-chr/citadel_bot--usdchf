import pathlib
import re

p = pathlib.Path('citadel_bot/dashboard_service.py')
text = p.read_text('utf-8')

# Update __init__
text = text.replace(
    "    def __init__(self):\n        self.connection = None\n        self.db_connected = False",
    "    def __init__(self):\n        self.connection = None\n        self.db_connected = False\n        from citadel_bot.database.database_manager import DatabaseManager\n        self.db = DatabaseManager()"
)

# Update ensure_database
old_ensure = """    async def ensure_database(self) -> bool:
        if self.db_connected and db_manager.pool is not None:
            return True
        try:
            if db_manager.pool is None:
                config = BotConfig.from_file("config.yaml")
                await asyncio.wait_for(init_database({
                    "host": config.database_host,
                    "port": config.database_port,
                    "database": config.database_name,
                    "user": config.database_user,
                    "password": config.database_password,
                }), timeout=5)
            self.db_connected = await asyncio.wait_for(db_manager.health_check(), timeout=3)
        except Exception as exc:
            log.debug("Dashboard database unavailable: %s", exc)
            self.db_connected = False
        return self.db_connected"""

new_ensure = """    async def ensure_database(self) -> bool:
        if self.db_connected and self.db.pool is not None:
            return True
        try:
            if self.db.pool is None:
                import asyncio
                config = BotConfig.from_file("config.yaml")
                self.db.configure({
                    "host": config.database_host,
                    "port": config.database_port,
                    "database": config.database_name,
                    "user": config.database_user,
                    "password": config.database_password,
                })
                await asyncio.wait_for(self.db.connect(), timeout=5)
            import asyncio
            self.db_connected = await asyncio.wait_for(self.db.health_check(), timeout=3)
        except Exception as exc:
            log.debug("Dashboard database unavailable: %s", exc)
            self.db_connected = False
        return self.db_connected"""

text = text.replace(old_ensure, new_ensure)

# Replace all `db_manager.connection()` with `self.db.connection()`
text = text.replace("db_manager.connection()", "self.db.connection()")

p.write_text(text, 'utf-8')
