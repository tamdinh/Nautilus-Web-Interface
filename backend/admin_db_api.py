"""
Admin Panel Database API
SQLite database management endpoints with auto audit logging.

Mounted into the main backend (nautilus_fastapi.py). Every route requires a valid
admin JWT; there is no separate service, port or API key.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional
import hashlib
import secrets
import sqlite3
import json
import os
from datetime import datetime
from pathlib import Path

from auth_jwt import require_admin

# Override with ADMIN_DB_PATH (e.g. a mounted volume). Read at call time so tests can monkeypatch.
DB_PATH = os.getenv("ADMIN_DB_PATH") or str(Path(__file__).parent / "data" / "admin_panel.db")

# Roles an admin may assign through this API
ALLOWED_ROLES = frozenset({"admin", "trader", "viewer"})

# ── Schema creation + seed data ──────────────────────────────────────────────

def _init_db() -> None:
    """Create all tables and seed default data if they don't exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            category    TEXT NOT NULL DEFAULT 'general',
            description TEXT,
            updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            email         TEXT UNIQUE NOT NULL,
            role          TEXT NOT NULL DEFAULT 'viewer',
            password_hash TEXT,
            created_at    TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS api_configs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT UNIQUE NOT NULL,
            endpoint   TEXT NOT NULL,
            api_key    TEXT,
            is_enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            task_type  TEXT NOT NULL,
            schedule   TEXT NOT NULL,
            parameters TEXT,
            is_active  INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            action     TEXT NOT NULL,
            user       TEXT NOT NULL DEFAULT 'system',
            details    TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS components (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT UNIQUE NOT NULL,
            type        TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'stopped',
            description TEXT,
            updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS features (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT UNIQUE NOT NULL,
            category    TEXT NOT NULL DEFAULT 'general',
            enabled     INTEGER NOT NULL DEFAULT 0,
            description TEXT,
            updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS adapters (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT UNIQUE NOT NULL,
            type           TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'disconnected',
            last_connected TEXT,
            config         TEXT,
            updated_at     TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS api_endpoints (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            url          TEXT NOT NULL,
            is_active    INTEGER NOT NULL DEFAULT 1,
            last_updated TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()

    # ── Seed default data (only if tables are empty) ──────────────────────────

    # Settings
    if not c.execute("SELECT 1 FROM settings LIMIT 1").fetchone():
        defaults = [
            ("theme",            "dark",          "ui",           "Interface theme"),
            ("language",         "en",            "ui",           "Display language"),
            ("timezone",         "UTC",           "general",      "System timezone"),
            ("log_level",        "INFO",          "system",       "Logging verbosity"),
            ("max_connections",  "100",           "performance",  "Max DB connections"),
            ("session_timeout",  "30",            "security",     "Session timeout (min)"),
        ]
        c.executemany(
            "INSERT OR IGNORE INTO settings (key, value, category, description) VALUES (?, ?, ?, ?)",
            defaults
        )

    # Users
    if not c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        c.execute(
            "INSERT OR IGNORE INTO users (username, email, role) VALUES (?, ?, ?)",
            ("admin", "admin@nautilus.local", "admin")
        )

    # API Configs
    if not c.execute("SELECT 1 FROM api_configs LIMIT 1").fetchone():
        c.executemany(
            "INSERT OR IGNORE INTO api_configs (name, endpoint, is_enabled) VALUES (?, ?, ?)",
            [
                ("Nautilus Main API", "http://localhost:8000", 1),
                ("Binance REST",      "https://api.binance.com", 1),
                ("Bybit REST",        "https://api.bybit.com",   0),
            ]
        )

    # Scheduled Tasks
    if not c.execute("SELECT 1 FROM scheduled_tasks LIMIT 1").fetchone():
        c.executemany(
            "INSERT OR IGNORE INTO scheduled_tasks (name, task_type, schedule, is_active) VALUES (?, ?, ?, ?)",
            [
                ("Daily Backup",    "backup",    "0 2 * * *", 1),
                ("DB Optimize",     "optimize",  "0 3 * * 0", 1),
                ("Cache Cleanup",   "clean",     "0 1 * * *", 1),
            ]
        )

    # Components
    if not c.execute("SELECT 1 FROM components LIMIT 1").fetchone():
        c.executemany(
            "INSERT OR IGNORE INTO components (name, type, status, description) VALUES (?, ?, ?, ?)",
            [
                ("Data Engine",       "DataEngine",      "running", "Handles market data subscriptions"),
                ("Execution Engine",  "ExecutionEngine", "running", "Manages order execution"),
                ("Risk Engine",       "RiskEngine",      "running", "Enforces risk limits"),
                ("Portfolio",         "Portfolio",       "active",  "Tracks positions and PnL"),
                ("Cache",             "Cache",           "active",  "In-memory data cache"),
                ("Message Bus",       "MessageBus",      "active",  "Internal event bus"),
            ]
        )

    # Features
    if not c.execute("SELECT 1 FROM features LIMIT 1").fetchone():
        c.executemany(
            "INSERT OR IGNORE INTO features (name, category, enabled, description) VALUES (?, ?, ?, ?)",
            [
                ("Live Trading",       "trading",   0, "Enable live order execution"),
                ("Backtesting",        "trading",   1, "Enable strategy backtesting"),
                ("Market Data",        "data",      1, "Enable live market data feeds"),
                ("Price Alerts",       "alerts",    1, "Enable price alert notifications"),
                ("Risk Management",    "risk",      1, "Enable risk limit enforcement"),
                ("Email Notifications","notify",    0, "Send email alerts"),
                ("Slack Notifications","notify",    0, "Send Slack alerts"),
                ("API Rate Limiting",  "security",  1, "Enforce API rate limits"),
            ]
        )

    # Adapters
    if not c.execute("SELECT 1 FROM adapters LIMIT 1").fetchone():
        c.executemany(
            "INSERT OR IGNORE INTO adapters (name, type, status) VALUES (?, ?, ?)",
            [
                ("Binance Spot",       "crypto",  "disconnected"),
                ("Binance Futures",    "crypto",  "disconnected"),
                ("Bybit",              "crypto",  "disconnected"),
                ("Coinbase Advanced",  "crypto",  "disconnected"),
                ("Interactive Brokers","equity",  "disconnected"),
                ("dYdX",               "crypto",  "disconnected"),
                ("Kraken",             "crypto",  "disconnected"),
                ("OKX",                "crypto",  "disconnected"),
            ]
        )

    # API Endpoints
    if not c.execute("SELECT 1 FROM api_endpoints LIMIT 1").fetchone():
        c.executemany(
            "INSERT OR IGNORE INTO api_endpoints (name, url, is_active) VALUES (?, ?, ?)",
            [
                ("Main API",       "http://localhost:8000", 1),
                ("WebSocket",      "ws://localhost:8000/ws", 1),
            ]
        )

    # The standalone admin service (port 8001) no longer exists
    c.execute("DELETE FROM api_endpoints WHERE name = 'Admin DB API' AND url LIKE '%:8001'")

    # Audit log entry
    if not c.execute("SELECT 1 FROM audit_logs LIMIT 1").fetchone():
        c.execute(
            "INSERT INTO audit_logs (action, user, details) VALUES (?, ?, ?)",
            ("system_init", "system", "Admin database initialised with default seed data")
        )

    conn.commit()
    conn.close()


# ── Lifespan ──────────────────────────────────────────────────────────────────

router = APIRouter(dependencies=[Depends(require_admin)])


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _log_action(conn: sqlite3.Connection, action: str, user: str = "system", details: str = "") -> None:
    """Insert an audit log entry within an existing connection."""
    conn.execute(
        "INSERT INTO audit_logs (action, user, details) VALUES (?, ?, ?)",
        (action, user, details),
    )


def _hash_password(password: str) -> str:
    """Hash password with PBKDF2-HMAC-SHA256 + random salt (260k iterations)."""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return f"pbkdf2:sha256:{salt}:{dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    """Verify a password against a stored hash (PBKDF2 or legacy SHA256)."""
    if stored.startswith("pbkdf2:sha256:"):
        _, _, salt, dk_hex = stored.split(":", 3)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
        return secrets.compare_digest(dk.hex(), dk_hex)
    # Legacy SHA256 fallback — still works but considered weak
    return secrets.compare_digest(
        hashlib.sha256(password.encode()).hexdigest(), stored
    )


# ── Pydantic models ───────────────────────────────────────────────────────────

class Setting(BaseModel):
    key: str
    value: str
    category: str
    description: Optional[str] = None

class User(BaseModel):
    username: str
    email: str
    role: str = "viewer"
    password: Optional[str] = None

class APIConfig(BaseModel):
    name: str
    endpoint: str
    api_key: Optional[str] = None
    is_enabled: bool = True

class ScheduledTask(BaseModel):
    name: str
    task_type: str
    schedule: str
    parameters: Optional[str] = None
    is_active: bool = True


# ── Settings ──────────────────────────────────────────────────────────────────

@router.get("/api/admin/settings")
def get_settings():
    conn = get_db()
    cursor = conn.execute("SELECT * FROM settings ORDER BY category, key")
    settings = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return {"settings": settings}

@router.get("/api/admin/settings/{key}")
def get_setting(key: str):
    conn = get_db()
    cursor = conn.execute("SELECT * FROM settings WHERE key = ?", (key,))
    setting = cursor.fetchone()
    conn.close()
    if not setting:
        raise HTTPException(status_code=404, detail="Setting not found")
    return dict(setting)

@router.post("/api/admin/settings")
def create_setting(setting: Setting):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO settings (key, value, category, description) VALUES (?, ?, ?, ?)",
            (setting.key, setting.value, setting.category, setting.description)
        )
        _log_action(conn, "setting.create", details=f"key={setting.key} category={setting.category}")
        conn.commit()
        conn.close()
        return {"message": f"Setting '{setting.key}' created successfully"}
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Setting already exists")

@router.put("/api/admin/settings/{key}")
def update_setting(key: str, value: str = Query(...)):
    conn = get_db()
    conn.execute(
        "UPDATE settings SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?",
        (value, key)
    )
    if conn.total_changes == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Setting not found")
    _log_action(conn, "setting.update", details=f"key={key} value={value}")
    conn.commit()
    conn.close()
    return {"message": f"Setting '{key}' updated successfully"}

@router.delete("/api/admin/settings/{key}")
def delete_setting(key: str):
    conn = get_db()
    conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    if conn.total_changes == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Setting not found")
    _log_action(conn, "setting.delete", details=f"key={key}")
    conn.commit()
    conn.close()
    return {"message": f"Setting '{key}' deleted successfully"}


# ── Users ─────────────────────────────────────────────────────────────────────

@router.get("/api/admin/users")
def get_users():
    conn = get_db()
    cursor = conn.execute(
        "SELECT id, username, email, role, created_at FROM users ORDER BY username"
    )
    users = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return {"users": users}

@router.post("/api/admin/users")
def create_user(user: User):
    if user.role not in ALLOWED_ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {sorted(ALLOWED_ROLES)}")
    conn = get_db()
    try:
        pw_hash = _hash_password(user.password) if user.password else None
        conn.execute(
            "INSERT INTO users (username, email, role, password_hash) VALUES (?, ?, ?, ?)",
            (user.username, user.email, user.role, pw_hash)
        )
        _log_action(conn, "user.create", details=f"username={user.username} role={user.role}")
        conn.commit()
        conn.close()
        return {"message": f"User '{user.username}' created successfully"}
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="User already exists")


# ── API Configs ───────────────────────────────────────────────────────────────

@router.get("/api/admin/api-configs")
def get_api_configs():
    conn = get_db()
    cursor = conn.execute("SELECT * FROM api_configs ORDER BY name")
    configs = []
    for row in cursor.fetchall():
        cfg = dict(row)
        cfg["has_api_key"] = bool(cfg.pop("api_key", None))
        configs.append(cfg)
    conn.close()
    return {"configs": configs}

@router.post("/api/admin/api-configs")
def create_api_config(config: APIConfig):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO api_configs (name, endpoint, api_key, is_enabled) VALUES (?, ?, ?, ?)",
            (config.name, config.endpoint, config.api_key, config.is_enabled)
        )
        _log_action(conn, "api_config.create", details=f"name={config.name} endpoint={config.endpoint}")
        conn.commit()
        conn.close()
        return {"message": f"API config '{config.name}' created successfully"}
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="API config already exists")


# ── Scheduled Tasks ───────────────────────────────────────────────────────────

@router.get("/api/admin/scheduled-tasks")
def get_scheduled_tasks():
    conn = get_db()
    cursor = conn.execute("SELECT * FROM scheduled_tasks ORDER BY name")
    tasks = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return {"tasks": tasks}

@router.post("/api/admin/scheduled-tasks")
def create_scheduled_task(task: ScheduledTask):
    conn = get_db()
    conn.execute(
        "INSERT INTO scheduled_tasks (name, task_type, schedule, parameters, is_active) VALUES (?, ?, ?, ?, ?)",
        (task.name, task.task_type, task.schedule, task.parameters, task.is_active)
    )
    _log_action(conn, "task.create", details=f"name={task.name} type={task.task_type} schedule={task.schedule}")
    conn.commit()
    conn.close()
    return {"message": f"Task '{task.name}' created successfully"}


# ── Audit Logs ────────────────────────────────────────────────────────────────

@router.get("/api/admin/audit-logs")
def get_audit_logs(limit: int = 100):
    conn = get_db()
    cursor = conn.execute("SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT ?", (limit,))
    logs = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return {"logs": logs}


# ── Components ────────────────────────────────────────────────────────────────

@router.get("/api/admin/components")
def get_components():
    conn = get_db()
    cursor = conn.execute("SELECT * FROM components ORDER BY name")
    components = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return {"components": components}

@router.get("/api/admin/components/{component_id}")
def get_component(component_id: int):
    conn = get_db()
    cursor = conn.execute("SELECT * FROM components WHERE id = ?", (component_id,))
    component = cursor.fetchone()
    conn.close()
    if not component:
        raise HTTPException(status_code=404, detail="Component not found")
    return dict(component)

@router.put("/api/admin/components/{component_id}/status")
def update_component_status(component_id: int, status: str = Query(...)):
    conn = get_db()
    row = conn.execute("SELECT name FROM components WHERE id = ?", (component_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Component not found")
    conn.execute(
        "UPDATE components SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, component_id)
    )
    _log_action(conn, "component.status_change", details=f"component={row['name']} status={status}")
    conn.commit()
    conn.close()
    return {"message": f"Component status updated to '{status}'"}


@router.post("/api/admin/components/sync")
def sync_components_from_main_api():
    """
    Pull live component statuses from the engine component router
    and update the admin DB to reflect current engine state.
    """
    from routers.components import _COMPONENT_DEFS, _current_status

    components_live = [
        {"name": info["name"], "status": _current_status(cid)}
        for cid, info in _COMPONENT_DEFS.items()
    ]

    conn = get_db()
    updated = 0
    for comp in components_live:
        name = comp.get("name", "")
        status = comp.get("status", "stopped")
        cur = conn.execute(
            "UPDATE components SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE name = ?",
            (status, name)
        )
        updated += cur.rowcount
    _log_action(conn, "components.sync", details=f"synced {updated} components from main API")
    conn.commit()
    conn.close()
    return {"success": True, "synced": updated, "message": f"Synced {updated} component statuses"}


# ── Features ──────────────────────────────────────────────────────────────────

@router.get("/api/admin/features")
def get_features():
    conn = get_db()
    cursor = conn.execute("SELECT * FROM features ORDER BY category, name")
    features = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return {"features": features}

@router.get("/api/admin/features/{feature_id}")
def get_feature(feature_id: int):
    conn = get_db()
    cursor = conn.execute("SELECT * FROM features WHERE id = ?", (feature_id,))
    feature = cursor.fetchone()
    conn.close()
    if not feature:
        raise HTTPException(status_code=404, detail="Feature not found")
    return dict(feature)

@router.put("/api/admin/features/{feature_id}/toggle")
def toggle_feature(feature_id: int):
    conn = get_db()
    cursor = conn.execute("SELECT name, enabled FROM features WHERE id = ?", (feature_id,))
    feature = cursor.fetchone()
    if not feature:
        conn.close()
        raise HTTPException(status_code=404, detail="Feature not found")
    new_status = 0 if feature['enabled'] else 1
    conn.execute(
        "UPDATE features SET enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_status, feature_id)
    )
    _log_action(
        conn, "feature.toggle",
        details=f"feature={feature['name']} enabled={'true' if new_status else 'false'}"
    )
    conn.commit()
    conn.close()
    return {"message": f"Feature {'enabled' if new_status else 'disabled'}", "enabled": bool(new_status)}


# ── Adapters ──────────────────────────────────────────────────────────────────

@router.get("/api/admin/adapters")
def get_adapters():
    conn = get_db()
    cursor = conn.execute("SELECT * FROM adapters ORDER BY type, name")
    adapters = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return {"adapters": adapters}

@router.get("/api/admin/adapters/{adapter_id}")
def get_adapter(adapter_id: int):
    conn = get_db()
    cursor = conn.execute("SELECT * FROM adapters WHERE id = ?", (adapter_id,))
    adapter = cursor.fetchone()
    conn.close()
    if not adapter:
        raise HTTPException(status_code=404, detail="Adapter not found")
    return dict(adapter)

@router.put("/api/admin/adapters/{adapter_id}/status")
def update_adapter_status(adapter_id: int, status: str = Query(...)):
    conn = get_db()
    row = conn.execute("SELECT name FROM adapters WHERE id = ?", (adapter_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Adapter not found")
    last_connected = datetime.now().isoformat() if status == 'connected' else None
    conn.execute(
        "UPDATE adapters SET status = ?, last_connected = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, last_connected, adapter_id)
    )
    _log_action(conn, "adapter.status_change", details=f"adapter={row['name']} status={status}")
    conn.commit()
    conn.close()
    return {"message": f"Adapter status updated to '{status}'"}


# ── API Endpoints ─────────────────────────────────────────────────────────────

@router.get("/api/admin/endpoints")
def get_api_endpoints():
    conn = get_db()
    cursor = conn.execute("SELECT * FROM api_endpoints WHERE is_active = 1")
    endpoints = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return {"endpoints": endpoints}

@router.put("/api/admin/endpoints/{endpoint_id}")
def update_api_endpoint(endpoint_id: int, data: dict):
    conn = get_db()
    conn.execute(
        "UPDATE api_endpoints SET url = ?, last_updated = CURRENT_TIMESTAMP WHERE id = ?",
        (data.get("url"), endpoint_id)
    )
    if conn.total_changes == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Endpoint not found")
    conn.commit()
    conn.close()
    return {"success": True, "message": "Endpoint updated"}
