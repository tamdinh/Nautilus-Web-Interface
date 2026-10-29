"""
Shared pytest configuration.

Resets module-level rate-limit counters before each test so that the
/api/auth/login call inside every `client` fixture is never blocked by the
5-req/minute cap that accumulates across the test session.
"""
import sys
import tempfile
from pathlib import Path

import os

import pytest

# Tests run as a development deployment (weak default secrets are tolerated).
os.environ.setdefault("ENVIRONMENT", "development")
# Tests drive reconciliation explicitly; no background loop
os.environ.setdefault("ORDER_RECONCILER_ENABLED", "0")

# Point every database at a throw-away directory BEFORE the app modules are imported, so a test that
# forgets to monkeypatch a path can never touch the developer's/real data under backend/data.
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="nautilus-tests-")
os.environ["NAUTILUS_DB_PATH"] = str(Path(_TEST_DATA_DIR) / "nautilus.db")
os.environ["ADMIN_DB_PATH"] = str(Path(_TEST_DATA_DIR) / "admin_panel.db")

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Authenticated admin test client with isolated DB."""
    import database
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")

    from fastapi.testclient import TestClient
    from nautilus_fastapi import app

    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        assert r.status_code == 200, f"Login failed: {r.text}"
        token = r.json()["access_token"]
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c


@pytest.fixture(autouse=True)
def reset_rate_limit_counters():
    """Clear in-memory rate-limit state before every test."""
    try:
        import nautilus_fastapi
        nautilus_fastapi._login_counters.clear()
        nautilus_fastapi._global_counters.clear()
    except (ImportError, AttributeError):
        pass
    yield


@pytest.fixture(autouse=True)
def reset_live_manager():
    """Reset LiveTradingManager singleton state between tests to prevent leakage."""
    try:
        from state import live_manager
        live_manager._connections.clear()
        live_manager._is_active = False
    except (ImportError, AttributeError):
        pass
    yield


@pytest.fixture(autouse=True)
def block_real_exchange_calls(monkeypatch):
    """Point exchange REST clients at a closed local port so tests never reach a real exchange.

    Connection refused is treated as a network failure (adapter -> connected_offline).
    Tests that need responses use httpx.MockTransport or an explicit base_url.
    """
    monkeypatch.setenv("BINANCE_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("BYBIT_BASE_URL", "http://127.0.0.1:9")
    yield


@pytest.fixture(autouse=True)
def isolate_admin_db(tmp_path, monkeypatch):
    """Never let tests touch the real admin_panel.db."""
    import admin_db_api
    monkeypatch.setattr(admin_db_api, "DB_PATH", str(tmp_path / "admin_panel.db"))
    yield


@pytest.fixture(autouse=True)
def forbid_real_data_dir():
    """Fail loudly if any test ends up pointing at the real backend/data directory."""
    import database
    real = (Path(__file__).parent.parent / "data").resolve()
    assert real not in Path(database.DB_PATH).resolve().parents, "test is using the real database"
    yield


@pytest.fixture
def fake_venue(monkeypatch):
    """Replace the exchange side of the shared order service with a scripted fake venue."""
    import order_service as osvc
    import routers.trading as rt
    from tests.test_order_service import FakeManager

    monkeypatch.setattr(osvc, "LOOKUP_DELAY", 0)
    fake = FakeManager()
    monkeypatch.setattr(osvc.order_service, "manager", fake)
    monkeypatch.setattr(rt, "live_manager", fake)
    import routers.positions as rp
    monkeypatch.setattr(rp, "live_manager", fake)
    return fake
