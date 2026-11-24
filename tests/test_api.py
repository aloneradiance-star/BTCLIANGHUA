"""Read-only API and no-fabricated-market-data tests."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from baqs.api import create_app
from baqs.models import Candle, RegimeSnapshot
from baqs.settings import Settings


def client_for(engine: Engine) -> TestClient:
    return TestClient(
        create_app(
            settings=Settings(_env_file=None, database_url="sqlite:///:memory:"),
            engine=engine,
        )
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


def test_regimes_are_scoped_to_requested_timeframe(db_engine: Engine) -> None:
    open_time = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    durations = {
        "1m": timedelta(minutes=1),
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
    }
    expected_regimes = {
        "1m": "TRANSITION",
        "5m": "BULL_TREND",
        "15m": "RANGE",
        "1h": "BEAR_TREND",
    }

    with Session(db_engine) as session:
        for timeframe, duration in durations.items():
            close_time = open_time + duration
            session.add(
                Candle(
                    symbol="BTCUSDT",
                    timeframe=timeframe,
                    open_time=open_time,
                    close_time=close_time,
                    open=Decimal("100"),
                    high=Decimal("101"),
                    low=Decimal("99"),
                    close=Decimal("100.5"),
                    volume=Decimal("1"),
                    is_final=True,
                    source="FIXTURE",
                )
            )
            session.add(
                RegimeSnapshot(
                    symbol="BTCUSDT",
                    timeframe=timeframe,
                    candle_open_time=open_time,
                    candle_close_time=close_time,
                    regime=expected_regimes[timeframe],
                    features={"timeframe_probe": timeframe},
                    reasons=["TIMEFRAME_FIXTURE"],
                    classifier_version=f"test-{timeframe}",
                )
            )
        session.commit()

    client = client_for(db_engine)
    for timeframe, expected_regime in expected_regimes.items():
        response = client.get(f"/api/v1/market/regimes?timeframe={timeframe}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["timeframe"] == timeframe
        btc = payload["symbols"]["BTCUSDT"]
        assert btc["timeframe"] == timeframe
        assert btc["regime"] == expected_regime
        assert btc["features"]["timeframe_probe"] == timeframe
        eth = payload["symbols"]["ETHUSDT"]
        assert eth["status"] == "UNAVAILABLE"
        assert eth["timeframe"] == timeframe

    default_payload = client.get("/api/v1/market/regimes").json()
    assert default_payload["timeframe"] == "5m"
    assert default_payload["symbols"]["BTCUSDT"]["regime"] == "BULL_TREND"


def test_regimes_reject_unsupported_timeframe(db_engine: Engine) -> None:
    response = client_for(db_engine).get("/api/v1/market/regimes?timeframe=2m")
    assert response.status_code == 422


def test_candles_only_returns_finalized(db_engine: Engine) -> None:
    moment = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    with Session(db_engine) as session:
        session.add(
            Candle(
                symbol="BTCUSDT",
                timeframe="5m",
                open_time=moment,
                close_time=datetime(2026, 11, 17, 12, 5, tzinfo=UTC),
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
                open_time=datetime(2026, 11, 17, 12, 5, tzinfo=UTC),
                close_time=datetime(2026, 11, 17, 12, 10, tzinfo=UTC),
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


def test_regime_timeframe_default_is_documented(db_engine: Engine) -> None:
    schema = client_for(db_engine).get("/openapi.json").json()
    parameters = schema["paths"]["/api/v1/market/regimes"]["get"]["parameters"]
    timeframe = next(parameter for parameter in parameters if parameter["name"] == "timeframe")
    assert timeframe["schema"]["default"] == "5m"
    assert "Supported values: 1m, 5m, 15m, 1h" in timeframe["description"]
