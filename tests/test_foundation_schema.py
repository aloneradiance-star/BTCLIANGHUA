"""Foundation readiness and schema compatibility checks with local test data."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import CheckConstraint, Engine, create_engine, inspect

from baqs.api import create_app
from baqs.models import Candle, RegimeSnapshot
from baqs.settings import Settings, get_settings


@pytest.mark.parametrize("drop_candles", [False, True], ids=["partial_schema", "empty_database"])
def test_health_rejects_missing_schema(db_engine: Engine, drop_candles: bool) -> None:
    RegimeSnapshot.__table__.drop(db_engine)
    if drop_candles:
        Candle.__table__.drop(db_engine)

    settings = Settings(_env_file=None, database_url="sqlite:///:memory:")
    client = TestClient(create_app(settings=settings, engine=db_engine))
    response = client.get("/api/v1/health")
    assert response.status_code == 503
    assert response.json()["status"] == "DEGRADED"
    assert response.json()["database"] == "UNAVAILABLE"
    assert response.json()["execution_enabled"] is False


def test_initial_migration_check_names_match_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{tmp_path / 'foundation-schema.sqlite'}"
    monkeypatch.setenv("BAQS_DATABASE_URL", url)
    get_settings.cache_clear()
    engine = create_engine(url)
    try:
        migrations = Path(__file__).resolve().parents[1] / "migrations"
        config = Config()
        config.set_main_option("script_location", str(migrations))
        command.upgrade(config, "0001_public")

        inspector = inspect(engine)
        for table in (Candle.__table__, RegimeSnapshot.__table__):
            migrated_names = {item["name"] for item in inspector.get_check_constraints(table.name)}
            model_names = {c.name for c in table.constraints if isinstance(c, CheckConstraint)}
            assert migrated_names == model_names
    finally:
        engine.dispose()
        get_settings.cache_clear()
