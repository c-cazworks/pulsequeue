import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.db import close_pool, create_pool
from app.events import router as events_router
from app.health import router as health_router

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


@app.exception_handler(OSError)  # includes TimeoutError and ConnectionError
@app.exception_handler(asyncpg.PostgresConnectionError)
@app.exception_handler(asyncpg.CannotConnectNowError)
async def database_unavailable(request: Request, exc: Exception) -> JSONResponse:
    logger.warning("%s %s: database unavailable: %r", request.method, request.url.path, exc)
    return JSONResponse({"detail": "database unavailable"}, status_code=503)
