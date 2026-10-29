"""
Step-up authentication for dangerous actions (switch to live, arm, disarm-with-flatten, limit changes).

A valid JWT is not enough to move real money: the caller must also present a fresh TOTP code.
Codes are single-use — a used (user, 30s time-step) pair is persisted, so a code observed over a
shoulder or in a log cannot be replayed, even after a restart.

Policy (STEP_UP_REQUIRED env): enforced by default outside ENVIRONMENT=development. If enforcement
is on and the user has no 2FA enabled the action is refused (enable 2FA first).
"""

import os
import time
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import pyotp
from fastapi import HTTPException

import database

_TABLE = (
    "CREATE TABLE IF NOT EXISTS totp_used ("
    " username TEXT NOT NULL, counter INTEGER NOT NULL, used_at TEXT NOT NULL,"
    " PRIMARY KEY (username, counter))"
)
_STEP = 30


def required() -> bool:
    default = "0" if os.getenv("ENVIRONMENT", "production").lower() == "development" else "1"
    return os.getenv("STEP_UP_REQUIRED", default) == "1"


def _denied(code: str, message: str, status: int = 403) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


async def verify(username: str, code: Optional[str]) -> None:
    """Raise HTTPException unless the caller proved possession of their authenticator (when required)."""
    if not required():
        return
    twofa = await database.get_user_2fa(username)
    if not twofa or not twofa.get("two_factor_enabled") or not twofa.get("totp_secret"):
        raise _denied("2fa_not_enabled",
                      "This action requires two-factor authentication: enable 2FA on your account first.")
    if not code or not code.strip().isdigit():
        raise _denied("step_up_required", "A current 6-digit authenticator code is required (totp_code).")

    totp = pyotp.TOTP(twofa["totp_secret"])
    now = time.time()
    matched: Optional[int] = None
    for offset in (-1, 0, 1):  # ±1 step of clock drift
        if totp.at(now + offset * _STEP) == code.strip():
            matched = int((now + offset * _STEP) // _STEP)
            break
    if matched is None:
        raise _denied("invalid_totp", "Invalid authenticator code.")

    async with database.connect() as db:
        await db.execute(_TABLE)
        try:
            await db.execute(
                "INSERT INTO totp_used (username, counter, used_at) VALUES (?, ?, ?)",
                (username, matched, datetime.now(timezone.utc).isoformat()))
            await db.execute("DELETE FROM totp_used WHERE counter < ?", (matched - 20,))  # keep the table tiny
            await db.commit()
        except aiosqlite.IntegrityError:
            raise _denied("totp_replayed",
                          "This code was already used. Wait for the next code and try again.")
