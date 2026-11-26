"""Synthetic traffic: posts events to the API at the active scenario's rate.

Runs as its own single replica, so traffic stays the same when workers or the API scale. It goes
through the API rather than the database, so API metrics and the ALB see realistic traffic.
"""

import asyncio
import logging
import os
import random
import signal
import string
import time
from collections.abc import Callable
from typing import Any

import httpx

from app import heartbeat, scenarios
from app.observability import configure_logging

logger = logging.getLogger(__name__)

API_URL = os.environ.get("SIMULATOR_API_URL", "http://api:8000")
SCENARIO_REFRESH_SECONDS = 2.0
# Stops sending while this many events wait, so a long backlog demo cannot fill the disk.
MAX_PENDING = 5000
REQUEST_TIMEOUT_SECONDS = 5.0

PARTNERS = ["shipfast", "taxly", "crmhub", "loyalty-plus"]
CURRENCIES = ["USD", "EUR", "GBP", "CAD"]


def _ref(rng: random.Random, prefix: str) -> str:
    return prefix + "_" + "".join(rng.choices(string.ascii_lowercase + string.digits, k=10))


PAYLOADS: dict[str, Callable[[random.Random], dict[str, Any]]] = {
    "payment.authorized": lambda r: {
        "id": _ref(r, "pay"),
        "amount_cents": r.randint(500, 250_000),
        "currency": r.choice(CURRENCIES),
    },
    "invoice.generated": lambda r: {
        "id": _ref(r, "inv"),
        "customer": _ref(r, "cus"),
        "lines": r.randint(1, 12),
    },
    "webhook.partner_notify": lambda r: {
        "id": _ref(r, "whk"),
        "partner": r.choice(PARTNERS),
        "topic": r.choice(["order.shipped", "order.refunded", "customer.updated"]),
    },
    "inventory.reserve": lambda r: {
        "id": _ref(r, "res"),
        "sku": f"SKU-{r.randint(1000, 9999)}",
        "quantity": r.randint(1, 5),
    },
}


def next_event(rng: random.Random) -> dict[str, Any]:
    names = list(scenarios.EVENT_TYPES)
    weights = [k.share for k in scenarios.EVENT_TYPES.values()]
    event_type = rng.choices(names, weights)[0]
    return {"event_type": event_type, "payload": PAYLOADS[event_type](rng)}


class Simulator:
    def __init__(self, client: httpx.AsyncClient, rng: random.Random | None = None) -> None:
        self.client = client
        self.rng = rng or random.Random()  # noqa: S311 - simulation, not security
        self.rate = scenarios.PROFILES[scenarios.DEFAULT_PROFILE].rate_per_second
        self.shedding = False

    async def refresh(self) -> None:
        """Follow the scenario. If the API is unreachable, keep the last known rate."""
        try:
            scenario = (await self.client.get("/api/scenario")).raise_for_status().json()
            queue = (await self.client.get("/api/summary")).raise_for_status().json()["queue"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.warning("cannot read the scenario: %r", exc)
            return
        rate = scenario["rate_per_second"]
        if rate != self.rate:
            logger.info("scenario changed", extra={"profile": scenario["profile"], "rate": rate})
        self.rate = rate
        shedding = queue["pending"] >= MAX_PENDING
        if shedding != self.shedding:
            logger.warning(
                "pausing traffic" if shedding else "resuming traffic",
                extra={"pending": queue["pending"]},
            )
        self.shedding = shedding

    async def send_one(self) -> None:
        try:
            res = await self.client.post("/api/events", json=next_event(self.rng))
            res.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("send failed: %r", exc)

    async def run(self, stop: asyncio.Event) -> None:
        """Send at the current rate, spacing requests evenly."""
        refreshed = 0.0
        while not stop.is_set():
            heartbeat.touch()
            if time.monotonic() - refreshed >= SCENARIO_REFRESH_SECONDS:
                await self.refresh()
                refreshed = time.monotonic()
            if self.shedding or self.rate <= 0:
                delay = SCENARIO_REFRESH_SECONDS
            else:
                start = time.monotonic()
                await self.send_one()
                delay = max(0.0, 1 / self.rate - (time.monotonic() - start))
            try:
                async with asyncio.timeout(delay):
                    await stop.wait()
            except TimeoutError:
                pass


async def main() -> None:
    configure_logging()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    logger.info("simulator started", extra={"api": API_URL})
    async with httpx.AsyncClient(base_url=API_URL, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        await Simulator(client).run(stop)
    logger.info("simulator stopped")


if __name__ == "__main__":
    asyncio.run(main())
