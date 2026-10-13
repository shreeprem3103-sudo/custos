"""FastAPI application factory.

Boot order is deliberate and mirrors how an EOC actually starts up:

1. logging (so everything after it is traceable),
2. storage — created if empty, seeded with the city if the twin is empty,
3. the twin cache warm,
4. the six-stage loop and its heartbeat publisher.

If any of those fail, the API still starts and reports itself degraded: a control
room needs the map and the last known state even when half the stack is down.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__, STAGES
from ..config import settings
from ..domain.schemas import HealthOut
from ..infrastructure import db
from ..observability import METRICS, configure_logging, get_logger, set_correlation_id
from ..realtime.bus import BUS, HeartbeatPublisher, Topics
from ..fusion import repository
from ..services import store
from ..services.pipeline import ORCHESTRATOR
from .deps import storage_mode
from .routers import ai as ai_router
from .routers import auth as auth_router
from .routers import incidents as incidents_router
from .routers import ops as ops_router
from .routers import simulations as simulations_router
from .routers import twin as twin_router
from . import ws as ws_router

log = get_logger("api.main")

#: how often the operating loop refreshes the twin when rainfall is significant
CYCLE_INTERVAL_S = 45.0
CYCLE_INTERVAL_QUIET_S = 180.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.log_level)
    log.info(
        "custos starting",
        extra={"custos": {"version": __version__, "environment": settings.environment, "city": settings.city_name, "stages": list(STAGES)}},
    )
    boot: dict[str, Any] = {}
    try:
        await db.init_db()
        boot["storage"] = {"created": True, "dialect": db.dialect_name()}
    except Exception as exc:  # noqa: BLE001 - degraded start is a feature
        log.error("storage unavailable at boot", extra={"custos": {"error": str(exc)[:200]}})
        boot["storage"] = {"created": False, "error": str(exc)[:200]}
    boot["seed"] = await ORCHESTRATOR.boot()
    boot["registry"] = {"connectors": [c["name"] for c in ORCHESTRATOR.status()["connectors"]]}

    # Events are mirrored to storage so an after-action review can replay the
    # night after a restart. The mirror is deliberately fire-and-forget: a
    # database hiccup slows the audit trail, never the flood response.
    BUS.persist = _persist_event

    heartbeat = HeartbeatPublisher(BUS, interval_s=15.0)
    heartbeat.set_snapshot_provider(lambda: ORCHESTRATOR.status())
    heartbeat.start()

    watchdog = asyncio.create_task(_run_loop())
    try:
        async with store.session_scope() as session:
            await store.write_audit(session, actor="system", role="system", action="boot", resource="custos", detail=boot)
    except Exception:  # noqa: BLE001
        pass
    log.info("custos ready", extra={"custos": boot})
    try:
        yield
    finally:
        watchdog.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog
        await heartbeat.stop()
        await db.dispose()
        log.info("custos stopped")


async def _persist_event(event: Any) -> None:
    """Mirror one bus event into ``events`` (best effort, never blocking)."""
    async with store.session_scope() as session:
        await repository.record_event(session, event.to_wire() if hasattr(event, "to_wire") else dict(event))


async def _run_loop() -> None:
    """The scheduled operating cycle.

    Cadence adapts to the weather: a quiet city is refreshed every three minutes,
    an active storm every 45 seconds. Burning a 48-member ensemble every 45 s
    regardless would make the console jitter and waste the CPU the routing needs.
    """
    while True:
        try:
            status_payload = ORCHESTRATOR.status()
            prediction = status_payload.get("prediction") or {}
            heavy = float(prediction.get("rainfall_mm_h") or 0.0) >= 12.0
            await ORCHESTRATOR.run_cycle(trigger="scheduler", force=heavy)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must survive its own errors
            log.error("scheduled cycle failed", extra={"custos": {"error": str(exc)[:200]}})
        await asyncio.sleep(CYCLE_INTERVAL_S if heavy else CYCLE_INTERVAL_QUIET_S)


def create_app() -> FastAPI:
    app = FastAPI(
        title="CUSTOS Crisis Digital Twin",
        description=(
            "Predictive resilience platform for urban flood response, Kochi. "
            "Collect → Fuse → Understand → Predict → Simulate → Recommend."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        contact={"name": "CUSTOS", "url": "https://example.invalid/custos"},
        license_info={"name": "Proprietary - emergency management"},
    )

    origins = [origin.strip() for origin in (settings.cors_origins or "").split(",") if origin.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"] if settings.environment == "development" else origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Correlation-ID", "X-Request-ID"],
        expose_headers=["X-Correlation-ID", "X-CUSTOS-Degraded", "X-CUSTOS-Twin-Digest", "X-CUSTOS-Snapshot-Age", "X-Process-Time"],
        max_age=600,
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.middleware("http")
    async def observability(request: Request, call_next):  # type: ignore[no-untyped-def]
        correlation_id = set_correlation_id(request.headers.get("X-Correlation-ID") or request.headers.get("X-Request-ID"))
        started = time.perf_counter()
        response: Response
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001
            METRICS.inc("http.error")
            log.error("unhandled request error", extra={"custos": {"path": request.url.path, "error": str(exc)[:200]}})
            response = JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"detail": "internal error", "correlation_id": correlation_id},
            )
        duration_ms = (time.perf_counter() - started) * 1000
        METRICS.observe("http.duration_ms", duration_ms)
        METRICS.inc(f"http.{response.status_code}")
        response.headers["X-Correlation-ID"] = correlation_id
        response.headers["X-Process-Time"] = f"{duration_ms:.1f}"
        response.headers["X-CUSTOS-Version"] = __version__
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Permissions-Policy"] = "geolocation=(self), camera=(), microphone=()"
        if settings.environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.method != "GET" and 200 <= response.status_code < 300 and request.url.path.startswith("/api/v1"):
            me = getattr(request.state, "identity_subject", None)
            if me:
                with contextlib.suppress(Exception):
                    async with store.session_scope() as session:
                        await store.write_audit(
                            session,
                            actor=str(me),
                            role=str(getattr(request.state, "identity_role", "unknown")),
                            action="commit" if request.method in {"POST", "PATCH", "PUT", "DELETE"} else "read",
                            resource=request.url.path,
                            detail={"method": request.method, "status": response.status_code},
                            correlation_id=correlation_id,
                        )
        return response

    from fastapi import APIRouter

    v1 = APIRouter(prefix="/api/v1")
    v1.include_router(auth_router.router)
    v1.include_router(twin_router.router)
    v1.include_router(incidents_router.router)
    v1.include_router(simulations_router.router)
    v1.include_router(ops_router.router)
    v1.include_router(ai_router.router)
    app.include_router(v1)
    app.include_router(ws_router.router, prefix="/api/v1")

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "path": str(request.url.path)},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Turn validation errors into something an operator can act on."""
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "detail": "request did not validate",
                "errors": [
                    {"field": ".".join(str(part) for part in error.get("loc", [])), "problem": error.get("msg"), "input": str(error.get("input"))[:200]}
                    for error in exc.errors()[:20]
                ],
                "path": str(request.url.path),
            },
        )

    @app.get("/", include_in_schema=False)
    async def root() -> Any:
        frontend = _frontend_dir()
        if frontend is not None:
            return RedirectResponse("/console/")
        return {
            "service": "CUSTOS Crisis Digital Twin",
            "version": __version__,
            "stages": list(STAGES),
            "docs": "/api/docs",
            "health": "/api/v1/health",
            "note": "the command console is not built into this deployment; see frontend/README.md",
        }

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # Serve the built console from the same origin when it exists: one box, one
    # port, no CORS in an air-gapped EOC, and no way for a stale CDN copy to serve
    # a console that disagrees with this API.
    frontend = _frontend_dir()
    if frontend is not None:
        app.mount("/console", StaticFiles(directory=str(frontend), html=True), name="console")
    else:
        @app.get("/console", include_in_schema=False)
        async def console_not_built() -> HTMLResponse:
            """Say *what to run*, rather than 404 into a blank page.

            A missing frontend build is a deployment mistake, and the person who
            made it is standing at the box: the response tells them the exact
            command instead of leaving them to guess from a stack trace.
            """
            return HTMLResponse(_CONSOLE_MISSING_HTML, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    return app


_CONSOLE_MISSING_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>CUSTOS console not built</title>
<style>
 body{background:#05080d;color:#e9eef6;font:14px/1.5 system-ui,sans-serif;display:grid;place-items:center;height:100vh;margin:0}
 main{max-width:640px;padding:32px;border:1px solid #1d2836;border-radius:12px;background:#0d141d}
 h1{letter-spacing:.3em;font-size:20px;margin:0 0 4px} p{color:#a7b6c9}
 code{background:#070c12;border:1px solid #27374a;border-radius:6px;padding:2px 6px;font-family:ui-monospace,monospace}
 li{margin:6px 0;color:#cbd5e1}
</style></head><body><main>
<h1>CUSTOS</h1>
<p>The command console is not built into this deployment. The API is running and
fully usable (<code>/api/docs</code>) — only the browser console is missing.</p>
<p><strong>Build it once, on this appliance:</strong></p>
<ol>
  <li><code>cd frontend</code></li>
  <li><code>npm install</code> (first time only; the appliance needs no internet afterwards)</li>
  <li><code>npm run build</code> → produces <code>frontend/out</code></li>
</ol>
<p>Then reload this page. On Windows, <code>scripts\start_custos.bat</code> does all of this for you.</p>
</main></body></html>
"""


def _frontend_dir() -> Path | None:
    candidates = [
        Path(__file__).resolve().parents[3] / "frontend" / "out",
        Path(__file__).resolve().parents[3] / "frontend" / "dist",
        Path.cwd() / "frontend" / "out",
        Path.cwd() / "frontend" / "dist",
    ]
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.iterdir()):
            return candidate
    return None


app = create_app()
