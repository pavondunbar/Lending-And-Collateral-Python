"""
services/api-gateway/main.py
--------------------------------------------------------------
API Gateway -- sole internet-facing entry point

Responsibilities:
  - API-key authentication on every incoming request
  - Rate limiting per client key
  - Request routing to internal microservices (lending-engine,
    collateral-manager, margin-engine, liquidation-engine,
    price-oracle, signing-gateway) via httpx reverse proxy
  - Response pass-through with upstream error normalisation
  - Structured access logging to Kafka audit trail
  - Health aggregation across all downstream services

Trust boundary: this is the ONLY service with a port binding to
the host (docker-compose `ports:` mapping).  All other services
are on the internal Docker network only and cannot be reached
from outside.
"""

import logging
import os
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import sys
sys.path.insert(0, "/app/shared")

import kafka_client as kafka
from metrics import instrument_app
from events import AuditTrailEntry
from rbac import hash_key, check_permission
from models import ApiKey

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# --- Configuration --------------------------------------------------------

SERVICE = os.environ.get("SERVICE_NAME", "api-gateway")
GATEWAY_API_KEY = os.environ["GATEWAY_API_KEY"]

UPSTREAM = {
    "lending": os.environ.get(
        "LENDING_ENGINE_URL", "http://lending-engine:8001",
    ),
    "collateral": os.environ.get(
        "COLLATERAL_MANAGER_URL", "http://collateral-manager:8002",
    ),
    "margin": os.environ.get(
        "MARGIN_ENGINE_URL", "http://margin-engine:8003",
    ),
    "liquidation": os.environ.get(
        "LIQUIDATION_ENGINE_URL", "http://liquidation-engine:8004",
    ),
    "price": os.environ.get(
        "PRICE_ORACLE_URL", "http://price-oracle:8005",
    ),
    "signing": os.environ.get(
        "SIGNING_GATEWAY_URL", "http://signing-gateway:8007",
    ),
}

# --- Rate Limiter (in-memory, per API key) --------------------------------


class SimpleRateLimiter:
    """Sliding window rate limiter -- 1000 req per 60s window per key."""

    def __init__(
        self, window_secs: int = 60, max_requests: int = 1000,
    ):
        self.window = window_secs
        self.max_req = max_requests
        self._buckets: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window
        bucket = self._buckets[key]
        self._buckets[key] = [t for t in bucket if t > cutoff]
        if len(self._buckets[key]) >= self.max_req:
            return False
        self._buckets[key].append(now)
        return True


limiter = SimpleRateLimiter()

# RBAC: in-memory key cache {key_hash: (key_id, role)}
_api_key_cache: dict[str, tuple[str, str]] = {}

# --- HTTP Client ----------------------------------------------------------

_http_client: Optional[httpx.AsyncClient] = None


async def get_client() -> httpx.AsyncClient:
    return _http_client


# --- Auth Dependency ------------------------------------------------------


async def require_api_key(request: Request):
    key = (
        request.headers.get("X-API-Key")
        or request.query_params.get("api_key")
    )
    if not key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    if key == GATEWAY_API_KEY:
        request.state.actor_id = "admin:legacy"
        request.state.actor_role = "admin"
    else:
        key_hex = hash_key(key)
        entry = _api_key_cache.get(key_hex)
        if not entry:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        key_id, role = entry
        if not check_permission(role, request.method, str(request.url.path)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{role}' cannot {request.method} {request.url.path}",
            )
        request.state.actor_id = f"{role}:{key_id}"
        request.state.actor_role = role

    if not limiter.is_allowed(key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Max 1000 requests per 60 seconds.",
        )
    return key


# --- Proxy Helper ---------------------------------------------------------


