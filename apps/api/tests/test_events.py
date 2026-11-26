from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import main


class FakePool:
    error: Exception | None = None

    async def fetchval(self, *_args: object) -> int:
        if self.error:
            raise self.error
        return 42

    async def close(self) -> None:
        pass


@pytest.fixture
def pool() -> FakePool:
    return FakePool()


@pytest.fixture
def client(pool: FakePool, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    async def fake_create_pool() -> FakePool:
        return pool

    monkeypatch.setattr(main, "create_pool", fake_create_pool)
    with TestClient(main.app) as test_client:
        yield test_client


EVENT = {"event_type": "payment.authorized", "payload": {"id": "pay_1", "amount_cents": 1200}}


def test_create_event_is_accepted(client: TestClient) -> None:
    res = client.post("/api/events", json=EVENT)
    assert res.status_code == 202
    assert res.json() == {"id": 42}


@pytest.mark.parametrize(
    "change",
    [
        {"event_type": "payment.refunded"},
        {"event_type": ""},
        {"payload": ["not", "an", "object"]},
        {"payload": {"blob": "x" * 5000}},
    ],
)
def test_invalid_event_is_rejected(client: TestClient, change: dict) -> None:
    assert client.post("/api/events", json=EVENT | change).status_code == 422


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "filter=everything"])
def test_list_query_is_validated(client: TestClient, query: str) -> None:
    assert client.get(f"/api/events?{query}").status_code == 422


def test_unknown_scenario_is_rejected(client: TestClient) -> None:
    assert client.put("/api/scenario", json={"profile": "meltdown"}).status_code == 422


def test_scenario_switching_can_be_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIMULATION_ENABLED", "false")
    assert client.put("/api/scenario", json={"profile": "healthy"}).status_code == 403


@pytest.mark.parametrize("error", [ConnectionRefusedError("db.internal"), TimeoutError()])
def test_database_outage_returns_503(client: TestClient, pool: FakePool, error: Exception) -> None:
    pool.error = error
    res = client.post("/api/events", json=EVENT)
    assert res.status_code == 503
    assert "db.internal" not in res.text
