import asyncio
import json
import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app import main, worker
from app.observability import JsonFormatter


class FakePool:
    error: Exception | None = None

    async def fetchval(self, *_args: object) -> int:
        if self.error:
            raise self.error
        return 1

    async def fetch(self, *_args: object) -> list[object]:
        return []

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
    with TestClient(main.app, raise_server_exceptions=False) as test_client:
        yield test_client


def requests(route: str, status: str, method: str = "GET") -> float:
    labels = {"method": method, "route": route, "status": status}
    return REGISTRY.get_sample_value("pulsequeue_http_requests_total", labels) or 0.0


def test_counts_requests_by_route_template(client: TestClient) -> None:
    before = requests("/api/events", "200")
    client.get("/api/events?limit=5")
    client.get("/api/events?limit=6")
    assert requests("/api/events", "200") == before + 2


def test_database_outage_counts_as_503(client: TestClient, pool: FakePool) -> None:
    pool.error = ConnectionRefusedError()
    before = requests("/api/events", "201", "POST") + requests("/api/events", "503", "POST")
    client.post("/api/events", json={"source": "s", "metric": "m", "value": 1})
    assert requests("/api/events", "503", "POST") == before + 1


def test_unknown_paths_share_one_label(client: TestClient) -> None:
    before = requests("unmatched", "404")
    client.get("/nope/1")
    client.get("/nope/2")
    assert requests("unmatched", "404") == before + 2


def test_probes_are_not_counted_as_traffic(client: TestClient, pool: FakePool) -> None:
    pool.error = OSError()
    client.get("/readyz")  # a 503, which must not look like a user-facing error
    client.get("/healthz")
    client.get("/metrics")
    routes = {
        s.labels["route"]
        for m in REGISTRY.collect()
        if m.name == "pulsequeue_http_requests"
        for s in m.samples
    }
    assert routes.isdisjoint({"/readyz", "/healthz", "/metrics"})


def test_metrics_endpoint_serves_prometheus_text(client: TestClient) -> None:
    res = client.get("/metrics")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    assert "pulsequeue_http_request_duration_seconds_bucket" in res.text


def test_json_log_includes_extra_fields() -> None:
    record = logging.makeLogRecord(
        {"name": "t", "levelname": "INFO", "msg": "hi %s", "args": ("there",), "route": "/x"}
    )
    entry = json.loads(JsonFormatter().format(record))
    assert entry["message"] == "hi there"
    assert entry["route"] == "/x"
    assert entry["level"] == "info"


def test_json_log_includes_exception() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.getLogger("t").makeRecord(
            "t", logging.ERROR, __file__, 1, "failed", (), exc_info=__import__("sys").exc_info()
        )
    assert "ValueError: boom" in json.loads(JsonFormatter().format(record))["exception"]


class StatsPool:
    """Serves rows in order, then stops the loop once only one is left."""

    def __init__(self, rows: list[object], stop: asyncio.Event) -> None:
        self.rows, self.stop = rows, stop

    async def fetchrow(self, _query: str) -> object:
        row = self.rows.pop(0)
        if len(self.rows) == 1:
            self.stop.set()
        if isinstance(row, Exception):
            raise row
        return row


def gauge(name: str) -> float | None:
    return REGISTRY.get_sample_value(name)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_queue_stats_survive_a_failed_sample(
    anyio_backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "QUEUE_STATS_SECONDS", 0.01)
    stop = asyncio.Event()
    good = {"pending": 3, "failed": 1, "oldest_age": 42.5}
    pool = StatsPool([ConnectionRefusedError(), good, good], stop)
    await asyncio.wait_for(worker.report_queue(pool, stop), 1)  # type: ignore[arg-type]
    assert gauge("pulsequeue_queue_pending") == 3
    assert gauge("pulsequeue_queue_failed") == 1
    assert gauge("pulsequeue_queue_oldest_pending_age_seconds") == 42.5


def test_json_log_drops_uvicorn_color_message() -> None:
    record = logging.makeLogRecord({"msg": "started", "color_message": "[36mstarted[0m"})
    assert "color_message" not in json.loads(JsonFormatter().format(record))
