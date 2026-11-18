"""Phase 1 runtime safety tests."""

import pytest
from pydantic import ValidationError

from baqs.settings import Settings


def test_defaults_are_research_only() -> None:
    settings = Settings(_env_file=None)
    assert settings.environment == "PAPER"
    assert settings.live_enabled is False


def test_live_enabled_rejected() -> None:
    with pytest.raises(ValidationError, match="forbids live"):
        Settings(_env_file=None, live_enabled=True)


def test_other_environments_rejected() -> None:
    for environment in ("LIVE", "TESTNET", "PRODUCTION"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, environment=environment)


def test_invalid_database_url_rejected() -> None:
    for url in ("", "ftp://host/path", "postgresql://otherdriver:password@host/db"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, database_url=url)


def test_environment_variable_cannot_override_live_safety(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAQS_LIVE_ENABLED", "true")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
