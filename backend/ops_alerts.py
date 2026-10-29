"""
Operational alert outbox.

Safety-relevant events (order rejected / outcome unknown / stuck, kill switch or risk halt, venue
credentials rejected) are written to a durable table first and delivered by a background sender with
exponential backoff, so a Telegram/SMTP outage or a restart never loses them. `enqueue` never raises:
alerting must not break the trading path.
"""

import asyncio
import html
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiosqlite

import database
from notifications import email_notifier, telegram_notifier

logger = logging.getLogger(__name__)

SEND_INTERVAL = 10.0
MAX_ATTEMPTS = 8
BASE_BACKOFF = 30.0
MAX_BACKOFF = 3600.0
DEDUPE_SECONDS = 300
RETENTION_DAYS = 14

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ops_alert_outbox (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    severity        TEXT NOT NULL,
    text            TEXT NOT NULL,
    dedupe_key      TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    sent_channels   TEXT NOT NULL DEFAULT '[]',
    next_attempt_at TEXT NOT NULL,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    sent_at         TEXT
)
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _ensure(db: aiosqlite.Connection) -> None:
    await db.execute(_SCHEMA)


async def enqueue(kind: str, text: str, *, severity: str = "critical",
                  dedupe_key: Optional[str] = None, dedupe_seconds: int = DEDUPE_SECONDS) -> bool:
    """Queue an alert. Returns True if queued, False if deduplicated or on any failure (never raises)."""
    try:
        now = _now()
        async with database.connect() as db:
            await _ensure(db)
            if dedupe_key:
                since = (now - timedelta(seconds=dedupe_seconds)).isoformat()
                async with db.execute(
                    "SELECT 1 FROM ops_alert_outbox WHERE dedupe_key = ? AND created_at >= ? LIMIT 1",
                    (dedupe_key, since),
                ) as cur:
                    if await cur.fetchone():
                        return False
            await db.execute(
                "INSERT INTO ops_alert_outbox (id, kind, severity, text, dedupe_key, next_attempt_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, kind, severity, text[:1000], dedupe_key, now.isoformat(), now.isoformat()),
            )
            await db.commit()
        return True
    except Exception:  # noqa: BLE001
        logger.exception("could not queue ops alert %s", kind)
        return False


def _channels(notif: Dict[str, Any]) -> List[str]:
    out = []
    if notif.get("telegram_enabled") and notif.get("telegram_bot_token") and notif.get("telegram_chat_id"):
        out.append("telegram")
    if notif.get("email_enabled") and notif.get("email_to"):
        out.append("email")
    return out


async def _send_channel(channel: str, row: Dict[str, Any], notif: Dict[str, Any]) -> None:
    label = f"[{row['severity'].upper()}] {row['kind']}"
    if channel == "telegram":
        await telegram_notifier.send(
            text=f"🚨 <b>{html.escape(label)}</b>\n{html.escape(row['text'])}",
            bot_token=notif["telegram_bot_token"], chat_id=notif["telegram_chat_id"])
    else:
        email_notifier.configure(notif)
        await email_notifier.send(
            subject=f"Nautilus ops alert: {row['kind']}",
            body=f"<h3>{html.escape(label)}</h3><p>{html.escape(row['text'])}</p>", to=notif["email_to"])


async def process_outbox() -> int:
    """One delivery round. Returns the number of alerts fully delivered."""
    now = _now()
    delivered = 0
    notif = (await database.get_settings_raw()).get("notifications", {}) or {}
    channels = _channels(notif)
    async with database.connect() as db:
        await _ensure(db)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM ops_alert_outbox WHERE status = 'pending' AND next_attempt_at <= ? "
            "ORDER BY created_at LIMIT 50", (now.isoformat(),),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        if rows and not channels:
            logger.warning("%d ops alert(s) pending but no notification channel is configured", len(rows))
        for row in rows:
            if not channels:
                break  # nothing to try: keep them pending, do not burn attempts
            done = set(json.loads(row["sent_channels"]))
            error = None
            for ch in channels:
                if ch in done:
                    continue
                try:
                    await _send_channel(ch, row, notif)
                    done.add(ch)
                except Exception as exc:  # noqa: BLE001
                    error = f"{ch}: {type(exc).__name__}: {str(exc)[:200]}"
            if error is None:
                await db.execute(
                    "UPDATE ops_alert_outbox SET status='sent', sent_at=?, sent_channels=?, last_error=NULL WHERE id=?",
                    (now.isoformat(), json.dumps(sorted(done)), row["id"]))
                delivered += 1
            else:
                attempts = row["attempts"] + 1
                status = "dead" if attempts >= MAX_ATTEMPTS else "pending"
                delay = min(MAX_BACKOFF, BASE_BACKOFF * 2 ** (attempts - 1))
                await db.execute(
                    "UPDATE ops_alert_outbox SET attempts=?, status=?, sent_channels=?, last_error=?, "
                    "next_attempt_at=? WHERE id=?",
                    (attempts, status, json.dumps(sorted(done)), error,
                     (now + timedelta(seconds=delay)).isoformat(), row["id"]))
                if status == "dead":
                    logger.error("ops alert %s (%s) gave up after %d attempts: %s",
                                 row["id"], row["kind"], attempts, error)
        cutoff = (now - timedelta(days=RETENTION_DAYS)).isoformat()
        await db.execute("DELETE FROM ops_alert_outbox WHERE status = 'sent' AND created_at < ?", (cutoff,))
        await db.commit()
    return delivered


async def list_outbox(limit: int = 100) -> List[Dict[str, Any]]:
    async with database.connect() as db:
        await _ensure(db)
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM ops_alert_outbox ORDER BY created_at DESC LIMIT ?", (limit,)) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def sender_loop() -> None:
    while True:
        try:
            await process_outbox()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("ops alert sender round failed")
        await asyncio.sleep(SEND_INTERVAL)
