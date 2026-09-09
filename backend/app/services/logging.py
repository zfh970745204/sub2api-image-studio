from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_context: ContextVar[str | None] = ContextVar("user_id", default=None)
job_id_context: ContextVar[str | None] = ContextVar("job_id", default=None)
ip_hash_context: ContextVar[str | None] = ContextVar("ip_hash", default=None)
user_agent_context: ContextVar[str | None] = ContextVar("user_agent", default=None)

SECRET_PATTERN = re.compile(
    r"(?i)(authorization|api[_-]?key|password|secret|token|cookie)"
    r"(\s*[:=]\s*|\s+)(bearer\s+)?([^\s,;]+)"
)


def sanitize_log_text(value: object) -> str:
    return SECRET_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", str(value)
    )


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": sanitize_log_text(record.getMessage()),
            "request_id": getattr(record, "request_id", None) or request_id_context.get(),
            "user_id": getattr(record, "user_id", None) or user_id_context.get(),
            "job_id": getattr(record, "job_id", None) or job_id_context.get(),
            "operation": getattr(record, "operation", None),
            "duration_ms": getattr(record, "duration_ms", None),
            "status": getattr(record, "status", None),
        }
        if record.exc_info:
            payload["exception"] = sanitize_log_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True


def bind_request_context(request_id: str) -> Token[str | None]:
    return request_id_context.set(request_id)


def reset_request_context(token: Token[str | None]) -> None:
    request_id_context.reset(token)


def bind_security_context(
    ip_hash: str, user_agent: str
) -> tuple[Token[str | None], Token[str | None]]:
    return ip_hash_context.set(ip_hash), user_agent_context.set(user_agent[:500])


def reset_security_context(tokens: tuple[Token[str | None], Token[str | None]]) -> None:
    ip_hash_context.reset(tokens[0])
    user_agent_context.reset(tokens[1])
