"""Public funding pagination, immutable persistence and backtest-readiness tests."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

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
    MarketDataError,
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


@pytest.mark.parametrize("last_offset_ms", [0, 1])
def test_funding_rest_paginates_inclusive_api_with_last_time_plus_one_ms(
    last_offset_ms: int,
) -> None:
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
            return httpx.Response(
                200, json=[row(180_000 + last_offset_ms, rate="-.001", mark="99")]
            )
        raise AssertionError(f"Unexpected funding page: {request.url}")

    async def run() -> None:
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            events = await PublicFuturesREST(client).fetch_funding_range(
                "BTCUSDT", START, END, page_size=2, max_pages=5, pause_seconds=0
            )
        assert [item.funding_time_ms for item in events] == [
            START,
            120_000,
            180_000 + last_offset_ms,
        ]
        assert events[0].mark_price == Decimal(100)
        assert events[-1].funding_rate == Decimal("-.001")
        assert calls == [
            (START, END - 1, 2),
            (120_000, 120_000, 3),
            (120_001, END - 1, 2),
        ]

    asyncio.run(run())


@pytest.mark.parametrize(("page_size", "page_interval"), [(200, 0.65), (201, 3.1)])
def test_funding_limit_applies_to_page_boundary_retry_and_next_symbol(
    monkeypatch: pytest.MonkeyPatch, page_size: int, page_interval: float
) -> None:
    now = 100.0
    requests: list[tuple[int, float]] = []
    boundary = START + page_size - 1

    async def sleep(delay: float) -> None:
        nonlocal now
        now += delay

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": SERVER})
        assert request.url.path == "/fapi/v1/fundingRate"
        params = request.url.params
        limit = int(params["limit"])
        requests.append((limit, now - 100.0))
        if params["symbol"] == "ETHUSDT":
            return httpx.Response(200, json=[])
        start, end = int(params["startTime"]), int(params["endTime"])
        if start == START:
            return httpx.Response(200, json=[row(ts) for ts in range(START, boundary + 1)])
        if start == end == boundary:
            return httpx.Response(200, json=[row(boundary)])
        assert start == boundary + 1 and end == END - 1
        if len(requests) == 3:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json=[row(boundary + 1)])

    async def run() -> None:
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com", transport=httpx.MockTransport(handler)
        ) as client:
            rest = PublicFuturesREST(client)
            events = await rest.fetch_funding_range(
                "BTCUSDT", START, END, page_size=page_size, pause_seconds=0
            )
            assert len(events) == page_size + 1
            assert await rest.fetch_funding_range("ETHUSDT", START, END, page_size=page_size) == []

    monkeypatch.setattr("baqs.market_data.rest.monotonic", lambda: now)
    monkeypatch.setattr("baqs.market_data.rest.asyncio.sleep", sleep)
    asyncio.run(run())

    boundary_request = page_interval
    next_page = boundary_request + 0.65
    retry = next_page + max(2, page_interval)
    assert [limit for limit, _ in requests] == [page_size, 3, page_size, page_size, page_size]
    assert [sent_at for _, sent_at in requests] == pytest.approx(
        [0, boundary_request, next_page, retry, retry + page_interval]
    )


def test_funding_rest_rejects_missing_mark_price() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": SERVER})
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "BTCUSDT",
                    "fundingRate": ".001",
                    "fundingTime": START,
                    "rateType": "Regular",
                }
            ],
        )

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


@pytest.mark.parametrize(("rate", "mark"), [(".002", "100"), (".001", "100.001")])
def test_funding_rest_rejects_changed_values_at_page_boundary(rate: str, mark: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": SERVER})
        start = int(request.url.params["startTime"])
        if start == START:
            return httpx.Response(200, json=[row(START), row(120_000)])
        if start == 120_000:
            return httpx.Response(200, json=[row(120_000, rate=rate, mark=mark)])
        return httpx.Response(200, json=[])

    async def run() -> None:
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(FundingGap, match="boundary.*values"):
                await PublicFuturesREST(client).fetch_funding_range(
                    "BTCUSDT", START, END, page_size=2, pause_seconds=0
                )

    asyncio.run(run())


def test_funding_rest_stops_when_retry_after_exceeds_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    funding_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal funding_requests
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": SERVER})
        funding_requests += 1
        if funding_requests == 1:
            return httpx.Response(429, headers={"Retry-After": "120"})
        return httpx.Response(200, json=[])

    async def no_wait(_delay: float) -> None:
        pass

    async def run() -> None:
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(MarketDataError, match="Retry-After"):
                await PublicFuturesREST(client).fetch_funding_range("BTCUSDT", START, END)

    monkeypatch.setattr("baqs.market_data.rest.asyncio.sleep", no_wait)
    asyncio.run(run())
    assert funding_requests == 1


def test_funding_fingerprint_retains_all_digits_under_caller_decimal_settings() -> None:
    first = event(120_000, rate=".000123456789", mark="95000.123456789012")
    changed = replace(first, mark_price=Decimal("95000.123456789013"))
    expected = first.fingerprint()
    with localcontext() as context:
        context.prec = 6
        assert first.fingerprint() == expected
        assert changed.fingerprint() != expected


def test_submillisecond_funding_timestamp_invalidates_persisted_manifest(db_engine: Engine) -> None:
    store = FundingStore(db_engine)
    record = event(120_000)
    store.upsert(record)
    store.complete_window("BTCUSDT", START, END, [record])
    timestamp = datetime.fromtimestamp(120, tz=UTC)
    with db_engine.begin() as connection:
        connection.execute(
            update(FundingRate)
            .where(FundingRate.symbol == record.symbol, FundingRate.funding_time == timestamp)
            .values(funding_time=timestamp + timedelta(microseconds=1))
        )
    assert store.coverage_readiness("BTCUSDT", START, END)["status"] == "GAP"
    with pytest.raises(FundingGap, match="timestamp"):
        store.load_window("BTCUSDT", START, END)


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
        service = PublicFundingBackfill(StubReader(), store, page_size=1000, pause_seconds=0)
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
        assert (
            FundingStore(db_engine).coverage_readiness("BTCUSDT", START, END)["status"]
            == "NO_COVERAGE"
        )

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
        assert (
            client.get(
                "/api/v1/market/backtest-readiness",
                params={"start_ms": START + 1, "end_ms": START + 60_000},
            ).status_code
            == 422
        )
