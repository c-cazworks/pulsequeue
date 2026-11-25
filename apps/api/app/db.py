import asyncio
import json
import os
from collections.abc import Awaitable, Callable

import asyncpg
import botocore.session
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

POOL_CLOSE_TIMEOUT_SECONDS = 5.0


def password_source() -> Callable[[], Awaitable[str]] | None:
    """Return a Secrets Manager password fetcher, or None to use PGPASSWORD.

    asyncpg calls it for every new connection, so a rotated password needs no restart.
    """
    arn = os.environ.get("DB_SECRET_ARN")
    if not arn:
        return None
    client = botocore.session.get_session().create_client(
        "secretsmanager",
        region_name=arn.split(":")[3],  # Pod Identity does not guarantee AWS_REGION
        config=Config(connect_timeout=2, read_timeout=2, retries={"total_max_attempts": 2}),
    )

    def fetch() -> str:
        try:
            secret = client.get_secret_value(SecretId=arn)["SecretString"]
        except (BotoCoreError, ClientError) as exc:
            # Callers already treat ConnectionError as "database unavailable".
            raise ConnectionError(f"cannot read the database secret: {exc}") from exc
        return json.loads(secret)["password"]

    async def password() -> str:
        return await asyncio.to_thread(fetch)

    return password


async def create_pool(max_size: int = 10) -> asyncpg.Pool:
    """Create a pool configured by PGHOST/PGUSER/PGDATABASE, plus PGPASSWORD or DB_SECRET_ARN.

    min_size=0 connects lazily, so the process starts (and stays live) while Postgres is
    unavailable; readiness reports the outage instead of the pod crash-looping.
    """
    return await asyncpg.create_pool(
        min_size=0, max_size=max_size, timeout=5, command_timeout=10, password=password_source()
    )


async def close_pool(pool: asyncpg.Pool) -> None:
    """Close gracefully, but never outlive the shutdown grace period."""
    try:
        async with asyncio.timeout(POOL_CLOSE_TIMEOUT_SECONDS):
            await pool.close()
    except TimeoutError:
        pool.terminate()