async def _proxy(
    request: Request,
    upstream_base: str,
    path: str,
    client: httpx.AsyncClient,
) -> Response:
    """Proxy the incoming request to an upstream service."""
    url = f"{upstream_base}{path}"
    body = await request.body()
    params = dict(request.query_params)
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "x-api-key", "content-length")
    }
    headers["X-Request-ID"] = request.state.request_id
    headers["X-Trace-ID"] = request.state.request_id
    actor_id = getattr(request.state, "actor_id", "")
    if actor_id:
        headers["X-Actor-Id"] = actor_id
    actor_role = getattr(request.state, "actor_role", "")
    if actor_role:
        headers["X-Actor-Role"] = actor_role

    try:
        upstream_resp = await client.request(
            method=request.method,
            url=url,
            content=body,
            params=params,
            headers=headers,
            timeout=30.0,
        )
    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail=f"Upstream unavailable: {upstream_base}",
        )
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504, detail="Upstream timeout",
        )

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=dict(upstream_resp.headers),
        media_type=upstream_resp.headers.get(
            "content-type", "application/json",
        ),
    )


# --- Middleware -----------------------------------------------------------


async def audit_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    start = time.perf_counter()

    response = await call_next(request)

    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)

    try:
        kafka.publish_dict(
            "audit.trail",
            {
                "event_id": request_id,
                "actor_service": SERVICE,
                "action": f"{request.method} {request.url.path}",
                "entity_type": "http_request",
                "entity_id": request_id,
                "trace_id": request_id,
                "after_state": {
                    "status_code": response.status_code,
                    "elapsed_ms": elapsed_ms,
                    "path": str(request.url.path),
                    "method": request.method,
                },
                "ip_address": (
                    request.client.host if request.client else None
                ),
                "actor_id": getattr(request.state, "actor_id", ""),
                "event_time": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception:
        pass  # Never fail a request because of audit logging

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time-Ms"] = str(elapsed_ms)
    log.info(
        "%s %s -> %d  %.1fms  req=%s",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
        request_id,
    )
    return response


# --- App ------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    _http_client = httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=200, max_keepalive_connections=50,
        ),
        timeout=httpx.Timeout(30.0),
    )
    log.info("API Gateway started. Upstreams: %s", UPSTREAM)
    # Load API keys from DB or use default
    default_key_hash = hash_key("change-me-in-production")
    _api_key_cache[default_key_hash] = (
        "00000000-0000-0000-0000-000000000010", "admin",
    )
    viewer_key_hash = hash_key("viewer-demo-key")
    _api_key_cache[viewer_key_hash] = (
        "viewer-demo", "viewer",
    )
    log.info("RBAC: loaded %d API keys", len(_api_key_cache))
    yield
    await _http_client.aclose()
    log.info("API Gateway shut down.")


app = FastAPI(
    title="Institutional Lending & Collateral Management -- API Gateway",
    version="1.0.0",
    description=(
        "Unified gateway for institutional lending infrastructure. "
        "Provides loan origination, collateral management, margin "
        "monitoring, liquidation, and price oracle services."
    ),
    lifespan=lifespan,
)

app.middleware("http")(audit_middleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Health ---------------------------------------------------------------


@app.get("/health", tags=["Meta"])
async def health(
    client: httpx.AsyncClient = Depends(get_client),
):
    """Aggregate health check across all downstream services."""
    checks = {}
    for name, base in UPSTREAM.items():
        try:
            r = await client.get(f"{base}/health", timeout=3.0)
            checks[name] = (
                "ok" if r.status_code == 200
                else f"degraded ({r.status_code})"
            )
        except Exception as exc:
            checks[name] = f"unreachable: {exc}"

    all_ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 207,
        content={"gateway": "ok", "services": checks},
    )


# --- Lending Routes -------------------------------------------------------


@app.api_route(
    "/v1/loans/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
    tags=["Lending"],
    dependencies=[Depends(require_api_key)],
)
async def loans_proxy(
    path: str,
    request: Request,
    client: httpx.AsyncClient = Depends(get_client),
):
    """
    Proxy to the Lending Engine.
    Endpoints: /loans/originate  /loans/{ref}/repay
    /loans/{ref}/accrue-interest  /loans/accrue-all
    /loans/{ref}/close  /loans/{ref}  /loans
    """
    return await _proxy(
        request, UPSTREAM["lending"], f"/loans/{path}", client,
    )


