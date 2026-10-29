"""
Nautilus Trader API — production entry point.
This file wires the FastAPI app together; business logic lives in routers/.
"""

import asyncio
import json
import logging
import os
import secrets
import sys
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Ensure backend dir is on the path so routers can import sibling modules
sys.path.insert(0, str(Path(__file__).parent))

import database
import auth as _auth_module
from auth import ApiKeyMiddleware
import auth_jwt as _auth_module_jwt
from auth_jwt import decode_token
import admin_db_api
import trading_control
import ops_alerts
import paper_runtime
from logging_config import configure_logging, new_request_id, request_id_var
from routers import (
    adapters,
    alerts,
    auth as auth_router_module,
    backtest,
    components,
    database_ops,
    market_data,
    orders,
    positions,
    risk,
    strategies,
    system,
    users,
    trading,
)
from routers.strategies import load_strategies_from_db
from routers.components import load_component_states
from state import manager, nautilus_system, live_manager
from order_service import order_service
from alert_monitor import run_alert_monitor


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────

_WEAK_SECRET = "dev-secret-key-CHANGE-IN-PRODUCTION-min-32-chars"


def _check_production_secrets() -> None:
    """
    Fail closed on weak/missing secrets. ENVIRONMENT defaults to "production", so a
    deployment that forgets to set variables refuses to start. Only an explicit
    ENVIRONMENT=development downgrades these checks to warnings.
    """
    secret_key = os.getenv("SECRET_KEY", _WEAK_SECRET)
    admin_pw = os.getenv("ADMIN_PASSWORD", "admin")
    env = os.getenv("ENVIRONMENT", "production").lower()

    problems = []
    if secret_key == _WEAK_SECRET or len(secret_key) < 32:
        problems.append(
            "SECRET_KEY is not set or too short (< 32 chars). "
            "Generate one with: openssl rand -hex 32"
        )
    if admin_pw.lower() in ("admin", "password", "123456", "") or len(admin_pw) < 12:
        problems.append("ADMIN_PASSWORD is missing, a known-weak value, or shorter than 12 chars.")
    try:
        from credential_utils import validate_encryption_key
        validate_encryption_key()
    except Exception as exc:
        problems.append(f"ENCRYPTION_KEY invalid: {exc}")

    if problems:
        border = "=" * 70
        print(f"\n{border}", file=sys.stderr)
        print(f"  SECURITY {'WARNING (development)' if env == 'development' else 'ERROR'}", file=sys.stderr)
        for w in problems:
            print(f"  - {w}", file=sys.stderr)
        if env != "development":
            print("  Refusing to start. Set the variables above, or ENVIRONMENT=development for local use only.", file=sys.stderr)
            print(f"{border}\n", file=sys.stderr)
            sys.exit(1)
        print(f"{border}\n", file=sys.stderr)


