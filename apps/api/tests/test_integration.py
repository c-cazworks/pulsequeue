"""Queue semantics against real Postgres. Opt in with PULSEQUEUE_TEST_DB=1 plus PG* env vars.

Runs in a throwaway schema, so it is safe to point at a development database.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest

from app import worker
from app.migrate import apply_schema

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("PULSEQUEUE_TEST_DB") != "1", reason="PULSEQUEUE_TEST_DB is not 1"
    ),
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def pool(anyio_backend: str) -> AsyncIterator[asyncpg.Pool]:
    schema = f"test_{uuid.uuid4().hex}"
    admin = await asyncpg.connect()
    await admin.execute(f"CREATE SCHEMA {schema}")
    try:
        async with asyncpg.create_pool(
            min_size=0, max_size=5, server_settings={"search_path": schema}
        ) as test_pool:
            async with test_pool.acquire() as conn:
                await apply_schema(conn)
                await apply_schema(conn)  # idempotent
            yield test_pool
    finally:
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()


async def add_event(pool: asyncpg.Pool, value: float = 1.0, threshold: float | None = None) -> int:
    return await pool.fetchval(
        "INSERT INTO events (source, metric, value, threshold) VALUES ('s', 'm', $1, $2) "
        "RETURNING id",
        value,
        threshold,
    )


async def get_event(pool: asyncpg.Pool, event_id: int) -> asyncpg.Record:
    return await pool.fetchrow("SELECT * FROM events WHERE id = $1", event_id)


async def test_processes_event_once(pool: asyncpg.Pool) -> None:
    event_id = await add_event(pool, value=2, threshold=1)
    assert await worker.process_one(pool)
    event = await get_event(pool, event_id)
    assert (event["status"], event["breached"], event["attempts"]) == ("processed", True, 1)
    assert not await worker.process_one(pool)


async def test_concurrent_workers_do_not_double_claim(pool: asyncpg.Pool) -> None:
    ids = {await add_event(pool), await add_event(pool)}
    seen: list[int] = []
    both_claimed = asyncio.Barrier(2)

    async def handler(event: asyncpg.Record) -> bool:
        seen.append(event["id"])
        await both_claimed.wait()
        return False

    async with asyncio.timeout(5):
        await asyncio.gather(*(worker.process_one(pool, handler) for _ in range(2)))
    assert len(seen) == 2
    assert set(seen) == ids


async def test_failure_backs_off_then_fails_permanently(pool: asyncpg.Pool) -> None:
    event_id = await add_event(pool)

    async def boom(_event: asyncpg.Record) -> bool:
        raise RuntimeError("boom")

    for attempt in range(1, worker.MAX_ATTEMPTS + 1):
        assert await worker.process_one(pool, boom)
        assert (await get_event(pool, event_id))["attempts"] == attempt
        assert not await worker.process_one(pool, boom)  # not available during backoff
        await pool.execute("UPDATE events SET available_at = now()")
    event = await get_event(pool, event_id)
    assert event["status"] == "failed"
    assert "boom" in event["last_error"]


async def test_cancelled_worker_returns_event_to_queue(pool: asyncpg.Pool) -> None:
    event_id = await add_event(pool)
    started = asyncio.Event()

    async def hang(_event: asyncpg.Record) -> bool:
        started.set()
        await asyncio.sleep(60)
        return False

    task = asyncio.create_task(worker.process_one(pool, hang))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await get_event(pool, event_id))["status"] == "pending"
    assert await worker.process_one(pool)
