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


EVENT = {"source": "web-1", "metric": "cpu.load", "value": 0.9, "threshold": 0.8}


def test_create_event_is_accepted(client: TestClient) -> None:
    res = client.post("/api/events", json=EVENT)
    assert res.status_code == 202
    assert res.json() == {"id": 42}


@pytest.mark.parametrize(
    "change", [{"source": ""}, {"source": "a\x00b"}, {"metric": "x" * 101}, {"value": "inf"}]
)
def test_invalid_event_is_rejected(client: TestClient, change: dict) -> None:
    assert client.post("/api/events", json=EVENT | change).status_code == 422


@pytest.mark.parametrize("limit", [0, 101])
def test_list_limit_is_bounded(client: TestClient, limit: int) -> None:
    assert client.get(f"/api/events?limit={limit}").status_code == 422


@pytest.mark.parametrize("error", [ConnectionRefusedError("db.internal"), TimeoutError()])
def test_database_outage_returns_503(client: TestClient, pool: FakePool, error: Exception) -> None:
    pool.error = error
    res = client.post("/api/events", json=EVENT)
    assert res.status_code == 503
    assert "db.internal" not in res.text
