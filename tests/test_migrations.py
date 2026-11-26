"""Smoke test actual Alembic migrations without any network or exchange credentials."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, inspect, select, text

from baqs.models import FundingRate, MarketStreamState
from baqs.settings import get_settings


@pytest.mark.parametrize("existing_revision", [None, "0002_market_stream", "0002_backtest_funding"])
def test_migration_upgrade_and_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_revision: str | None
) -> None:
    db_path = tmp_path / "migration-smoke.sqlite"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("BAQS_DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        config_path = Path(__file__).resolve().parents[1] / "alembic.ini"
        config = Config(str(config_path))
        engine = create_engine(url)
        if existing_revision is not None:
            command.upgrade(config, existing_revision)
            with engine.begin() as conn:
                if existing_revision == "0002_market_stream":
                    conn.execute(insert(MarketStreamState).values(id=1, last_message_ms=1234))
                else:
                    conn.execute(
                        insert(FundingRate).values(
                            symbol="BTCUSDT",
                            funding_time=datetime(1970, 1, 1, 0, 2, tzinfo=UTC),
                            rate_type="Regular",
                            funding_rate=Decimal("0.001"),
                            mark_price=Decimal(101),
                            source="TEST",
                        )
                    )
        command.upgrade(config, "head")

        tables = set(inspect(engine).get_table_names())
        assert {
            "alembic_version",
            "candles",
            "regime_snapshots",
            "market_stream_state",
            "funding_rates",
            "funding_coverage",
        } <= tables
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
                "0003_public_backtest"
            )
            if existing_revision == "0002_market_stream":
                assert conn.scalar(select(MarketStreamState.last_message_ms)) == 1234
            if existing_revision == "0002_backtest_funding":
                assert conn.scalar(select(FundingRate.mark_price)) == Decimal(101)
        engine.dispose()

        command.downgrade(config, "0001_public")
        engine = create_engine(url)
        tables = set(inspect(engine).get_table_names())
        assert "market_stream_state" not in tables
        assert "funding_rates" not in tables
        assert "funding_coverage" not in tables
        assert {"candles", "regime_snapshots"} <= tables
        engine.dispose()
        command.upgrade(config, "head")

        command.downgrade(config, "base")
        engine = create_engine(url)
        tables = set(inspect(engine).get_table_names())
        assert "candles" not in tables
        assert "market_stream_state" not in tables
        assert "regime_snapshots" not in tables
        assert "funding_rates" not in tables
        assert "funding_coverage" not in tables
        engine.dispose()
    finally:
        get_settings.cache_clear()
