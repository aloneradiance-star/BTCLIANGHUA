"""Smoke test actual Alembic migrations without any network or exchange credentials."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from baqs.settings import get_settings


def test_migration_upgrade_and_downgrade(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "migration-smoke.sqlite"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("BAQS_DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        config_path = Path(__file__).resolve().parents[1] / "alembic.ini"
        config = Config(str(config_path))
        command.upgrade(config, "head")

        engine = create_engine(url)
        tables = set(inspect(engine).get_table_names())
        assert {
            "alembic_version",
            "candles",
            "regime_snapshots",
            "funding_rates",
            "funding_coverage",
        } <= tables
        engine.dispose()

        command.downgrade(config, "base")
        engine = create_engine(url)
        tables = set(inspect(engine).get_table_names())
        assert "candles" not in tables
        assert "regime_snapshots" not in tables
        assert "funding_rates" not in tables
        assert "funding_coverage" not in tables
        engine.dispose()
    finally:
        get_settings.cache_clear()