async def _purge_expired_tokens_loop() -> None:
    """Hourly purge of expired entries from the token revocation table."""
    while True:
        await asyncio.sleep(3600)
        try:
            removed = await database.purge_expired_revoked_tokens()
            if removed:
                print(f"[auth] Purged {removed} expired revoked token(s)")
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: check secrets before anything else
    _check_production_secrets()
    # Initialise the SQLite schema + seed defaults
    await database.init_db()
    admin_db_api._init_db()
    await trading_control.reset_on_startup()  # always come up disarmed
    # Restore persisted strategies and component states
    await load_strategies_from_db()
    await load_component_states()
    # Start background tasks
    alert_task = asyncio.create_task(run_alert_monitor())
    await live_manager.restore_connections()
    order_service.start()  # reconcile live orders until they reach a final state
    purge_task = asyncio.create_task(_purge_expired_tokens_loop())
    ops_alert_task = asyncio.create_task(ops_alerts.sender_loop())
    paper_task = asyncio.create_task(paper_runtime.runtime_loop())
    yield
    # Shutdown: cancel background tasks
    await order_service.stop()
    for task in (alert_task, purge_task, ops_alert_task, paper_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ── App factory ───────────────────────────────────────────────────────────────

configure_logging()

_IS_PROD = os.getenv("ENVIRONMENT", "production").lower() == "production"

app = FastAPI(
    title="Nautilus Trader API",
    description="Real Nautilus Trader integration API",
    version="2.0.0",
    lifespan=lifespan,
    # Interactive docs only outside production
    docs_url=None if _IS_PROD else "/docs",
    redoc_url=None if _IS_PROD else "/redoc",
    openapi_url=None if _IS_PROD else "/openapi.json",
)

# CORS — set CORS_ORIGINS env var in production (comma-separated)
_cors_env = os.getenv("CORS_ORIGINS", "")
CORS_ORIGINS = (
    [o.strip() for o in _cors_env.split(",") if o.strip()]
    or ["http://localhost:5173", "http://localhost:3000"]
)

# API key auth (enabled when API_KEY env var is set)
app.add_middleware(ApiKeyMiddleware)

# ── Rate limiting ─────────────────────────────────────────────────────────────

_GLOBAL_RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MINUTE", "200"))
_LOGIN_RATE_LIMIT = int(os.getenv("LOGIN_RATE_LIMIT_PER_MINUTE", "5"))

# Per-IP sliding window counters: {ip: {"count": N, "window_start": T}}
_global_counters: dict = defaultdict(lambda: {"count": 0, "window_start": 0.0})
_login_counters: dict = defaultdict(lambda: {"count": 0, "window_start": 0.0})


def _check_rate_limit(counters: dict, key: str, limit: int, now: float) -> tuple[int, bool]:
    """
    Check and update a rate-limit counter.
    Returns (remaining, is_exceeded).
    """
    if len(counters) > 10_000:
        # Bound memory: drop windows that expired long ago
        for stale in [k for k, v in counters.items() if now - v["window_start"] > 120.0]:
            del counters[stale]
    state = counters[key]
    if now - state["window_start"] >= 60.0:
        state["count"] = 0
        state["window_start"] = now
    state["count"] += 1
    remaining = max(0, limit - state["count"])
    return remaining, state["count"] > limit


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    client_ip = (request.client.host if request.client else "unknown") or "unknown"
    now = time.time()
    if request.url.path == "/api/trading/kill":
        return await call_next(request)   # stopping trading is never rate limited

    # Tight limit on login endpoint
    if request.url.path == "/api/auth/login":
        _, exceeded = _check_rate_limit(_login_counters, client_ip, _LOGIN_RATE_LIMIT, now)
        if exceeded:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Retry after 60 seconds."},
                headers={"Retry-After": "60", "X-RateLimit-Remaining": "0"},
            )

    # Global rate limit (applied to all routes)
    remaining, exceeded = _check_rate_limit(_global_counters, client_ip, _GLOBAL_RATE_LIMIT, now)
    if exceeded:
        return JSONResponse(
            status_code=429,
            content={"detail": "Global rate limit exceeded. Retry after 60 seconds."},
            headers={"Retry-After": "60", "X-RateLimit-Remaining": "0"},
        )

    response = await call_next(request)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    return response


# ── JWT authentication middleware ─────────────────────────────────────────────

# Paths that are always public (no token required)
_PUBLIC_PATHS = frozenset(
    [
        "/",
        "/health",
        "/readyz",
        "/api/health",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/refresh",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/ws",
    ]
)


@app.middleware("http")
async def jwt_middleware(request: Request, call_next):
    path = request.url.path

    # Allow public paths and non-API routes
    if path in _PUBLIC_PATHS or not path.startswith("/api/"):
        return await call_next(request)

    # Skip JWT check when a valid API key is already provided (alternative auth)
    api_key_header = request.headers.get("X-API-Key", "")
    if _auth_module.API_KEY and api_key_header and secrets.compare_digest(api_key_header, _auth_module.API_KEY):
        return await call_next(request)

    # Check for valid Bearer token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing authentication token"},
        )

    token = auth_header[7:]
    payload = decode_token(token)
    if not payload:
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or expired token"},
        )

    # Check persistent DB blacklist (survives restarts)
    if payload.get("jti") and await database.is_token_revoked(payload["jti"]):
        return JSONResponse(
            status_code=401,
            content={"detail": "Token has been revoked"},
        )

    try:
        payload = await _auth_module_jwt.resolve_active_user(payload)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    request.state.user = payload
    return await call_next(request)


# Request counter middleware
@app.middleware("http")
async def _count_requests(request: Request, call_next):
    system.increment_request_counter()
    return await call_next(request)


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    """Audit trail for every state-changing API call by an authenticated user (safety net on top of
    the explicit, richer entries written by the critical paths)."""
    response = await call_next(request)
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path.startswith("/api/"):
        user = getattr(request.state, "user", None)
        if user:  # unauthenticated calls were already rejected (401) and carry no identity
            try:
                await database.log_action(
                    action=f"http:{request.method} {request.url.path}",
                    user_id=str(user.get("sub", "")),
                    resource="http",
                    details=f"status={response.status_code} request_id={request_id_var.get()}",
                    ip_address=request.client.host if request.client else "",
                )
            except Exception:  # noqa: BLE001 - auditing must never break the request
                logging.getLogger(__name__).exception("audit log write failed")
    return response


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    rid = new_request_id(request.headers.get("x-request-id"))
    token = request_id_var.set(rid)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = rid
    return response


# CORS must be added LAST so it is the outermost middleware: it has to answer
# preflight OPTIONS requests (which carry no token) and add CORS headers to
# 401/429 responses produced by the auth and rate-limit middleware above.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "Idempotency-Key", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)

