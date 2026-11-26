import json
import os
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, field_validator

from app import alerts, scenarios

router = APIRouter(prefix="/api", tags=["events"])

MAX_PAYLOAD_BYTES = 4096


class EventIn(BaseModel):
    event_type: scenarios.EventType
    payload: dict[str, Any] = {}

    @field_validator("payload")
    @classmethod
    def small_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(payload)) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload must be at most {MAX_PAYLOAD_BYTES} bytes as JSON")
        return payload


class EventAccepted(BaseModel):
    id: int


EventState = Literal["pending", "retrying", "processed", "failed"]


class EventOut(BaseModel):
    id: int
    event_type: str
    reference: str | None  # the payload's `id`, e.g. a payment ID
    state: EventState
    attempts: int
    duration_ms: float | None
    last_error: str | None
    created_at: datetime
    processed_at: datetime | None


EVENT_COLUMNS = (
    "id, event_type, payload->>'id' AS reference, attempts, duration_ms, last_error, "
    "created_at, processed_at, "
    "CASE WHEN status = 'pending' AND attempts > 0 THEN 'retrying' ELSE status END AS state"
)
# Events not yet attempted are left out: the newest rows are always those, and the queue
# figures already count them. "Problems": dead-lettered, retrying, or delivered after a retry.
FILTERS = {
    "all": "attempts > 0",
    "problems": "status = 'failed' OR attempts > 1 OR (status = 'pending' AND attempts > 0)",
}


@router.post("/events", status_code=status.HTTP_202_ACCEPTED)
async def create_event(event: EventIn, request: Request) -> EventAccepted:
    event_id = await request.app.state.pool.fetchval(
        "INSERT INTO events (event_type, payload) VALUES ($1, $2::jsonb) RETURNING id",
        event.event_type,
        json.dumps(event.payload),
    )
    return EventAccepted(id=event_id)


@router.get("/events")
async def list_events(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    filter: Literal["all", "problems"] = "all",  # noqa: A002 - the query parameter's name
) -> list[EventOut]:
    rows = await request.app.state.pool.fetch(
        f"SELECT {EVENT_COLUMNS} FROM events WHERE {FILTERS[filter]} "  # noqa: S608 - fixed SQL
        "ORDER BY id DESC LIMIT $1",
        limit,
    )
    return [EventOut(**dict(row)) for row in rows]


@router.get("/events/{event_id}")
async def get_event(event_id: int, request: Request) -> EventOut:
    row = await request.app.state.pool.fetchrow(
        f"SELECT {EVENT_COLUMNS} FROM events WHERE id = $1",  # noqa: S608 - fixed SQL
        event_id,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such event")
    return EventOut(**dict(row))


# --- Scenario control


def simulation_enabled() -> bool:
    """Set SIMULATION_ENABLED=false to make the scenario read-only."""
    return os.environ.get("SIMULATION_ENABLED", "true").lower() != "false"


class ScenarioOut(BaseModel):
    profile: str
    label: str
    rate_per_second: float
    changed_at: datetime
    switchable: bool


class ScenarioIn(BaseModel):
    profile: scenarios.ProfileName


def scenario_out(row: Any) -> ScenarioOut:
    profile = scenarios.PROFILES.get(row["profile"], scenarios.PROFILES["healthy"])
    return ScenarioOut(
        profile=row["profile"],
        label=profile.label,
        rate_per_second=profile.rate_per_second,
        changed_at=row["changed_at"],
        switchable=simulation_enabled(),
    )


@router.get("/scenario")
async def get_scenario(request: Request) -> ScenarioOut:
    row = await request.app.state.pool.fetchrow("SELECT profile, changed_at FROM scenario")
    return scenario_out(row)


@router.put("/scenario")
async def set_scenario(body: ScenarioIn, request: Request) -> ScenarioOut:
    if not simulation_enabled():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "scenario switching is disabled")
    row = await request.app.state.pool.fetchrow(
        "UPDATE scenario SET profile = $1, changed_at = now() RETURNING profile, changed_at",
        body.profile,
    )
    return scenario_out(row)


# --- Console summary


class TypeSummary(BaseModel):
    event_type: str
    throughput_per_second: float
    error_rate: float
    p95_ms: float | None
    slo_ms: float


class QueueSummary(BaseModel):
    pending: int
    retrying: int
    failed: int
    lag_seconds: float


class AlertOut(BaseModel):
    rule: str
    key: str
    severity: str
    summary: str
    fired_at: datetime
    resolved_at: datetime | None


class Summary(BaseModel):
    at: datetime
    window_seconds: int
    scenario: ScenarioOut
    throughput_per_second: float  # delivery attempts that succeeded
    error_rate: float  # failed attempts / attempts
    p95_ms: float | None
    queue: QueueSummary
    types: list[TypeSummary]
    alerts: list[AlertOut]  # firing first, then resolved in the last 10 minutes


ALERTS_SQL = """
    SELECT rule, key, severity, summary, fired_at, resolved_at FROM alerts
    WHERE fired_at IS NOT NULL
      AND (resolved_at IS NULL OR resolved_at > now() - interval '10 minutes')
    ORDER BY resolved_at IS NOT NULL, fired_at DESC
    LIMIT 20
"""


@router.get("/summary")
async def summary(request: Request) -> Summary:
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        now = await conn.fetchval("SELECT now()")
        s = await alerts.read_signals(conn, now)
        scenario = await conn.fetchrow("SELECT profile, changed_at FROM scenario")
        firing = await conn.fetch(ALERTS_SQL)
    window = s.window_seconds
    return Summary(
        at=s.at,
        window_seconds=window,
        scenario=scenario_out(scenario),
        throughput_per_second=round(s.attempts * (1 - s.error_rate) / window, 2),
        error_rate=s.error_rate,
        p95_ms=s.p95_ms,
        queue=QueueSummary(
            pending=s.pending, retrying=s.retrying, failed=s.failed, lag_seconds=s.lag_seconds
        ),
        types=[
            TypeSummary(
                event_type=t.event_type,
                throughput_per_second=round(t.attempts * (1 - t.error_rate) / window, 2),
                error_rate=t.error_rate,
                p95_ms=t.p95_ms,
                slo_ms=t.slo_ms,
            )
            for t in s.types
        ],
        alerts=[AlertOut(**dict(r)) for r in firing],
    )