@app.api_route(
    "/v1/loans",
    methods=["GET", "POST"],
    tags=["Lending"],
    dependencies=[Depends(require_api_key)],
)
async def loans_root_proxy(
    request: Request,
    client: httpx.AsyncClient = Depends(get_client),
):
    return await _proxy(
        request, UPSTREAM["lending"], "/loans", client,
    )


# --- Account Routes -------------------------------------------------------


@app.api_route(
    "/v1/accounts/{path:path}",
    methods=["GET", "POST"],
    tags=["Accounts"],
    dependencies=[Depends(require_api_key)],
)
async def accounts_proxy(
    path: str,
    request: Request,
    client: httpx.AsyncClient = Depends(get_client),
):
    return await _proxy(
        request, UPSTREAM["lending"], f"/accounts/{path}", client,
    )


@app.api_route(
    "/v1/accounts",
    methods=["POST"],
    tags=["Accounts"],
    dependencies=[Depends(require_api_key)],
)
async def accounts_root_proxy(
    request: Request,
    client: httpx.AsyncClient = Depends(get_client),
):
    return await _proxy(
        request, UPSTREAM["lending"], "/accounts", client,
    )


# --- Collateral Routes ----------------------------------------------------


@app.api_route(
    "/v1/collateral/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
    tags=["Collateral"],
    dependencies=[Depends(require_api_key)],
)
async def collateral_proxy(
    path: str,
    request: Request,
    client: httpx.AsyncClient = Depends(get_client),
):
    """
    Proxy to the Collateral Manager.
    Endpoints: /collateral/deposit  /collateral/withdraw
    /collateral/{ref}  /collateral/loan/{loan_ref}
    /collateral/valuation/{loan_ref}
    """
    return await _proxy(
        request,
        UPSTREAM["collateral"],
        f"/collateral/{path}",
        client,
    )


# --- Margin Routes --------------------------------------------------------


@app.api_route(
    "/v1/margin/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
    tags=["Margin"],
    dependencies=[Depends(require_api_key)],
)
async def margin_proxy(
    path: str,
    request: Request,
    client: httpx.AsyncClient = Depends(get_client),
):
    """
    Proxy to the Margin Engine.
    Endpoints: /margin/evaluate  /margin/evaluate-all
    /margin/status/{loan_ref}  /margin/calls
    """
    return await _proxy(
        request, UPSTREAM["margin"], f"/margin/{path}", client,
    )


# --- Liquidation Routes ---------------------------------------------------


@app.api_route(
    "/v1/liquidations/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
    tags=["Liquidation"],
    dependencies=[Depends(require_api_key)],
)
async def liquidations_proxy(
    path: str,
    request: Request,
    client: httpx.AsyncClient = Depends(get_client),
):
    """
    Proxy to the Liquidation Engine.
    Endpoints: /liquidations/initiate
    /liquidations/{ref}/execute  /liquidations
    """
    return await _proxy(
        request,
        UPSTREAM["liquidation"],
        f"/liquidations/{path}",
        client,
    )


@app.api_route(
    "/v1/liquidations",
    methods=["GET", "POST"],
    tags=["Liquidation"],
    dependencies=[Depends(require_api_key)],
)
async def liquidations_root_proxy(
    request: Request,
    client: httpx.AsyncClient = Depends(get_client),
):
    return await _proxy(
        request, UPSTREAM["liquidation"], "/liquidations", client,
    )


# --- Price Oracle Routes --------------------------------------------------


@app.api_route(
    "/v1/prices/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
    tags=["Prices"],
    dependencies=[Depends(require_api_key)],
)
async def prices_proxy(
    path: str,
    request: Request,
    client: httpx.AsyncClient = Depends(get_client),
):
    """
    Proxy to the Price Oracle.
    Endpoints: /prices/feed  /prices/{asset}  /prices
    """
    return await _proxy(
        request, UPSTREAM["price"], f"/prices/{path}", client,
    )


