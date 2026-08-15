"""
Consilium Backend - main application entrypoint.
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import agents, chat, missions, reports, websocket
from app.db.database import init_db
from app.logger import setup_logging
from app.services.workflow_contracts import build_error, utc_now_iso

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logger = setup_logging(LOG_LEVEL)


def _json_error_response(
    status_code: int,
    *,
    code: str,
    message: str,
    retryable: bool,
    details: Optional[Dict[str, Any]] = None,
) -> JSONResponse:
    """Create a normalized JSON error response."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": build_error(
                code,
                message,
                retryable=retryable,
                details=details,
            ),
            "timestamp": utc_now_iso(),
        },
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    logger.info("Consilium Backend starting")
    init_db()
    # Job threads do not survive a restart, so anything still queued or running
    # belongs to work that is never coming back. Left alone, those rows are
    # permanently busy ghosts on the floor view.
    from app.services.job_service import reap_orphans
    from app.services.round_service import reap_orphans as reap_round_orphans
    reap_orphans()
    reap_round_orphans()
    logger.info("Docs: http://localhost:%s/docs", os.getenv("PORT", "8000"))
    logger.info("Health: http://localhost:%s/health", os.getenv("PORT", "8000"))
    # Report the provider actually in use. This was hardcoded to Groq and
    # mentioned a "demo mode" that stopped existing when the five-provider
    # registry landed, so switching to Anthropic still produced a line about
    # Groq -- exactly the kind of stale log that sends someone hunting a
    # config bug that is not there.
    from app.config import load_config
    from app.services.llm_client import UnifiedLLMClient
    from app.services.model_registry import available_providers, has_credential

    cfg = load_config()
    ready, reason = UnifiedLLMClient(cfg).is_ready()
    if ready:
        logger.info("Model provider: %s / %s", cfg.provider, cfg.model or "tier default")
    else:
        logger.warning("Model provider %s is not usable: %s", cfg.provider, reason)

    with_keys = [p for p in available_providers() if has_credential(p)]
    logger.info(
        "Providers with a credential: %s",
        ", ".join(with_keys) if with_keys else "none - set one in .env or Settings",
    )

    from app.services.langfuse_client import init_langfuse, shutdown_langfuse

    init_langfuse()
    yield
    shutdown_langfuse()
    logger.info("Consilium Backend shutting down")


app = FastAPI(
    title="Consilium API",
    description="Your invisible AI partner for SME growth",
    version="1.2.0",
    lifespan=lifespan,
)

# Must run here, immediately after construction -- NOT inside lifespan().
# Starlette.__call__ builds and *caches* app.middleware_stack on its very
# first invocation, and that first invocation is the ASGI lifespan-startup
# call itself (before lifespan()'s own body runs). Patching
# build_middleware_stack from inside lifespan() is therefore always too late:
# the stack is already built with the unpatched method by the time it runs,
# and FastAPI spans silently never appear (SQLAlchemy/httpx instrumentation
# patch their libraries directly and are unaffected by this -- only the
# FastAPI ASGI-middleware patch has this ordering requirement).
from app.services.telemetry import setup_telemetry  # noqa: E402
from app.db.database import engine as _db_engine  # noqa: E402

setup_telemetry(app, engine=_db_engine)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    elapsed = (time.time() - start) * 1000
    logger.info("%s %s -> %s (%.0fms)", request.method, request.url.path, response.status_code, elapsed)
    return response


def _serializable_issues(exc: RequestValidationError) -> list:
    """
    Pydantic puts the original exception object in `ctx` when a custom
    `field_validator` raises. That object is not JSON serializable, so
    rendering the 422 raised inside the error handler and the client got a
    500 -- a validation bug disguised as a server fault. Stringify anything
    the encoder cannot take.
    """
    issues = []
    for issue in exc.errors():
        clean = {k: v for k, v in issue.items() if k != "ctx"}
        ctx = issue.get("ctx")
        if isinstance(ctx, dict):
            clean["ctx"] = {k: str(v) for k, v in ctx.items()}
        issues.append(clean)
    return issues


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError):
    issues = _serializable_issues(exc)
    logger.warning("Validation error for %s: %s", request.url.path, issues)
    return _json_error_response(
        422,
        code="VALIDATION_ERROR",
        message="Request validation failed.",
        retryable=False,
        details={"issues": issues},
    )


@app.exception_handler(StarletteHTTPException)
async def handle_http_error(request: Request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
    logger.warning("HTTP %s for %s: %s", exc.status_code, request.url.path, detail)
    return _json_error_response(
        exc.status_code,
        code="HTTP_ERROR",
        message=detail,
        retryable=500 <= exc.status_code < 600,
        details={"status_code": exc.status_code},
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception):
    logger.exception("Unhandled error for %s", request.url.path)
    return _json_error_response(
        500,
        code="INTERNAL_SERVER_ERROR",
        message="Unexpected server error.",
        retryable=True,
        details={"exception_type": exc.__class__.__name__},
    )


from app.api import agents, chat, config_api, jobs, missions, org, reports, rounds, websocket

# ...
app.include_router(missions.router, prefix="/api/missions", tags=["missions"])
app.include_router(agents.router, prefix="/api/agents", tags=["agents"])
app.include_router(org.router, prefix="/api/org", tags=["org"])
app.include_router(jobs.router, prefix="/api/jobs", tags=["jobs"])
app.include_router(rounds.router, prefix="/api/rounds", tags=["rounds"])
app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
app.include_router(config_api.router, prefix="/api", tags=["config"])
app.include_router(reports.router, prefix="/api/reports", tags=["reports"])
app.include_router(websocket.router, prefix="/ws", tags=["websocket"])


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "consilium"}


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "message": "Welcome to Consilium",
        "docs": "/docs",
        "health": "/health",
    }
