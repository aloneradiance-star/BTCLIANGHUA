"""Public funding pagination, immutable persistence and backtest-readiness tests."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, update

from baqs.api import create_app
from baqs.market_data.funding import FundingStore
from baqs.market_data.funding_history import PublicFundingBackfill
from baqs.market_data.rest import PublicFuturesREST
from baqs.market_data.schema import (
    FundingConflict,
    FundingGap,
    FundingRateRecord,
    InvalidCandle,
)
from baqs.models import FundingRate
from baqs.settings import Settings

START = 60_000
END = 240_000
SERVER = 300_000


def event(
    timestamp: int,
    *,
    rate: str = ".001",
    mark: str = "100",
    rate_type: str = "Regular",
) -> FundingRateRecord:
    return FundingRateRecord("BTCUSDT", timestamp, Decimal(rate), Decimal(mark), rate_type)


def row(
    timestamp: int,
    *,
    rate: str = ".001",
    mark: str = "100",
    rate_type: str = "Regular",
) -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "fundingRate": rate,
        "fundingTime": timestamp,
        "markPrice": mark,
        "rateType": rate_type,
    }


def test_funding_rest_paginates_inclusive_api_with_last_time_plus_one_ms() -> None:
    calls: list[tuple[int, int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": SERVER})
        assert request.url.path == "/fapi/v1/fundingRate"
        start = int(request.url.params["startTime"])
        end = int(request.url.params["endTime"])
        limit = int(request.url.params["limit"])
        calls.append((start, end, limit))
        if start == START and end == END - 1:
            return httpx.Response(200, json=[row(START), row(120_000)])
        if start == 120_000 and end == 120_000:
            return httpx.Response(200, json=[row(120_000)])
        if start == 120_001 and end == END - 1:
            return httpx.Response(200, json=[row(180_000, rate="-.001", mark="99")])
        raise AssertionError(f"Unexpected funding page: {request.url}")

    async def run() -> None:
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            events = await PublicFuturesREST(client).fetch_funding_range(
                "BTCUSDT", START, END, page_size=2, max_pages=5, pause_seconds=0
            )
        assert [item.funding_time_ms for item in events] == [START, 120_000, 180_000]
        assert events[0].mark_price == Decimal(100)
        assert events[-1].funding_rate == Decimal("-.001")
        assert calls == [
            (START, END - 1, 2),
            (120_000, 120_000, 1000),
            (120_001, END - 1, 2),
        ]

    asyncio.run(run())


def test_funding_rest_rejects_missing_mark_price() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": SERVER})
        return httpx.Response(200, json=[{
            "symbol": "BTCUSDT",
            "fundingRate": ".001",
            "fundingTime": START,
            "rateType": "Regular",
        }])

    async def run() -> None:
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(InvalidCandle):
                await PublicFuturesREST(client).fetch_funding_range(
                    "BTCUSDT", START, END, pause_seconds=0
                )

    asyncio.run(run())


def test_funding_rest_fails_closed_when_page_splits_same_timestamp() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": SERVER})
        start = int(request.url.params["startTime"])
        if start == START:
            return httpx.Response(200, json=[row(START), row(120_000)])
        if start == 120_000:
            return httpx.Response(
                200,
                json=[
                    row(120_000),
                    row(120_000, rate=".002", rate_type="Special"),
                ],
            )
        raise AssertionError("Unexpected funding request")

    async def run() -> None:
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(FundingGap, match="split events"):
                await PublicFuturesREST(client).fetch_funding_range(
                    "BTCUSDT", START, END, page_size=2, pause_seconds=0
                )

    asyncio.run(run())


def test_funding_store_preserves_regular_and_special_same_time_events(db_engine: Engine) -> None:
    store = FundingStore(db_engine)
    events = [
        event(120_000, rate=".001", mark="100", rate_type="Regular"),
        event(120_000, rate=".0002", mark="101", rate_type="Special"),
        event(180_000, rate="-.001", mark="99"),
    ]
    assert store.upsert_many(events) == ["CREATED", "CREATED", "CREATED"]
    assert store.upsert_many(events) == ["DUPLICATE", "DUPLICATE", "DUPLICATE"]
    with pytest.raises(FundingConflict):
        store.upsert(replace(events[0], funding_rate=Decimal(".002")))

    assert store.complete_window("BTCUSDT", START, END, events) == "CREATED"
    readiness = store.coverage_readiness("BTCUSDT", START, END)
    assert readiness["status"] == "READY"
    assert readiness["event_count"] == 3
    assert readiness["expected_timestamps_ms"] == [120_000, 120_000, 180_000]
    snapshot = store.load_window("BTCUSDT", START, END)
    assert snapshot.complete
    assert [item.rate_type for item in snapshot.events] == ["Regular", "Special", "Regular"]


def test_funding_manifest_detects_tampered_persisted_rows(db_engine: Engine) -> None:
    from sqlalchemy.orm import Session

    store = FundingStore(db_engine)
    records = [event(120_000), event(180_000, rate="-.001", mark="99")]
    store.upsert_many(records)
    store.complete_window("BTCUSDT", START, END, records)
    with Session(db_engine) as session:
        session.execute(
            update(FundingRate)
            .where(
                FundingRate.symbol == "BTCUSDT",
                FundingRate.funding_time == datetime.fromtimestamp(180_000 / 1000, tz=UTC),
            )
            .values(mark_price=Decimal(98))
        )
        session.commit()
    assert store.coverage_readiness("BTCUSDT", START, END)["status"] == "GAP"


def test_funding_window_backfill_is_idempotent_and_certifies_complete_data(
    db_engine: Engine,
) -> None:
    records = [event(120_000), event(180_000, rate="-.001", mark="99")]

    class StubReader:
        async def server_time_ms(self) -> int:
            return END

        async def fetch_funding_range(
            self,
            symbol: str,
            start_ms: int,
            end_ms: int,
            *,
            page_size: int = 1000,
            max_pages: int = 100,
            pause_seconds: float = 1.0,
        ) -> list[FundingRateRecord]:
            assert (symbol, start_ms, end_ms) == ("BTCUSDT", START, END)
            return records

    async def run() -> None:
        store = FundingStore(db_engine)
        service = PublicFundingBackfill(
            StubReader(), store, page_size=1000, pause_seconds=0
        )
        first = await service.run("BTCUSDT", START, END)
        assert first.readiness == "READY"
        assert first.created_events == 2
        second = await service.run("BTCUSDT", START, END)
        assert second.created_events == 0
        assert second.reused_events == 2

    asyncio.run(run())


def test_funding_backfill_rejects_out_of_window_records(db_engine: Engine) -> None:
    class BadReader:
        async def server_time_ms(self) -> int:
            return END

        async def fetch_funding_range(
            self,
            symbol: str,
            start_ms: int,
            end_ms: int,
            *,
            page_size: int = 1000,
            max_pages: int = 100,
            pause_seconds: float = 1.0,
        ) -> list[FundingRateRecord]:
            return [event(END)]

    async def run() -> None:
        with pytest.raises(FundingGap, match="escaped"):
            await PublicFundingBackfill(BadReader(), FundingStore(db_engine)).run(
                "BTCUSDT", START, END
            )
        assert FundingStore(db_engine).coverage_readiness("BTCUSDT", START, END)[
            "status"
        ] == "NO_COVERAGE"

    asyncio.run(run())


def test_backtest_readiness_api_is_read_only_and_fails_closed(db_engine: Engine) -> None:
    app = create_app(
        settings=Settings(_env_file=None, database_url="sqlite:///:memory:"),
        engine=db_engine,
    )
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/market/backtest-readiness",
            params={"start_ms": START, "end_ms": START + 60_000},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["execution_enabled"] is False
        assert payload["symbols"]["BTCUSDT"]["status"] == "UNAVAILABLE"
        assert payload["symbols"]["BTCUSDT"]["execution_1m"]["status"] == "NO_DATA"
        assert payload["symbols"]["BTCUSDT"]["funding"]["status"] == "NO_COVERAGE"
        assert client.get(
            "/api/v1/market/backtest-readiness",
            params={"start_ms": START + 1, "end_ms": START + 60_000},
        ).status_code == 422
