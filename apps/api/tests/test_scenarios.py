import asyncio
import random

import httpx
import pytest

from app import scenarios, simulator

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip simulated latency. Patches asyncio.sleep globally, so only for delivery tests."""

    async def instant(_seconds: float) -> None:
        pass

    monkeypatch.setattr(scenarios.asyncio, "sleep", instant)


async def failure_rate(profile: str, event_type: str, n: int = 2000) -> float:
    failed = 0
    for event_id in range(n):
        try:
            await scenarios.deliver(profile, event_type, event_id, 1)
        except scenarios.DeliveryError:
            failed += 1
    return failed / n


def test_traffic_shares_add_up() -> None:
    assert sum(k.share for k in scenarios.EVENT_TYPES.values()) == pytest.approx(1)


def test_every_profile_covers_every_event_type() -> None:
    for profile in scenarios.PROFILES.values():
        assert set(profile.behaviour) == set(scenarios.EVENT_TYPES)


@pytest.mark.usefixtures("no_sleep")
async def test_healthy_is_above_99_5_percent() -> None:
    for event_type in scenarios.EVENT_TYPES:
        assert await failure_rate("healthy", event_type) < 0.005


@pytest.mark.usefixtures("no_sleep")
async def test_degradation_hits_only_the_partner_webhook() -> None:
    assert 0.25 <= await failure_rate("downstream_degradation", "webhook.partner_notify") <= 0.35
    assert await failure_rate("downstream_degradation", "payment.authorized") < 0.005


@pytest.mark.usefixtures("no_sleep")
async def test_outcomes_are_deterministic() -> None:
    async def failed(event_id: int) -> bool:
        try:
            await scenarios.deliver("retry_storm_backlog", "webhook.partner_notify", event_id, 1)
        except scenarios.DeliveryError:
            return True
        return False

    first = [await failed(i) for i in range(50)]
    assert first == [await failed(i) for i in range(50)]


@pytest.mark.usefixtures("no_sleep")
async def test_unknown_names_fall_back_to_healthy() -> None:
    await scenarios.deliver("no-such-profile", "no.such.type", 1, 1)


def test_storm_is_slower_than_two_workers_can_drain() -> None:
    """The backlog demo depends on arrivals outpacing two workers."""
    storm = scenarios.PROFILES["retry_storm_backlog"]
    mean_s = sum(
        scenarios.EVENT_TYPES[t].share * sum(b.latency_ms) / 2 / 1000
        for t, b in storm.behaviour.items()
    )
    assert storm.rate_per_second > 2 / mean_s
    healthy = scenarios.PROFILES["healthy"]
    mean_s = sum(
        scenarios.EVENT_TYPES[t].share * sum(b.latency_ms) / 2 / 1000
        for t, b in healthy.behaviour.items()
    )
    assert healthy.rate_per_second < 0.75 * 2 / mean_s


def test_generated_events_are_valid_api_input() -> None:
    from app.events import EventIn

    rng = random.Random(1)
    seen = set()
    for _ in range(200):
        event = EventIn(**simulator.next_event(rng))
        assert event.payload["id"]
        seen.add(event.event_type)
    assert seen == set(scenarios.EVENT_TYPES)


def api(scenario: dict, pending: int, posted: list[dict]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/scenario":
            return httpx.Response(200, json=scenario)
        if request.url.path == "/api/summary":
            return httpx.Response(200, json={"queue": {"pending": pending}})
        posted.append(request.read())
        return httpx.Response(202, json={"id": len(posted)})

    return httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(handler))


async def test_simulator_follows_the_scenario_rate() -> None:
    posted: list[dict] = []
    async with api({"profile": "recover", "rate_per_second": 200}, 0, posted) as client:
        sim = simulator.Simulator(client, random.Random(1))
        stop = asyncio.Event()
        task = asyncio.create_task(sim.run(stop))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, 1)
    assert sim.rate == 200
    assert 10 <= len(posted) <= 60  # about 40 at 200/s with real (unpatched) waits


async def test_simulator_pauses_when_the_queue_is_full() -> None:
    posted: list[dict] = []
    async with api({"profile": "healthy", "rate_per_second": 10}, 5000, posted) as client:
        sim = simulator.Simulator(client)
        await sim.refresh()
        assert sim.shedding


async def test_simulator_keeps_its_rate_when_the_api_is_down() -> None:
    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(down)) as c:
        sim = simulator.Simulator(c)
        await sim.refresh()
        await sim.send_one()  # logged, not raised
    assert sim.rate == scenarios.PROFILES["healthy"].rate_per_second
