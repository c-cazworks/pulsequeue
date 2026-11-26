"""Background worker: claims pending events from Postgres and delivers them.

Each event is claimed and finished in one transaction using FOR UPDATE SKIP LOCKED, so replicas
never double-claim and a killed worker's event returns to the queue when its connection drops.
Delivery is simulated; the active scenario (app/scenarios.py) decides how it behaves.
"""

import asyncio
import logging
import signal
import time
from collections.abc import Awaitable, Callable

import asyncpg
from prometheus_client import start_http_server

from app import alerts, heartbeat, scenarios
from app.db import close_pool, create_pool
from app.observability import (
    DELIVERY_SLO,
    QUEUE_FAILED,
    QUEUE_LAG,
    QUEUE_PENDING,
    SCENARIO,
    WORKER_EVENT_DURATION,
    WORKER_EVENTS,
    WORKER_LAST_POLL,
    configure_logging,
)

logger = logging.getLogger(__name__)

POLL_SECONDS = 1.0
ERROR_DELAY_SECONDS = 5.0
MAX_ATTEMPTS = 5
MAX_BACKOFF_SECONDS = 60
HANDLER_TIMEOUT_SECONDS = 10.0
# Bounds the SIGTERM drain: 20s + 5s pool close stays under the 30s grace period.
ITERATION_TIMEOUT_SECONDS = 20.0
METRICS_PORT = 9100
# Scenario refresh, queue gauges and alert evaluation. Short, so a scenario click shows quickly.
MAINTENANCE_SECONDS = 2.0
PRUNE_EVERY_TICKS = 30

CLAIM_SQL = """
    SELECT id, event_type, attempts
    FROM events
    WHERE status = 'pending' AND available_at <= now()
    ORDER BY available_at, id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
"""
DONE_SQL = """
    UPDATE events
    SET status = 'processed', attempts = $2, duration_ms = $3, last_error = NULL,
        processed_at = now()
    WHERE id = $1
"""
RETRY_SQL = """
    UPDATE events
    SET status = $2, attempts = $3, last_error = $4, duration_ms = $6,
        available_at = now() + make_interval(secs => $5::double precision),
        processed_at = CASE WHEN $2 = 'failed' THEN now() END
    WHERE id = $1
"""
ATTEMPT_SQL = """
    INSERT INTO attempts (event_id, event_type, attempt, ok, duration_ms)
    VALUES ($1, $2, $3, $4, $5)
"""

Handler = Callable[[asyncpg.Record], Awaitable[None]]

# Refreshed by maintain(); read by every delivery.
current_profile: str = scenarios.DEFAULT_PROFILE


async def deliver(event: asyncpg.Record) -> None:
    await scenarios.deliver(
        current_profile, event["event_type"], event["id"], event["attempts"] + 1
    )


async def process_one(pool: asyncpg.Pool, handler: Handler = deliver) -> bool:
    """Process at most one event. Returns False if none was available."""
    async with pool.acquire() as conn, conn.transaction():
        event = await conn.fetchrow(CLAIM_SQL)
        if event is None:
            return False
        attempts = event["attempts"] + 1
        event_type = event["event_type"]
        start = time.perf_counter()
        try:
            async with asyncio.timeout(HANDLER_TIMEOUT_SECONDS):
                await handler(event)
        except Exception as exc:
            elapsed = time.perf_counter() - start
            status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            backoff = min(2**attempts, MAX_BACKOFF_SECONDS)
            logger.warning(
                "delivery failed",
                extra={
                    "event_id": event["id"],
                    "event_type": event_type,
                    "attempt": attempts,
                    "next": "dead-letter" if status == "failed" else f"retry in {backoff}s",
                    "error": repr(exc)[:200],
                },
                # A stack trace helps for real bugs, not for simulated downstream failures.
                exc_info=not isinstance(exc, scenarios.DeliveryError),
            )
            await conn.execute(
                RETRY_SQL,
                event["id"],
                status,
                attempts,
                str(exc)[:500] or repr(exc)[:500],
                float(backoff),
                elapsed * 1000,
            )
            outcome = "failed" if status == "failed" else "retried"
            ok = False
        else:
            elapsed = time.perf_counter() - start
            await conn.execute(DONE_SQL, event["id"], attempts, elapsed * 1000)
            outcome, ok = "processed", True
        await conn.execute(ATTEMPT_SQL, event["id"], event_type, attempts, ok, elapsed * 1000)
    # Counted after commit, so a rolled-back attempt is never reported.
    WORKER_EVENTS.labels(outcome, event_type).inc()
    WORKER_EVENT_DURATION.labels(event_type).observe(elapsed)
    return True


async def tick(pool: asyncpg.Pool, count: int) -> None:
    """One maintenance step: scenario, queue gauges, alerts, and now and then pruning."""
    global current_profile
    async with pool.acquire() as conn:
        profile = await conn.fetchval("SELECT profile FROM scenario")
        if profile in scenarios.PROFILES:
            current_profile = profile
        for name in scenarios.PROFILES:
            SCENARIO.labels(name).set(1 if name == current_profile else 0)

        signals = await alerts.read_signals(conn, await conn.fetchval("SELECT now()"))
        QUEUE_PENDING.set(signals.pending)
        QUEUE_FAILED.set(signals.failed)
        QUEUE_LAG.set(signals.lag_seconds)

        await alerts.evaluate(conn)
        if count % PRUNE_EVERY_TICKS == 0:
            await alerts.prune(conn)


async def maintain(pool: asyncpg.Pool, stop: asyncio.Event) -> None:
    """Run tick() until stopped. A failed tick only delays the next one."""
    count = 0
    while not stop.is_set():
        try:
            async with asyncio.timeout(10):
                await tick(pool, count)
        except Exception as exc:
            logger.warning("maintenance failed: %r", exc)
        count += 1
        try:
            async with asyncio.timeout(MAINTENANCE_SECONDS):
                await stop.wait()
        except TimeoutError:
            pass


async def run(pool: asyncpg.Pool, stop: asyncio.Event) -> None:
    """Poll until `stop` is set. The in-flight event always finishes (or rolls back) first."""
    while not stop.is_set():
        heartbeat.touch()  # also during DB outages: restarting the pod would not fix those
        WORKER_LAST_POLL.set_to_current_time()
        delay = POLL_SECONDS
        try:
            async with asyncio.timeout(ITERATION_TIMEOUT_SECONDS):
                if await process_one(pool):
                    continue
        except Exception as exc:
            logger.warning("iteration failed, retrying: %r", exc)
            delay = ERROR_DELAY_SECONDS
        try:
            async with asyncio.timeout(delay):
                await stop.wait()
        except TimeoutError:
            pass


async def main() -> None:
    configure_logging()
    for name, kind in scenarios.EVENT_TYPES.items():
        DELIVERY_SLO.labels(name).set(kind.slo_ms / 1000)
    start_http_server(METRICS_PORT)  # a daemon thread; dies with the process
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # signal.signal rather than loop.add_signal_handler: the latter is missing on Windows.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))

    # One connection for events, one for maintenance, so neither waits on the other.
    pool = await create_pool(max_size=2)
    logger.info("worker started")
    maintenance = asyncio.create_task(maintain(pool, stop))
    try:
        await run(pool, stop)
        logger.info("drained, shutting down")
    finally:
        maintenance.cancel()
        await asyncio.gather(maintenance, return_exceptions=True)
        await close_pool(pool)


if __name__ == "__main__":
    asyncio.run(main())
