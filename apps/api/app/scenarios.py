"""The domain story and the demo scenarios, shared by the API, worker and simulator.

PulseQueue dispatches a commerce platform's async work: payment authorizations, invoices,
inventory reservations and partner webhooks. The worker's delivery step is simulated, and the
active scenario decides how slow it is and how often each event type fails.

Randomness is seeded by event ID and attempt, so a scenario always fails the same events.
"""

import asyncio
import random
from dataclasses import dataclass, field
from typing import Literal

EventType = Literal[
    "payment.authorized", "invoice.generated", "webhook.partner_notify", "inventory.reserve"
]
ProfileName = Literal["healthy", "downstream_degradation", "retry_storm_backlog", "recover"]


@dataclass(frozen=True)
class EventKind:
    share: float  # fraction of generated traffic
    slo_ms: float  # p95 processing-latency objective for one attempt
    description: str


EVENT_TYPES: dict[EventType, EventKind] = {
    "payment.authorized": EventKind(0.40, 250, "Card authorization; must be fast, rarely retried"),
    "invoice.generated": EventKind(0.20, 500, "Invoice PDF and ledger entry"),
    "webhook.partner_notify": EventKind(
        0.25, 1000, "Notify a partner's endpoint; timeouts are retried with backoff"
    ),
    "inventory.reserve": EventKind(0.15, 300, "Hold stock for an order"),
}


@dataclass(frozen=True)
class Behaviour:
    latency_ms: tuple[float, float]  # uniform range for one attempt
    failure_rate: float
    error: str = "downstream returned 503"


HEALTHY: dict[EventType, Behaviour] = {
    "payment.authorized": Behaviour((40, 120), 0.0005),
    "invoice.generated": Behaviour((60, 150), 0.0005),
    "webhook.partner_notify": Behaviour((80, 200), 0.002, "partner endpoint returned 502"),
    "inventory.reserve": Behaviour((30, 90), 0.0005),
}


@dataclass(frozen=True)
class Profile:
    label: str  # the UI's badge
    rate_per_second: float  # simulator traffic
    behaviour: dict[EventType, Behaviour] = field(default_factory=lambda: HEALTHY)


# Two workers drain about 18 events/s when healthy, so healthy traffic keeps the queue flat.
PROFILES: dict[str, Profile] = {
    "healthy": Profile("HEALTHY", 10),
    # One partner is failing; everything else is unaffected. Retries add load, but the workers
    # keep up: partial failure, not collapse.
    "downstream_degradation": Profile(
        "PARTIAL DEGRADATION",
        10,
        HEALTHY
        | {
            "webhook.partner_notify": Behaviour(
                (150, 400), 0.30, "partner endpoint returned 503 (upstream timeout)"
            )
        },
    ),
    # Every dependency slows down and webhooks fail half the time: work arrives faster than
    # two workers can drain it, so the queue lag climbs until workers are added or load drops.
    "retry_storm_backlog": Profile(
        "BACKLOG / SLO BREACH",
        12,
        {
            "payment.authorized": Behaviour((300, 500), 0.002, "card network timed out"),
            "invoice.generated": Behaviour((400, 700), 0.002, "ledger write timed out"),
            "webhook.partner_notify": Behaviour(
                (400, 900), 0.50, "partner endpoint returned 503 (upstream timeout)"
            ),
            "inventory.reserve": Behaviour((300, 500), 0.002, "inventory service timed out"),
        },
    ),
    # Faults cleared and traffic reduced, so the backlog drains. The alert evaluator switches
    # back to healthy once the queue has caught up.
    "recover": Profile("RECOVERING", 4),
}

DEFAULT_PROFILE: ProfileName = "healthy"


class DeliveryError(Exception):
    """A simulated downstream failure; the worker retries it with backoff."""


def behaviour_for(profile: str, event_type: str) -> Behaviour:
    """Unknown names fall back to healthy payment behaviour rather than failing the event."""
    behaviours: dict[str, Behaviour] = PROFILES.get(profile, PROFILES[DEFAULT_PROFILE]).behaviour
    return behaviours.get(event_type, HEALTHY["payment.authorized"])


async def deliver(profile: str, event_type: str, event_id: int, attempt: int) -> None:
    """Simulate the downstream call for one attempt."""
    behaviour = behaviour_for(profile, event_type)
    rng = random.Random(f"{event_id}:{attempt}")  # noqa: S311 - simulation, not security
    await asyncio.sleep(rng.uniform(*behaviour.latency_ms) / 1000)
    if rng.random() < behaviour.failure_rate:
        raise DeliveryError(behaviour.error)
