"""Structured JSON logging for BAQS application records.

Only the baqs logger namespace is configured here. Uvicorn's access and
error loggers retain the logging configuration supplied by the ASGI server.
"""

import json
import logging
from datetime import UTC, datetime
from typing import TextIO


class JsonFormatter(logging.Formatter):
    """Render application records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, str] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class JsonStreamHandler(logging.StreamHandler[TextIO]):
    """Marker handler so repeated initialization does not add duplicates."""


def configure_logging(level: str = "INFO") -> None:
    """Configure JSON output for BAQS logs without resetting server loggers."""
    normalized_level = level.upper()
    numeric_level = logging.getLevelName(normalized_level)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Unsupported logging level: {level}")

    logger = logging.getLogger("baqs")
    logger.setLevel(numeric_level)
    logger.propagate = False

    handlers = [handler for handler in logger.handlers if isinstance(handler, JsonStreamHandler)]
    if handlers:
        handlers[0].setLevel(numeric_level)
        for duplicate in handlers[1:]:
            logger.removeHandler(duplicate)
            duplicate.close()
        return

    handler = JsonStreamHandler()
    handler.setLevel(numeric_level)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
