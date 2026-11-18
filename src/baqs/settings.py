"""Strict safety-first Phase 1 runtime settings."""

from functools import lru_cache
from typing import Literal, Self

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine.url import make_url


class Settings(BaseSettings):
    """No LIVE, TESTNET or private exchange functionality is permitted in Phase 1."""

    model_config = SettingsConfigDict(
        env_prefix="BAQS_", env_file=".env", extra="ignore", case_sensitive=False
    )

    environment: Literal["PAPER"] = "PAPER"
    live_enabled: bool = False
    database_url: str = "postgresql+psycopg://baqs:baqs_dev_only@localhost:5432/baqs"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        try:
            url = make_url(value)
        except Exception as exc:
            raise ValueError("Invalid SQLAlchemy database URL") from exc
        if url.drivername not in {"sqlite", "sqlite+pysqlite", "postgresql+psycopg"}:
            raise ValueError("Only SQLite test and PostgreSQL psycopg URLs are accepted")
        if url.drivername.startswith("sqlite") and url.database is None:
            raise ValueError("SQLite needs an explicit database")
        return value

    @model_validator(mode="after")
    def prohibit_live_trading(self) -> Self:
        if self.live_enabled:
            raise ValueError("Phase 1 forbids live trading, regardless of environment")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
