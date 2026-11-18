"""Read-only API and no-fabricated-market-data tests."""
from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from baqs.api import create_app
from baqs.models import Candle
from baqs.settings import Settings


def client_for(engine: Engine) -> TestClient:
    return TestClient(
        create_app(settings=Settings(_env_file=None, database_url="sqlite:///:memory:"), engine=engine)
    )


def test_health_reports_no_execution(db_engine: Engine) -> None:
    client = client_for(db_engine)
    data = client.get("/api/v1/health").json()
    assert data["database"] == "CONNECTED"
    assert data["execution_enabled"] is False
    assert data["market_data"] == "NOT_IMPLEMENTED"


def test_absent_regimes_unavailable(db_engine: Engine) -> None:
    client = client_for(db_engine)
    result = client.get("/api/v1/market/regimes")
    assert result.status_code == 200
    symbols = result.json()["symbols"]
    assert symbols["BTCUSDT"]["status"] == "UNAVAILABLE"
    assert symbols["ETHUSDT"]["status"] == "UNAVAILABLE"


def test_candles_only_returns_finalized(db_engine: Engine) -> None:
    moment = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    with Session(db_engine) as session:
        session.add(
            Candle(
                symbol="BTCUSDT",
                timeframe="5m",
                open_time=moment,
                close_time=datetime(2026, 11, 17, 12, 5, tzinfo=timezone.utc),
                open=Decimal("95000"),
                high=Decimal("95010"),
                low=Decimal("94990"),
                close=Decimal("95002"),
                volume=Decimal("1.0"),
                is_final=True,
                source="FIXTURE",
            )
        )
        session.add(
            Candle(
                symbol="BTCUSDT",
                timeframe="5m",
                open_time=datetime(2026, 11, 17, 12, 5, tzinfo=timezone.utc),
                close_time=datetime(2026, 11, 17, 12, 10, tzinfo=timezone.utc),
                open=Decimal("95002"),
                high=Decimal("95006"),
                low=Decimal("95001"),
                close=Decimal("95005"),
                volume=Decimal("0.8"),
                is_final=False,
                source="FIXTURE",
            )
        )
        session.commit()
    client = client_for(db_engine)
    result = client.get("/api/v1/market/candles?symbol=BTCUSDT&timeframe=5m")
    assert result.status_code == 200
    items = result.json()["items"]
    assert len(items) == 1
    assert items[0]["close"] == "95002.000000000000"


def test_invalid_queries_are_rejected(db_engine: Engine) -> None:
    client = client_for(db_engine)
    assert client.get("/api/v1/market/candles?symbol=DOGEUSDT").status_code == 422
    assert client.get("/api/v1/market/candles?limit=501").status_code == 422


def test_no_write_or_trading_routes(db_engine: Engine) -> None:
    spec = client_for(db_engine).get("/openapi.json").json()
    for path, operations in spec["paths"].items():
        assert "order" not in path.lower()
        assert "account" not in path.lower()
        assert set(operations).issubset({"get"})
