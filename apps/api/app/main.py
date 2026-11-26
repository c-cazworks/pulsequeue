import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.db import close_pool, create_pool
from app.events import router as events_router
from app.health import router as health_router
from app.observability import HTTP_DURATION, HTTP_REQUESTS

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.pool = await create_pool()
    try:
        yield
    finally:
        # uvicorn has drained in-flight requests by now (SIGTERM -> stop accepting -> drain).
        await close_pool(app.state.pool)


app = FastAPI(title="PulseQueue API", lifespan=lifespan)
app.include_router(health_router)
app.include_router(events_router)

# Not user traffic: probes would turn a readiness failure into a fake 5xx spike.
UNOBSERVED_ROUTES = {"/healthz", "/readyz", "/metrics"}


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus scrape target. Not public: nginx proxies only /api/."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.middleware("http")
async def observe(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    start = time.perf_counter()
    status = 500  # if the handler raises, the client gets a 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        # The route template, not the raw path, keeps label cardinality bounded.
        route = getattr(request.scope.get("route"), "path", "unmatched")
        if route not in UNOBSERVED_ROUTES:
            elapsed = time.perf_counter() - start
            HTTP_REQUESTS.labels(request.method, route, str(status)).inc()
            HTTP_DURATION.labels(request.method, route).observe(elapsed)
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "route": route,
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 1),
                },
            )


@app.exception_handler(OSError)  # includes TimeoutError and ConnectionError
@app.exception_handler(asyncpg.PostgresConnectionError)
@app.exception_handler(asyncpg.CannotConnectNowError)
async def database_unavailable(request: Request, exc: Exception) -> JSONResponse:
    logger.warning("%s %s: database unavailable: %r", request.method, request.url.path, exc)
    return JSONResponse({"detail": "database unavailable"}, status_code=503)
