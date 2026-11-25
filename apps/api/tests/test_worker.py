import asyncio
import os
import time
from pathlib import Path

import pytest

from app import heartbeat, worker

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def heartbeat_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    file = tmp_path / "heartbeat"
    monkeypatch.setenv("WORKER_HEARTBEAT_FILE", str(file))
    return file


@pytest.mark.parametrize(
    ("value", "threshold", "breached"),
    [(5.0, None, False), (5.0, 10.0, False), (10.0, 10.0, False), (10.1, 10.0, True)],
)
async def test_evaluate(value: float, threshold: float | None, breached: bool) -> None:
    assert await worker.evaluate({"value": value, "threshold": threshold}) is breached


async def test_stop_lets_in_flight_event_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    finished = 0

    async def slow_process(_pool: object) -> bool:
        nonlocal finished
        started.set()
        await release.wait()
        finished += 1
        return True

    monkeypatch.setattr(worker, "process_one", slow_process)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(None, stop))
    await started.wait()
    stop.set()
    await asyncio.sleep(0.02)
    assert not task.done()
    release.set()
    await asyncio.wait_for(task, 1)
    assert finished == 1


async def test_drain_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()

    async def hang(_pool: object) -> bool:
        started.set()
        await asyncio.sleep(60)
        return True

    monkeypatch.setattr(worker, "process_one", hang)
    monkeypatch.setattr(worker, "ITERATION_TIMEOUT_SECONDS", 0.05)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(None, stop))
    await started.wait()
    stop.set()
    await asyncio.wait_for(task, 1)


async def test_idle_worker_stops_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    async def empty(_pool: object) -> bool:
        return False

    monkeypatch.setattr(worker, "process_one", empty)
    monkeypatch.setattr(worker, "POLL_SECONDS", 60)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(None, stop))
    await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, 1)


async def test_database_outage_keeps_worker_alive(
    monkeypatch: pytest.MonkeyPatch, heartbeat_file: Path
) -> None:
    stop = asyncio.Event()
    calls = 0

    async def flaky(_pool: object) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionRefusedError
        stop.set()
        return True

    monkeypatch.setattr(worker, "process_one", flaky)
    monkeypatch.setattr(worker, "ERROR_DELAY_SECONDS", 0.01)
    await asyncio.wait_for(worker.run(None, stop), 1)
    assert calls == 2
    assert heartbeat.is_fresh()


async def test_heartbeat_goes_stale(heartbeat_file: Path) -> None:
    assert not heartbeat.is_fresh()
    heartbeat.touch()
    assert heartbeat.is_fresh()
    old = time.time() - heartbeat.MAX_AGE_SECONDS - 1
    os.utime(heartbeat_file, (old, old))
    assert not heartbeat.is_fresh()
