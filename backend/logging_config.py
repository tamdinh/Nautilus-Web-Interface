"""Structured JSON logging with a per-request id (stdlib only)."""

import contextvars
import json
import logging
import os
import uuid
from datetime import datetime, timezone

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """JSON logs unless LOG_FORMAT=text. Safe to call more than once."""
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    root.setLevel(level)
    if os.getenv("LOG_FORMAT", "json").lower() == "text":
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.handlers[:] = [handler]
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True


def new_request_id(incoming: str | None) -> str:
    """Reuse a sane client/proxy id, otherwise generate one."""
    if incoming and len(incoming) <= 64 and incoming.replace("-", "").isalnum():
        return incoming
    return uuid.uuid4().hex
