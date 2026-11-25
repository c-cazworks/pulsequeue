import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import health, main


class FakePool:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.delay = 0.0
        self.queries = 0
        self.closed = False

    async def fetchval(self, query: str) -> int:
        self.queries += 1
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return 1

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> FakePool:
    fake = FakePool()

    async def fake_create_pool() -> FakePool:
        return fake

    monkeypatch.setattr(main, "create_pool", fake_create_pool)
    return fake


@pytest.fixture
def client(pool: FakePool) -> Iterator[TestClient]:
    with TestClient(main.app) as test_client:
        yield test_client


def test_healthz_does_not_touch_database(client: TestClient, pool: FakePool) -> None:
    pool.error = ConnectionRefusedError()
    assert client.get("/healthz").status_code == 200
    assert pool.queries == 0


def test_readyz_ok(client: TestClient) -> None:
    res = client.get("/readyz")
    assert res.status_code == 200
    assert res.json() == {"status": "ready", "database": "ok"}


def test_readyz_503_hides_error_details(client: TestClient, pool: FakePool) -> None:
    pool.error = OSError("connect failed: db.internal")
    res = client.get("/readyz")
    assert res.status_code == 503
    assert "db.internal" not in res.text


def test_readyz_503_when_database_hangs(
    client: TestClient, pool: FakePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "READY_TIMEOUT_SECONDS", 0.05)
    pool.delay = 5
    assert client.get("/readyz").status_code == 503


def test_readyz_recovers(client: TestClient, pool: FakePool) -> None:
    pool.error = OSError()
    assert client.get("/readyz").status_code == 503
    pool.error = None
    assert client.get("/readyz").status_code == 200


def test_pool_closed_on_shutdown(pool: FakePool) -> None:
    with TestClient(main.app):
        assert not pool.closed
    assert pool.closed