# ── Include routers ───────────────────────────────────────────────────────────

app.include_router(auth_router_module.router)
app.include_router(strategies.router)
app.include_router(orders.router)
app.include_router(positions.router)
app.include_router(risk.router)
app.include_router(market_data.router)
app.include_router(alerts.router)
app.include_router(system.router)
app.include_router(backtest.router)
app.include_router(adapters.router)
app.include_router(database_ops.router)
app.include_router(components.router)
app.include_router(users.router)
app.include_router(trading.router)
app.include_router(admin_db_api.router, tags=["admin"])


# ── Root endpoint ─────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "name": "Nautilus Trader API",
        "version": "2.0.0",
        "status": "running",
        "docs": "/docs",
    }


# Alias so /health works in addition to /api/health
@app.get("/health")
async def health_alias():
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": "nautilus-trader-api",
        "version": "2.0.0",
    }


@app.get("/readyz")
async def readyz():
    """Readiness: database reachable and Nautilus engine initialised."""
    checks: dict = {}
    try:
        async with database.connect() as db:
            await db.execute("SELECT 1")
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
    checks["engine"] = "ok" if nautilus_system.is_initialized else "not_initialized"
    ready = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )


# ── WebSocket live-data helpers ───────────────────────────────────────────────

async def _collect_live_snapshot() -> dict:
    """Gather a lightweight snapshot of live system state for WebSocket push."""
    info = nautilus_system.get_system_info()

    # System metrics (non-blocking psutil)
    metrics: dict = {}
    try:
        import psutil
        metrics = {
            "cpu_percent": round(psutil.cpu_percent(interval=None), 1),
            "memory_percent": round(psutil.virtual_memory().percent, 1),
        }
    except Exception:
        pass

    # Strategies
    strategy_list = [
        {
            "id": s["id"],
            "name": s.get("name", s["id"]),
            "status": s.get("status", "unknown"),
        }
        for s in nautilus_system.get_all_strategies()
    ]

    # Open positions (from latest backtest results, filtered by closed set)
    all_positions = []
    for results in nautilus_system.backtest_results.values():
        all_positions.extend(results.get("positions", []))
    open_positions = [p for p in all_positions if p.get("is_open")]

    # Recent orders count
    order_count = sum(
        len(r.get("orders", []))
        for r in nautilus_system.backtest_results.values()
    )

    return {
        "type": "live_data",
        "ts": datetime.now(timezone.utc).isoformat(),
        "engine": {
            "is_initialized": info["is_initialized"],
            "trader_id": info["trader_id"],
            "strategies_count": info["strategies_count"],
            "backtests_count": info["backtests_count"],
        },
        "metrics": metrics,
        "strategies": strategy_list,
        "open_positions_count": len(open_positions),
        "total_orders_count": order_count,
    }


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = ""):
    """
    WebSocket live-data endpoint.
    Clients must provide a valid JWT via query param: /ws?token=<jwt>
    """
    # Validate token before accepting the connection
    if not token:
        await websocket.close(code=4001, reason="Missing authentication token")
        return

    payload = decode_token(token)
    if not payload:
        await websocket.close(code=4001, reason="Invalid or expired token")
        return

    if payload.get("jti") and await database.is_token_revoked(payload["jti"]):
        await websocket.close(code=4001, reason="Token has been revoked")
        return

    await manager.connect(websocket)
    last_push = 0.0
    try:
        info = nautilus_system.get_system_info()
        await websocket.send_json(
            {
                "type": "connection",
                "status": "connected",
                "trader_id": info["trader_id"],
                "is_initialized": info["is_initialized"],
            }
        )
        while True:
            try:
                # Short timeout so we can push live data on schedule
                data = await asyncio.wait_for(websocket.receive_text(), timeout=2.0)
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                now = time.time()
                # Heartbeat every tick
                await websocket.send_json(
                    {"type": "heartbeat", "ts": datetime.now(timezone.utc).isoformat()}
                )
                # Full live-data push every 3 seconds
                if now - last_push >= 3.0:
                    snapshot = await _collect_live_snapshot()
                    await websocket.send_json(snapshot)
                    last_push = now
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("NAUTILUS_API_PORT", "8000"))
    print(f"Starting Nautilus Trader API on port {port}")
    print(f"Docs: http://0.0.0.0:{port}/docs")
    # Behind a reverse proxy (Traefik on Dokploy) the real client IP arrives in X-Forwarded-For;
    # without this every user shares the proxy's IP and one attacker can lock everyone out of login.
    # Only trust forwarded headers from FORWARDED_ALLOW_IPS (comma-separated proxy IPs/CIDRs).
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        log_config=None,   # keep our JSON logging; uvicorn must not reinstall its own handlers
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )
