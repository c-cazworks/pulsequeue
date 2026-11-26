"""Queue and alert semantics against real Postgres. Opt in with PULSEQUEUE_TEST_DB=1 plus PG*.

Runs in a throwaway schema, so it is safe to point at a development database.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import asyncpg
import pytest

from app import alerts, worker
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


async def add_event(pool: asyncpg.Pool, event_type: str = "payment.authorized") -> int:
    return await pool.fetchval(
        "INSERT INTO events (event_type) VALUES ($1) RETURNING id", event_type
    )


async def get_event(pool: asyncpg.Pool, event_id: int) -> asyncpg.Record:
    return await pool.fetchrow("SELECT * FROM events WHERE id = $1", event_id)


async def ok(_event: asyncpg.Record) -> None:
    pass


async def test_replaces_the_original_telemetry_table(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE events")
        await conn.execute("CREATE TABLE events (id int, source text, metric text)")
        await apply_schema(conn)
        assert await add_event(pool)


async def test_processes_event_once(pool: asyncpg.Pool) -> None:
    event_id = await add_event(pool)
    assert await worker.process_one(pool, ok)
    event = await get_event(pool, event_id)
    assert (event["status"], event["attempts"]) == ("processed", 1)
    assert event["duration_ms"] is not None
    assert not await worker.process_one(pool, ok)
    attempts = await pool.fetch("SELECT * FROM attempts WHERE event_id = $1", event_id)
    assert [(a["attempt"], a["ok"]) for a in attempts] == [(1, True)]


async def test_concurrent_workers_do_not_double_claim(pool: asyncpg.Pool) -> None:
    ids = {await add_event(pool), await add_event(pool)}
    seen: list[int] = []
    both_claimed = asyncio.Barrier(2)

    async def handler(event: asyncpg.Record) -> None:
        seen.append(event["id"])
        await both_claimed.wait()

    async with asyncio.timeout(5):
        await asyncio.gather(*(worker.process_one(pool, handler) for _ in range(2)))
    assert len(seen) == 2
    assert set(seen) == ids


async def test_failure_backs_off_then_fails_permanently(pool: asyncpg.Pool) -> None:
    event_id = await add_event(pool)

    async def boom(_event: asyncpg.Record) -> None:
        raise RuntimeError("boom")

    for attempt in range(1, worker.MAX_ATTEMPTS + 1):
        assert await worker.process_one(pool, boom)
        assert (await get_event(pool, event_id))["attempts"] == attempt
        assert not await worker.process_one(pool, boom)  # not available during backoff
        await pool.execute("UPDATE events SET available_at = now()")
    event = await get_event(pool, event_id)
    assert event["status"] == "failed"
    assert event["processed_at"] is not None  # when it was dead-lettered
    assert "boom" in event["last_error"]
    assert await pool.fetchval("SELECT count(*) FROM attempts WHERE NOT ok") == worker.MAX_ATTEMPTS


async def test_cancelled_worker_returns_event_to_queue(pool: asyncpg.Pool) -> None:
    event_id = await add_event(pool)
    started = asyncio.Event()

    async def hang(_event: asyncpg.Record) -> None:
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(worker.process_one(pool, hang))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await get_event(pool, event_id))["status"] == "pending"
    assert await pool.fetchval("SELECT count(*) FROM attempts") == 0  # rolled back too
    assert await worker.process_one(pool, ok)


# --- Alerts


async def add_attempts(pool: asyncpg.Pool, event_type: str, n: int, failed: int, ms: float) -> None:
    await pool.executemany(
        "INSERT INTO attempts (event_id, event_type, attempt, ok, duration_ms) "
        "VALUES ($1, $2, 1, $3, $4)",
        [(i, event_type, i >= failed, ms) for i in range(n)],
    )


async def open_alerts(pool: asyncpg.Pool) -> dict[tuple[str, str], asyncpg.Record]:
    rows = await pool.fetch("SELECT * FROM alerts WHERE resolved_at IS NULL")
    return {(r["rule"], r["key"]): r for r in rows}


async def test_alert_goes_pending_then_fires_then_resolves(pool: asyncpg.Pool) -> None:
    await add_attempts(pool, "webhook.partner_notify", 100, failed=30, ms=200)
    await add_attempts(pool, "payment.authorized", 100, failed=0, ms=80)
    key = ("DeliveryErrorRate", "webhook.partner_notify")
    async with pool.acquire() as conn:
        t0 = await conn.fetchval("SELECT now()")
        assert await alerts.evaluate(conn, t0)
        pending = await open_alerts(pool)
        assert list(pending) == [key]  # payments are healthy
        assert pending[key]["fired_at"] is None  # must hold for 30 s first
        assert "30%" in pending[key]["summary"]

        await alerts.evaluate(conn, t0 + timedelta(seconds=31))
        assert (await open_alerts(pool))[key]["fired_at"] is not None

        # The attempts age out of the 60 s window: the alert resolves, and stays in history.
        await alerts.evaluate(conn, t0 + timedelta(seconds=120))
        assert not await open_alerts(pool)
        assert await pool.fetchval("SELECT count(*) FROM alerts WHERE resolved_at IS NOT NULL") == 1


async def test_a_brief_breach_never_fires(pool: asyncpg.Pool) -> None:
    await add_attempts(pool, "webhook.partner_notify", 100, failed=30, ms=200)
    async with pool.acquire() as conn:
        t0 = await conn.fetchval("SELECT now()")
        await alerts.evaluate(conn, t0)
        await alerts.evaluate(conn, t0 + timedelta(seconds=120))
    assert await pool.fetchval("SELECT count(*) FROM alerts") == 0


async def test_latency_slo_uses_each_types_objective(pool: asyncpg.Pool) -> None:
    await add_attempts(pool, "payment.authorized", 50, failed=0, ms=400)  # SLO 250 ms
    await add_attempts(pool, "webhook.partner_notify", 50, failed=0, ms=400)  # SLO 1000 ms
    async with pool.acquire() as conn:
        await alerts.evaluate(conn)
    assert set(await open_alerts(pool)) == {("LatencySLOBreach", "payment.authorized")}


async def test_queue_lag_ignores_events_waiting_out_a_backoff(pool: asyncpg.Pool) -> None:
    await pool.execute(
        "INSERT INTO events (event_type, attempts, available_at) VALUES "
        "('a', 1, now() + interval '1 minute'), ('a', 0, now() - interval '45 seconds')"
    )
    async with pool.acquire() as conn:
        s = await alerts.read_signals(conn, await conn.fetchval("SELECT now()"))
        assert (s.pending, s.retrying) == (2, 1)
        assert 44 < s.lag_seconds < 50
        await alerts.evaluate(conn)
    assert ("QueueLag", "") in await open_alerts(pool)


async def test_only_one_replica_evaluates_at_a_time(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as a, pool.acquire() as b, a.transaction():
        await a.execute("SELECT pg_advisory_xact_lock($1)", alerts.EVALUATOR_LOCK_KEY)
        assert not await alerts.evaluate(b)


async def test_recover_returns_to_healthy_once_caught_up(pool: asyncpg.Pool) -> None:
    await pool.execute("UPDATE scenario SET profile = 'recover'")
    lagging = await pool.fetchval(
        "INSERT INTO events (event_type, available_at) "
        "VALUES ('a', now() - interval '45 seconds') RETURNING id"
    )
    async with pool.acquire() as conn:
        await alerts.evaluate(conn)
        assert await conn.fetchval("SELECT profile FROM scenario") == "recover"
        await conn.execute("UPDATE events SET status = 'processed' WHERE id = $1", lagging)
        await alerts.evaluate(conn)
        assert await conn.fetchval("SELECT profile FROM scenario") == "healthy"


async def test_prune_keeps_recent_and_dead_lettered_events(pool: asyncpg.Pool) -> None:
    await pool.execute(
        "INSERT INTO events (event_type, status, processed_at) VALUES "
        "('old', 'processed', now() - interval '2 hours'), ('new', 'processed', now()), "
        "('dead', 'failed', now() - interval '2 hours')"
    )
    async with pool.acquire() as conn:
        await alerts.prune(conn)
    remaining = {r["event_type"] for r in await pool.fetch("SELECT event_type FROM events")}
    assert remaining == {"new", "dead"}