@app.api_route(
    "/v1/prices",
    methods=["GET", "POST"],
    tags=["Prices"],
    dependencies=[Depends(require_api_key)],
)
async def prices_root_proxy(
    request: Request,
    client: httpx.AsyncClient = Depends(get_client),
):
    return await _proxy(
        request, UPSTREAM["price"], "/prices", client,
    )


# --- Settlement / Blockchain Routes ----------------------------------------


@app.api_route(
    "/v1/settlements/{path:path}",
    methods=["GET"],
    tags=["Settlements"],
    dependencies=[Depends(require_api_key)],
)
async def settlements_proxy(
    path: str,
    request: Request,
    client: httpx.AsyncClient = Depends(get_client),
):
    """
    Proxy to the Lending Engine's settlement endpoints.
    Endpoints: /settlements  /settlements/{ref}
    /settlements/tx/{tx_hash}
    """
    return await _proxy(
        request,
        UPSTREAM["lending"],
        f"/settlements/{path}",
        client,
    )


@app.api_route(
    "/v1/settlements",
    methods=["GET"],
    tags=["Settlements"],
    dependencies=[Depends(require_api_key)],
)
async def settlements_root_proxy(
    request: Request,
    client: httpx.AsyncClient = Depends(get_client),
):
    return await _proxy(
        request, UPSTREAM["lending"], "/settlements", client,
    )


# --- API Reference --------------------------------------------------------


@app.get("/", tags=["Meta"])
def root():
    return {
        "name": "Institutional Lending & Collateral Management",
        "version": "1.0.0",
        "docs": "/docs",
        "endpoints": {
            "accounts": {
                "POST /v1/accounts": "Onboard institutional participant",
                "GET  /v1/accounts/{id}": "Get account details",
            },
            "lending": {
                "POST /v1/loans/originate": "Originate a loan",
                "POST /v1/loans/{ref}/repay": "Make repayment",
                "POST /v1/loans/{ref}/accrue-interest": "Accrue interest",
                "POST /v1/loans/accrue-all": "Batch interest accrual",
                "POST /v1/loans/{ref}/close": "Close repaid loan",
                "GET  /v1/loans/{ref}": "Get loan status",
                "GET  /v1/loans": "List loans",
            },
            "collateral": {
                "POST /v1/collateral/deposit": "Deposit collateral",
                "POST /v1/collateral/withdraw": "Withdraw collateral",
                "GET  /v1/collateral/{ref}": "Get collateral position",
                "GET  /v1/collateral/loan/{loan_ref}": (
                    "Collateral for loan"
                ),
                "GET  /v1/collateral/valuation/{loan_ref}": (
                    "Real-time valuation"
                ),
            },
            "margin": {
                "POST /v1/margin/evaluate": "Evaluate single loan LTV",
                "POST /v1/margin/evaluate-all": "Batch LTV evaluation",
                "GET  /v1/margin/status/{loan_ref}": "Current LTV status",
                "GET  /v1/margin/calls": "Active margin calls",
            },
            "liquidation": {
                "POST /v1/liquidations/initiate": "Start liquidation",
                "POST /v1/liquidations/{ref}/execute": (
                    "Execute collateral sale"
                ),
                "GET  /v1/liquidations": "List liquidation events",
            },
            "prices": {
                "POST /v1/prices/feed": "Ingest price feed",
                "GET  /v1/prices/{asset}": "Latest price",
                "GET  /v1/prices": "All latest prices",
            },
            "settlements": {
                "GET  /v1/settlements": (
                    "List all on-chain settlements"
                ),
                "GET  /v1/settlements/{ref}": (
                    "Get settlement by reference"
                ),
                "GET  /v1/settlements/tx/{tx_hash}": (
                    "Look up settlement by tx hash"
                ),
            },
        },
        "auth": "Pass X-API-Key header on all /v1/* requests",
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
