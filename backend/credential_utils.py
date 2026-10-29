"""
Credential encryption utilities — Sprint 1 (S1-01).

Uses Fernet symmetric encryption (AES-128-CBC with HMAC-SHA256).
The encryption key is read from the ENCRYPTION_KEY environment variable (comma-separated
for rotation). If not set, a temporary key is generated only when ENVIRONMENT=development.
"""

import logging
import os
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

logger = logging.getLogger(__name__)


def _is_dev() -> bool:
    return os.getenv("ENVIRONMENT", "production").lower() == "development"


def _get_fernet() -> MultiFernet:
    """
    Return a MultiFernet built from ENCRYPTION_KEY.

    ENCRYPTION_KEY may hold several comma-separated Fernet keys: the first encrypts,
    all of them decrypt (key rotation). Outside ENVIRONMENT=development a missing or
    invalid key is a hard error — a throw-away key would silently make every stored
    credential undecryptable after a restart.
    """
    raw = os.getenv("ENCRYPTION_KEY", "").strip()
    if not raw:
        if not _is_dev():
            raise RuntimeError("ENCRYPTION_KEY is not set (generate one with: python -c "
                               "\"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\")")
        # Development only: a fresh key per process; stored credentials do not survive a restart.
        if not hasattr(_get_fernet, "_dev_key"):
            _get_fernet._dev_key = Fernet.generate_key()
        return MultiFernet([Fernet(_get_fernet._dev_key)])
    return MultiFernet([Fernet(k.strip().encode()) for k in raw.split(",") if k.strip()])


def has_persistent_key() -> bool:
    """True when ENCRYPTION_KEY is configured (so ciphertext survives a restart)."""
    return bool(os.getenv("ENCRYPTION_KEY", "").strip())


def validate_encryption_key() -> None:
    """Raise if ENCRYPTION_KEY is missing (outside dev) or not a valid Fernet key. Call at startup."""
    _get_fernet()


def encrypt_credential(plaintext: str) -> str:
    """Encrypt a plaintext credential string and return a base64 ciphertext string."""
    if not plaintext:
        return ""
    # Strip null bytes (security hardening)
    cleaned = plaintext.replace("\x00", "")
    if not cleaned:
        return ""
    f = _get_fernet()
    return f.encrypt(cleaned.encode()).decode()


def decrypt_credential(ciphertext: str) -> str:
    """Decrypt a base64 ciphertext string back to plaintext. Returns '' on error."""
    if not ciphertext:
        return ""
    try:
        f = _get_fernet()
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        # Wrong key, corrupt data, or not encrypted → return empty string
        logger.warning("Credential decryption failed (wrong ENCRYPTION_KEY or corrupt data)")
        return ""


def mask_credential(plaintext: str, visible_chars: int = 4) -> str:
    """Return a masked version showing only the last N characters: '********abcd'."""
    if not plaintext:
        return "****"
    if len(plaintext) <= visible_chars:
        return "*" * 8
    return "*" * 8 + plaintext[-visible_chars:]
