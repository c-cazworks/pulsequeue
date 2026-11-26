"""Delivery signals and the alert rules the UI shows, evaluated from Postgres.

Prometheus (deploy/observability/alerts.yml) uses the same thresholds for paging locally; these
exist so the console shows alerts on EKS too, where there is no Prometheus. One worker replica
evaluates at a time, holding an advisory lock.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

import asyncpg

from app.scenarios import EVENT_TYPES

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60
MIN_ATTEMPTS = 20  # below this, one failure on an idle type is not a rate
ERROR_RATE_THRESHOLD = 0.05
QUEUE_LAG_THRESHOLD_SECONDS = 30
DEAD_LETTER_WINDOW_SECONDS = 300
# The `recover` scenario switches back to `healthy` once the queue has caught up and no rule
# is breaching.
RECOVERED_LAG_SECONDS = 5
EVALUATOR_LOCK_KEY = 7_301_002

# $1 is the evaluation time and $2 a window in seconds. Both are cast: Postgres cannot infer them.
ATTEMPTS_WINDOW = """
    FROM attempts
    WHERE finished_at > $1::timestamptz - make_interval(secs => $2::float8)
      AND finished_at <= $1::timestamptz
"""
TYPE_SQL = f"""
    SELECT event_type,
           count(*) AS attempts,
           count(*) FILTER (WHERE ok) AS ok,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms
    {ATTEMPTS_WINDOW}
    GROUP BY event_type
"""  # noqa: S608 - constant SQL
TOTAL_SQL = f"""
    SELECT count(*) AS attempts,
           count(*) FILTER (WHERE ok) AS ok,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms
    {ATTEMPTS_WINDOW}
"""  # noqa: S608 - constant SQL
# Lag counts only events that are ready: one waiting out a retry backoff is not late.
QUEUE_SQL = """
    WITH t AS (SELECT $1::timestamptz AS now, make_interval(secs => $2::float8) AS dead_window)
    SELECT count(*) FILTER (WHERE status = 'pending') AS pending,
           count(*) FILTER (WHERE status = 'pending' AND attempts > 0) AS retrying,
           count(*) FILTER (WHERE status = 'failed') AS failed,
           count(*) FILTER (WHERE status = 'failed' AND processed_at > t.now - t.dead_window)
               AS dead_lettered,
           coalesce(extract(epoch FROM t.now - min(available_at)
                            FILTER (WHERE status = 'pending' AND available_at <= t.now)), 0)::float
               AS lag_seconds
    FROM t LEFT JOIN events ON true  -- still one row when there are no events
    GROUP BY t.now, t.dead_window
"""


@dataclass(frozen=True)
class TypeSignal:
    event_type: str
    attempts: int
    error_rate: float
    p95_ms: float | None
    slo_ms: float


@dataclass(frozen=True)
class Signals:
    at: datetime
    window_seconds: int
    attempts: int
    error_rate: float
    p95_ms: float | None
    pending: int
    retrying: int
    failed: int
    dead_lettered: int  # failed within DEAD_LETTER_WINDOW_SECONDS
    lag_seconds: float
    types: list[TypeSignal]


def _rate(attempts: int, ok: int) -> float:
    return (attempts - ok) / attempts if attempts else 0.0


async def read_signals(conn: asyncpg.Connection | asyncpg.Pool, now: datetime) -> Signals:
    rows = {r["event_type"]: r for r in await conn.fetch(TYPE_SQL, now, WINDOW_SECONDS)}
    total = await conn.fetchrow(TOTAL_SQL, now, WINDOW_SECONDS)
    queue = await conn.fetchrow(QUEUE_SQL, now, DEAD_LETTER_WINDOW_SECONDS)
    types = []
    for name, kind in EVENT_TYPES.items():
        row = rows.get(name)
        attempts = row["attempts"] if row else 0
        types.append(
            TypeSignal(
                name,
                attempts,
                _rate(attempts, row["ok"]) if row else 0.0,
                row["p95_ms"] if row else None,
                kind.slo_ms,
            )
        )
    return Signals(
        at=now,
        window_seconds=WINDOW_SECONDS,
        attempts=total["attempts"],
        error_rate=_rate(total["attempts"], total["ok"]),
        p95_ms=total["p95_ms"],
        pending=queue["pending"],
        retrying=queue["retrying"],
        failed=queue["failed"],
        dead_lettered=queue["dead_lettered"],
        lag_seconds=queue["lag_seconds"],
        types=types,
    )


@dataclass(frozen=True)
class Breach:
    rule: str
    key: str  # event type, or '' for queue-wide rules
    severity: str
    for_seconds: int
    value: float
    summary: str


def breaches(s: Signals) -> list[Breach]:
    """Every rule currently breaching. Thresholds match deploy/observability/alerts.yml."""
    found = []
    for t in s.types:
        if t.attempts < MIN_ATTEMPTS:
            continue
        if t.error_rate > ERROR_RATE_THRESHOLD:
            found.append(
                Breach(
                    "DeliveryErrorRate",
                    t.event_type,
                    "warning",
                    30,
                    t.error_rate,
                    f"{t.error_rate:.0%} of {t.event_type} deliveries failing (threshold 5%)",
                )
            )
        if t.p95_ms is not None and t.p95_ms > t.slo_ms:
            found.append(
                Breach(
                    "LatencySLOBreach",
                    t.event_type,
                    "warning",
                    30,
                    t.p95_ms,
                    f"{t.event_type} p95 is {t.p95_ms:.0f} ms (SLO {t.slo_ms:.0f} ms)",
                )
            )
    if s.lag_seconds > QUEUE_LAG_THRESHOLD_SECONDS:
        found.append(
            Breach(
                "QueueLag",
                "",
                "critical",
                30,
                s.lag_seconds,
                f"Ready events have waited {s.lag_seconds:.0f} s for a worker (threshold 30 s)",
            )
        )
    if s.dead_lettered:
        found.append(
            Breach(
                "EventsDeadLettered",
                "",
                "warning",
                0,
                s.dead_lettered,
                f"{s.dead_lettered} events exhausted their retries in 5 minutes",
            )
        )
    return found


OPEN_SQL = "SELECT id, rule, key, pending_since, fired_at FROM alerts WHERE resolved_at IS NULL"
INSERT_SQL = """
    INSERT INTO alerts (rule, key, severity, summary, value, pending_since, fired_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7)
