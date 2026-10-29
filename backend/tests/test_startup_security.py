"""Fail-closed startup checks and credential encryption key handling."""

import pytest
from cryptography.fernet import Fernet

import credential_utils
import nautilus_fastapi

GOOD_SECRET = "x" * 40
GOOD_PASSWORD = "a-strong-passphrase"


@pytest.fixture
def prod_env(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", GOOD_SECRET)
    monkeypatch.setenv("ADMIN_PASSWORD", GOOD_PASSWORD)
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    return monkeypatch


def test_production_with_good_config_starts(prod_env):
    nautilus_fastapi._check_production_secrets()  # no SystemExit


@pytest.mark.parametrize("var,value", [
    ("SECRET_KEY", "short"),
    ("SECRET_KEY", nautilus_fastapi._WEAK_SECRET),
    ("ADMIN_PASSWORD", "admin"),
    ("ADMIN_PASSWORD", "short-pw"),
    ("ENCRYPTION_KEY", ""),
    ("ENCRYPTION_KEY", "not-a-fernet-key"),
])
def test_production_refuses_weak_or_missing_secrets(prod_env, var, value):
    prod_env.setenv(var, value)
    with pytest.raises(SystemExit):
        nautilus_fastapi._check_production_secrets()


def test_unset_environment_defaults_to_production(prod_env):
    prod_env.delenv("ENVIRONMENT")
    prod_env.setenv("SECRET_KEY", "short")
    with pytest.raises(SystemExit):
        nautilus_fastapi._check_production_secrets()


def test_development_only_warns(prod_env, capsys):
    prod_env.setenv("ENVIRONMENT", "development")
    prod_env.setenv("SECRET_KEY", "short")
    nautilus_fastapi._check_production_secrets()  # no SystemExit
    assert "SECURITY WARNING" in capsys.readouterr().err


def test_missing_encryption_key_is_an_error_outside_development(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError):
        credential_utils.encrypt_credential("secret")


def test_encryption_key_rotation_decrypts_with_old_key(monkeypatch):
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setenv("ENCRYPTION_KEY", old)
    token = credential_utils.encrypt_credential("exchange-secret")
    monkeypatch.setenv("ENCRYPTION_KEY", f"{new},{old}")
    assert credential_utils.decrypt_credential(token) == "exchange-secret"
    # new encryptions use the first key only
    monkeypatch.setenv("ENCRYPTION_KEY", new)
    assert credential_utils.decrypt_credential(credential_utils.encrypt_credential("x")) == "x"
