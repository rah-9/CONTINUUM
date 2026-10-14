"""Minimal localhost HTTP API (R10). Loopback only; no production auth in v1.

POST /runs                                     start a pre-registered task
GET  /runs/{run_id}                            status
GET  /runs/{run_id}/trace                      read-only JSON trace (extension)
POST /runs/{run_id}/resume                     resume under lease/ledger safeguards
POST /runs/{run_id}/actions/{action_id}/resolve  record a human decision
"""

from __future__ import annotations

import ipaddress
from dataclasses import asdict
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from continuum.core.errors import (
    AlreadyOwnedError,
    ContinuumError,
    InvalidTransitionError,
    NotFoundError,
    PacketTooLargeError,
    SpecValidationError,
    UnsupportedCapabilityError,
)
from continuum.core.runtime import RunResult
from continuum.interfaces.sdk import Continuum

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class StartRequest(BaseModel):
    task_id: str
    model_alias: str | None = None


class ResumeRequest(BaseModel):
    model_alias: str | None = None


class ResolveRequest(BaseModel):
    decision: Literal["confirmed", "abort", "retry_at_risk", "approve", "reject"]
    actor: str
    reason: str
    evidence_ref: str | None = None


def _result(r: RunResult) -> dict[str, Any]:
    data = asdict(r)
    data["status"] = r.status.value
    return data


def _hostname(host_header: str | None) -> str | None:
    """Host header -> hostname without port ('[::1]:8765' -> '::1')."""
    if not host_header:
        return None
    return urlsplit(f"//{host_header}").hostname


def _is_loopback(host: str | None) -> bool:
    if host is None:
        return False
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_app(sdk: Continuum) -> FastAPI:
    app = FastAPI(title="Continuum (local)", version="0.6.3")

    @app.middleware("http")
    async def loopback_only(request: Request, call_next: Any) -> Any:
        client = request.client.host if request.client else None
        if not _is_loopback(client):
            return JSONResponse({"detail": "loopback access only"}, status_code=403)
        # DNS rebinding: a web page whose domain resolves to 127.0.0.1 reaches us from a
        # loopback client, so the Host (and any browser Origin) must name loopback too.
        if not _is_loopback(_hostname(request.headers.get("host"))):
            return JSONResponse({"detail": "invalid Host header"}, status_code=403)
        origin = request.headers.get("origin")
        if origin is not None and not _is_loopback(urlsplit(origin).hostname):
            return JSONResponse({"detail": "cross-origin requests are refused"},
                                status_code=403)
        return await call_next(request)

    @app.exception_handler(ContinuumError)
    async def errors(request: Request, exc: ContinuumError) -> JSONResponse:
        status = 500
        if isinstance(exc, NotFoundError):
            status = 404
        elif isinstance(exc, AlreadyOwnedError | InvalidTransitionError):
            status = 409
        elif isinstance(exc, SpecValidationError | UnsupportedCapabilityError
                        | PacketTooLargeError):
            status = 422
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=status)

    @app.post("/runs", status_code=201)
    def start(body: StartRequest) -> dict[str, Any]:
        return _result(sdk.start(body.task_id, model_alias=body.model_alias))

    @app.get("/runs/{run_id}")
    def status(run_id: str) -> dict[str, Any]:
        return sdk.status(run_id)

    @app.get("/runs/{run_id}/trace")
    def trace(run_id: str) -> list[dict[str, Any]]:
        return sdk.trace(run_id)

    @app.post("/runs/{run_id}/resume")
    def resume(run_id: str, body: ResumeRequest | None = None) -> dict[str, Any]:
        return _result(sdk.resume(run_id, model_alias=body.model_alias if body else None))

    @app.post("/runs/{run_id}/actions/{action_id}/resolve")
    def resolve(run_id: str, action_id: str, body: ResolveRequest) -> dict[str, Any]:
        if not body.actor.strip():
            raise HTTPException(422, "actor is required")
        return _result(sdk.resolve_action(run_id, action_id, body.decision, actor=body.actor,
                                          reason=body.reason, evidence_ref=body.evidence_ref))

    return app


def serve(sdk: Continuum, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    if host not in LOOPBACK_HOSTS:
        raise SpecValidationError(f"refusing to bind {host}: v1 HTTP is loopback-only")
    import uvicorn

    uvicorn.run(create_app(sdk), host=host, port=port, log_level="info")