"""
UPDATE_SQL = "UPDATE alerts SET summary = $2, value = $3, fired_at = $4 WHERE id = $1"
RESOLVE_SQL = "UPDATE alerts SET resolved_at = $2 WHERE id = $1"
RECOVERED_SQL = "UPDATE scenario SET profile = 'healthy', changed_at = $1 WHERE profile = 'recover'"


async def evaluate(conn: asyncpg.Connection, now: datetime | None = None) -> bool:
    """Advance every alert one step. Returns False if another replica holds the lock."""
    async with conn.transaction():
        if not await conn.fetchval("SELECT pg_try_advisory_xact_lock($1)", EVALUATOR_LOCK_KEY):
            return False
        now = now or await conn.fetchval("SELECT now()")
        signals = await read_signals(conn, now)
        current = {(b.rule, b.key): b for b in breaches(signals)}
        open_alerts = {(r["rule"], r["key"]): r for r in await conn.fetch(OPEN_SQL)}

        for key, b in current.items():
            row = open_alerts.get(key)
            if row is None:
                fired = now if b.for_seconds == 0 else None
                await conn.execute(
                    INSERT_SQL, b.rule, b.key, b.severity, b.summary, b.value, now, fired
                )
                continue
            fired = row["fired_at"]
            if fired is None and (now - row["pending_since"]).total_seconds() >= b.for_seconds:
                fired = now
                logger.warning("alert firing", extra={"rule": b.rule, "key": b.key})
            await conn.execute(UPDATE_SQL, row["id"], b.summary, b.value, fired)

        for key, row in open_alerts.items():
            if key in current:
                continue
            if row["fired_at"] is None:  # never fired: forget it
                await conn.execute("DELETE FROM alerts WHERE id = $1", row["id"])
            else:
                await conn.execute(
                    "UPDATE alerts SET resolved_at = $2 WHERE id = $1", row["id"], now
                )
                logger.info("alert resolved", extra={"rule": key[0], "key": key[1]})

        if signals.lag_seconds < RECOVERED_LAG_SECONDS and not current:
            await conn.execute(RECOVERED_SQL, now)
    return True


# Processed rows are only needed for the rolling window and the recent-events table.
PRUNE_SQL = [
    "DELETE FROM attempts WHERE finished_at < now() - interval '1 hour'",
    "DELETE FROM events WHERE status = 'processed' AND processed_at < now() - interval '1 hour'",
    "DELETE FROM events WHERE status = 'failed' AND processed_at < now() - interval '1 day'",
    "DELETE FROM alerts WHERE resolved_at < now() - interval '1 day'",
]


async def prune(conn: asyncpg.Connection) -> None:
    for sql in PRUNE_SQL:
        await conn.execute(sql)
