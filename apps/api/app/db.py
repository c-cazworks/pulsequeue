import asyncio

import asyncpg

POOL_CLOSE_TIMEOUT_SECONDS = 5.0


async def create_pool(max_size: int = 10) -> asyncpg.Pool:
    """Create a pool configured by the standard PGHOST/PGUSER/PGPASSWORD/PGDATABASE env vars.

    min_size=0 connects lazily, so the process starts (and stays live) while Postgres is
    unavailable; readiness reports the outage instead of the pod crash-looping.
    """
    return await asyncpg.create_pool(min_size=0, max_size=max_size, timeout=5, command_timeout=10)


async def close_pool(pool: asyncpg.Pool) -> None:
    """Close gracefully, but never outlive the shutdown grace period."""
    try:
        async with asyncio.timeout(POOL_CLOSE_TIMEOUT_SECONDS):
            await pool.close()
    except TimeoutError:
        pool.terminate()
