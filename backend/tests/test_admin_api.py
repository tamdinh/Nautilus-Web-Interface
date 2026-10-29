"""Admin API is part of the main backend: every route requires an admin JWT."""

import pytest


def _login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def trader_headers(client):
    client.post("/api/users", json={"username": "trader9", "password": "secret1234", "role": "trader"})
    return _login(client, "trader9", "secret1234")


def test_admin_routes_reject_unauthenticated(client):
    r = client.get("/api/admin/users", headers={"Authorization": ""})
    assert r.status_code == 401


def test_admin_routes_forbidden_for_trader(client, trader_headers):
    for path in ("/api/admin/users", "/api/admin/settings", "/api/admin/api-configs", "/api/admin/endpoints"):
        assert client.get(path, headers=trader_headers).status_code == 403, path


def test_admin_users_never_expose_password_hash(client):
    client.post("/api/admin/users", json={"username": "u1", "email": "u1@x.io", "role": "viewer", "password": "pw-123456"})
    users = client.get("/api/admin/users").json()["users"]
    assert users and all("password_hash" not in u for u in users)


def test_admin_user_role_is_validated(client):
    r = client.post("/api/admin/users", json={"username": "u2", "email": "u2@x.io", "role": "root"})
    assert r.status_code == 400


def test_api_configs_never_expose_api_key(client):
    client.post("/api/admin/api-configs", json={"name": "svc", "endpoint": "https://x.io", "api_key": "sekret-value"})
    configs = client.get("/api/admin/api-configs").json()["configs"]
    assert configs and all("api_key" not in c for c in configs)
    assert "sekret-value" not in str(configs)


def test_deactivated_user_loses_access_immediately(client):
    r = client.post("/api/users", json={"username": "gone", "password": "secret1234", "role": "trader"})
    user_id = r.json()["user"]["id"]
    headers = _login(client, "gone", "secret1234")
    assert client.get("/api/orders", headers=headers).status_code == 200
    assert client.delete(f"/api/users/{user_id}").status_code == 200
    assert client.get("/api/orders", headers=headers).status_code == 401
