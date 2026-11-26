import asyncio
import logging
from pathlib import Path

import asyncpg

from app.db import password_source
from app.observability import configure_logging

logger = logging.getLogger(__name__)
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
LOCK_KEY = 7_301_001  # arbitrary; serializes concurrent migrators


async def apply_schema(conn: asyncpg.Connection) -> None:
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", LOCK_KEY)
        await conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))


async def main() -> None:
    configure_logging()
    conn = await asyncpg.connect(timeout=10, command_timeout=60, password=password_source())
    try:
        await apply_schema(conn)
    finally:
        await conn.close()
    logger.info("schema applied")


if __name__ == "__main__":
    asyncio.run(main())
