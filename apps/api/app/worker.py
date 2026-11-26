"""Background worker: claims pending events from Postgres and evaluates them.

Each event is claimed and finished in one transaction using FOR UPDATE SKIP LOCKED, so replicas
never double-claim and a killed worker's event returns to the queue when its connection drops.
"""

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable

import asyncpg
from prometheus_client import start_http_server

from app import heartbeat
from app.db import close_pool, create_pool
from app.observability import (
    QUEUE_FAILED,
    QUEUE_OLDEST_AGE,
    QUEUE_PENDING,
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
QUEUE_STATS_SECONDS = 15.0

CLAIM_SQL = """
    SELECT id, value, threshold, attempts
    FROM events
    WHERE status = 'pending' AND available_at <= now()
    ORDER BY available_at, id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
"""
DONE_SQL = """
    UPDATE events
    SET status = 'processed', breached = $2, attempts = $3, last_error = NULL, processed_at = now()
    WHERE id = $1
"""
RETRY_SQL = """
    UPDATE events
    SET status = $2, attempts = $3, last_error = $4,
        available_at = now() + make_interval(secs => $5::double precision)
    WHERE id = $1
"""

QUEUE_STATS_SQL = """
    SELECT count(*) FILTER (WHERE status = 'pending') AS pending,
           count(*) FILTER (WHERE status = 'failed') AS failed,
           coalesce(
               extract(epoch FROM now() - min(created_at) FILTER (WHERE status = 'pending')), 0
           ) AS oldest_age
    FROM events
"""

Handler = Callable[[asyncpg.Record], Awaitable[bool]]


async def evaluate(event: asyncpg.Record) -> bool:
    """Return whether the event breached its threshold."""
    return event["threshold"] is not None and event["value"] > event["threshold"]


async def process_one(pool: asyncpg.Pool, handler: Handler = evaluate) -> bool:
    """Process at most one event. Returns False if none was available."""
    async with pool.acquire() as conn, conn.transaction():
        event = await conn.fetchrow(CLAIM_SQL)
        if event is None:
            return False
        attempts = event["attempts"] + 1
        try:
            with WORKER_EVENT_DURATION.time():
                async with asyncio.timeout(HANDLER_TIMEOUT_SECONDS):
                    breached = await handler(event)
        except Exception as exc:
            status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            WORKER_EVENTS.labels("failed" if status == "failed" else "retried").inc()
            backoff = min(2**attempts, MAX_BACKOFF_SECONDS)
            logger.exception("event %s attempt %s failed -> %s", event["id"], attempts, status)
            await conn.execute(
                RETRY_SQL, event["id"], status, attempts, repr(exc)[:500], float(backoff)
            )
        else:
            await conn.execute(DONE_SQL, event["id"], breached, attempts)
            WORKER_EVENTS.labels("processed").inc()
        return True


async def report_queue(pool: asyncpg.Pool, stop: asyncio.Event) -> None:
    """Publish queue depth and age. Failures only delay the next sample."""
    while not stop.is_set():
        try:
            async with asyncio.timeout(5):
                row = await pool.fetchrow(QUEUE_STATS_SQL)
            QUEUE_PENDING.set(row["pending"])
            QUEUE_FAILED.set(row["failed"])
            QUEUE_OLDEST_AGE.set(row["oldest_age"])
        except Exception as exc:
            logger.warning("queue stats failed: %r", exc)
        try:
            async with asyncio.timeout(QUEUE_STATS_SECONDS):
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
    start_http_server(METRICS_PORT)  # a daemon thread; dies with the process
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # signal.signal rather than loop.add_signal_handler: the latter is missing on Windows.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))

    # One connection for events, one for queue stats, so neither waits on the other.
    pool = await create_pool(max_size=2)
    logger.info("worker started")
    stats = asyncio.create_task(report_queue(pool, stop))
    try:
        await run(pool, stop)
        logger.info("drained, shutting down")
    finally:
        stats.cancel()
        await asyncio.gather(stats, return_exceptions=True)
        await close_pool(pool)


if __name__ == "__main__":
    asyncio.run(main())
