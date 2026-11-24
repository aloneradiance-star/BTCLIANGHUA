"""Smoke test actual Alembic migrations without any network or exchange credentials."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, create_engine, inspect

from baqs.models import Candle, RegimeSnapshot
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
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert {"alembic_version", "candles", "regime_snapshots"} <= tables
        for table in (Candle.__table__, RegimeSnapshot.__table__):
            migrated_names = {item["name"] for item in inspector.get_check_constraints(table.name)}
            model_names = {c.name for c in table.constraints if isinstance(c, CheckConstraint)}
            assert migrated_names == model_names
        engine.dispose()

        command.downgrade(config, "base")
        engine = create_engine(url)
        tables = set(inspect(engine).get_table_names())
        assert "candles" not in tables
        assert "regime_snapshots" not in tables
        engine.dispose()
    finally:
        get_settings.cache_clear()
