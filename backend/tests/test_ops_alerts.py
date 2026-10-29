"""Operational alert outbox: durability, dedupe, retry/backoff, no-channel behaviour."""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import database  # noqa: E402
import ops_alerts  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "ops.db")
    asyncio.run(database.init_db())


def _configure(monkeypatch, notif):
    async def fake():
        return {"notifications": notif}
    monkeypatch.setattr(database, "get_settings_raw", fake)


TG = {"telegram_enabled": True, "telegram_bot_token": "tok", "telegram_chat_id": "1"}


def _force_due():
    async def go():
        async with database.connect() as c:
            await c.execute("UPDATE ops_alert_outbox SET next_attempt_at = ?",
                            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
            await c.commit()
    asyncio.run(go())


def test_enqueue_dedupes_within_window(db):
    assert asyncio.run(ops_alerts.enqueue("k", "t", dedupe_key="x")) is True
    assert asyncio.run(ops_alerts.enqueue("k", "t", dedupe_key="x")) is False
    assert asyncio.run(ops_alerts.enqueue("k", "t", dedupe_key="y")) is True
    assert len(asyncio.run(ops_alerts.list_outbox())) == 2


def test_enqueue_never_raises(monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", Path("/nonexistent/dir/x.db"))
    assert asyncio.run(ops_alerts.enqueue("k", "t")) is False


def test_delivery_marks_sent_and_escapes_html(db, monkeypatch):
    _configure(monkeypatch, TG)
    sent = []

    async def fake_send(text, bot_token, chat_id):
        sent.append(text)
    monkeypatch.setattr(ops_alerts.telegram_notifier, "send", fake_send)
    asyncio.run(ops_alerts.enqueue("order_rejected", "bad <b>order</b>"))
    assert asyncio.run(ops_alerts.process_outbox()) == 1
    assert "&lt;b&gt;order" in sent[0]
    assert asyncio.run(ops_alerts.list_outbox())[0]["status"] == "sent"


def test_failure_backs_off_then_retries_then_dies(db, monkeypatch):
    _configure(monkeypatch, TG)
    calls = {"n": 0}

    async def failing(text, bot_token, chat_id):
        calls["n"] += 1
        raise RuntimeError("Telegram API returned HTTP 502")
    monkeypatch.setattr(ops_alerts.telegram_notifier, "send", failing)
    asyncio.run(ops_alerts.enqueue("k", "t"))
    assert asyncio.run(ops_alerts.process_outbox()) == 0
    row = asyncio.run(ops_alerts.list_outbox())[0]
    assert row["status"] == "pending" and row["attempts"] == 1 and "502" in row["last_error"]
    # not due yet: no second attempt
    asyncio.run(ops_alerts.process_outbox())
    assert calls["n"] == 1
    for _ in range(ops_alerts.MAX_ATTEMPTS):
        _force_due()
        asyncio.run(ops_alerts.process_outbox())
    row = asyncio.run(ops_alerts.list_outbox())[0]
    assert row["status"] == "dead" and row["attempts"] == ops_alerts.MAX_ATTEMPTS


def test_partial_channel_failure_only_retries_failed_channel(db, monkeypatch):
    _configure(monkeypatch, {**TG, "email_enabled": True, "email_to": "a@b.c"})
    tg, mail = [], {"fail": True}

    async def tg_send(text, bot_token, chat_id):
        tg.append(text)

    async def mail_send(subject, body, to):
        if mail["fail"]:
            raise RuntimeError("smtp down")
    monkeypatch.setattr(ops_alerts.telegram_notifier, "send", tg_send)
    monkeypatch.setattr(ops_alerts.email_notifier, "send", mail_send)
    asyncio.run(ops_alerts.enqueue("k", "t"))
    asyncio.run(ops_alerts.process_outbox())
    mail["fail"] = False
    _force_due()
    assert asyncio.run(ops_alerts.process_outbox()) == 1
    assert len(tg) == 1  # telegram not re-sent
    row = asyncio.run(ops_alerts.list_outbox())[0]
    assert row["status"] == "sent" and json.loads(row["sent_channels"]) == ["email", "telegram"]


def test_no_channel_keeps_pending_without_burning_attempts(db, monkeypatch):
    _configure(monkeypatch, {})
    asyncio.run(ops_alerts.enqueue("k", "t"))
    asyncio.run(ops_alerts.process_outbox())
    row = asyncio.run(ops_alerts.list_outbox())[0]
    assert row["status"] == "pending" and row["attempts"] == 0


def test_disarm_queues_alert(db):
    import trading_control
    asyncio.run(trading_control.disarm("tester", "unit test"))
    kinds = [r["kind"] for r in asyncio.run(ops_alerts.list_outbox())]
    assert "trading_disarmed" in kinds
