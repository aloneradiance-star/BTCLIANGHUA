"""Runtime structured logging and server-logger isolation tests."""

import json
import logging

from fastapi.testclient import TestClient

from baqs.api import create_app
from baqs.logging_config import JsonFormatter, JsonStreamHandler, configure_logging
from baqs.settings import Settings


def test_application_startup_emits_json_and_preserves_uvicorn(db_engine, capsys) -> None:
    uvicorn_loggers = (
        logging.getLogger("uvicorn.error"),
        logging.getLogger("uvicorn.access"),
    )
    original_handlers = {logger.name: list(logger.handlers) for logger in uvicorn_loggers}
    app = create_app(
        settings=Settings(_env_file=None, database_url="sqlite:///:memory:"),
        engine=db_engine,
    )

    with TestClient(app):
        pass

    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.err.splitlines() if line.startswith("{")]
    startup = next(record for record in records if record["message"] == "application_started")
    assert startup["timestamp"].endswith("+00:00")
    assert startup["level"] == "INFO"
    assert startup["logger"] == "baqs.api"
    assert logging.getLogger("baqs").propagate is False
    assert all(logger.handlers == original_handlers[logger.name] for logger in uvicorn_loggers)


def test_configure_logging_is_idempotent_and_does_not_reset_uvicorn() -> None:
    uvicorn_loggers = (
        logging.getLogger("uvicorn.error"),
        logging.getLogger("uvicorn.access"),
    )
    original_handlers = {logger.name: list(logger.handlers) for logger in uvicorn_loggers}

    configure_logging("INFO")
    configure_logging("DEBUG")

    baqs_handlers = [
        handler
        for handler in logging.getLogger("baqs").handlers
        if isinstance(handler, JsonStreamHandler)
    ]
    assert len(baqs_handlers) == 1
    assert baqs_handlers[0].level == logging.DEBUG
    assert isinstance(baqs_handlers[0].formatter, JsonFormatter)
    assert logging.getLogger("baqs").propagate is False
    assert all(logger.handlers == original_handlers[logger.name] for logger in uvicorn_loggers)
