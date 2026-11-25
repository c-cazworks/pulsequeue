from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request, status
from pydantic import BaseModel, Field, FiniteFloat

router = APIRouter(prefix="/api", tags=["events"])

Name = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:/-]+$")]


class EventIn(BaseModel):
    source: Name
    metric: Name
    value: FiniteFloat
    threshold: FiniteFloat | None = None


class EventAccepted(BaseModel):
    id: int


class EventOut(BaseModel):
    id: int
    source: str
    metric: str
    value: float
    threshold: float | None
    status: Literal["pending", "processed", "failed"]
    breached: bool
    created_at: datetime


class QueueStatus(BaseModel):
    pending: int
    processed: int
    failed: int
    breached: int


@router.post("/events", status_code=status.HTTP_202_ACCEPTED)
async def create_event(event: EventIn, request: Request) -> EventAccepted:
    event_id = await request.app.state.pool.fetchval(
        "INSERT INTO events (source, metric, value, threshold) VALUES ($1, $2, $3, $4) "
        "RETURNING id",
        event.source,
        event.metric,
        event.value,
        event.threshold,
    )
    return EventAccepted(id=event_id)


@router.get("/events")
async def list_events(
    request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[EventOut]:
    rows = await request.app.state.pool.fetch(
        "SELECT id, source, metric, value, threshold, status, breached, created_at "
        "FROM events ORDER BY id DESC LIMIT $1",
        limit,
    )
    return [EventOut(**dict(row)) for row in rows]


@router.get("/status")
async def queue_status(request: Request) -> QueueStatus:
    row = await request.app.state.pool.fetchrow(
        "SELECT count(*) FILTER (WHERE status = 'pending') AS pending, "
        "count(*) FILTER (WHERE status = 'processed') AS processed, "
        "count(*) FILTER (WHERE status = 'failed') AS failed, "
        "count(*) FILTER (WHERE breached) AS breached FROM events"
    )
    return QueueStatus(**dict(row))
