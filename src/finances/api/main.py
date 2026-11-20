"""The application: one FastAPI app, server-rendered, no build step.

Served by uvicorn as a single process behind `max_replicas = 1`. There is no SPA
and no second container — market-agent's two-image split exists because its
dashboard is a real React build, and nothing here needs one.

**`/healthz` and `/readyz` are different on purpose.** Container Apps uses the
liveness probe to decide whether to restart the container. If that probe touched
storage, a blob blip would restart every replica and turn a brief outage into a
crash loop. So `/healthz` answers from memory alone, and `/readyz` is the one
that reaches the document.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from appkit import headers, health
from appkit.integrity import IntegrityError
from fastapi import Depends, FastAPI, status
from fastapi.responses import JSONResponse

from ..settings import secret, settings
from . import deps, routes

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Resolve the passcode once when the process starts.

    `secret` is cached after this call, so a changed Key Vault value takes
    effect on the next container start rather than on an arbitrary first
    request. Storage remains lazy: a brief blob outage should fail a request,
    not prevent the app starting.
    """
    if settings().require_passcode:
        secret("APP-PASSCODE")
    yield


def _ready() -> None:
    """Readiness: the cheapest read that proves storage is reachable."""
    from .. import store

    store.load()


def create_app() -> FastAPI:
    cfg = settings()
    app = FastAPI(
        title="finances",
        description="Household savings pots, commitments, renewals and wealth.",
        lifespan=lifespan,
        # No interactive docs: this serves HTML to one household, and an OpenAPI
        # page is surface area behind the same passcode for no benefit.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    headers.install(app)

    # /healthz from memory (reporting IMAGE_TAG as `version` for the smoke
    # test), /readyz through a real read of storage: see the module docstring.
    health.install(app, environment=cfg.environment, ready=_ready)

    # Login is the one route outside the gate, for obvious reasons.
    app.include_router(routes.public)
    app.include_router(routes.router, dependencies=[Depends(deps.require_session)])

    @app.exception_handler(routes.ConflictResponse)
    async def _conflict(_request: Any, exc: routes.ConflictResponse) -> JSONResponse:
        """A write that lost a race, after the one retry in `store.update`.

        Surfaced rather than swallowed: whoever typed it needs to know it was
        not recorded, because they are the only one who can re-enter it.
        """
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc)},
        )

    @app.exception_handler(routes.InvalidInput)
    async def _invalid(_request: Any, exc: routes.InvalidInput) -> JSONResponse:
        """A form value refused before anything was written."""
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.exception_handler(IntegrityError)
    async def _broken(_request: Any, exc: IntegrityError) -> JSONResponse:
        """A change `store.save` refused because the document would break one
        of its rules: a route bug the route-level checks missed. Nothing was
        written. The detail names ids and indexes, never amounts."""
        logger.error("write refused: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": f"Not saved: {exc}"},
        )

    return app


app = create_app()
