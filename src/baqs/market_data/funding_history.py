"""Explicit public historical funding backfill and completeness certification."""

from dataclasses import dataclass
from typing import Protocol

from baqs.market_data.funding import FundingStore
from baqs.market_data.schema import FundingGap, FundingRateRecord, SYMBOLS


class PublicFundingReader(Protocol):
    async def server_time_ms(self) -> int: ...

    async def fetch_funding_range(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
        *,
        page_size: int = 1000,
        max_pages: int = 100,
        pause_seconds: float = 1.0,
    ) -> list[FundingRateRecord]: ...


@dataclass(frozen=True, slots=True)
class FundingBackfillReport:
    symbol: str
    start_ms: int
    end_ms: int
    fetched_events: int
    created_events: int
    reused_events: int
    readiness: str


class PublicFundingBackfill:
    """Persist public funding history; certify only after REST pagination is exhausted."""

    def __init__(
        self,
        reader: PublicFundingReader,
        store: FundingStore,
        *,
        page_size: int = 1000,
        max_pages: int = 100,
        pause_seconds: float = 1.0,
    ) -> None:
        if not 1 <= page_size <= 1000 or not 1 <= max_pages <= 1000:
            raise ValueError("Funding pagination must be bounded to 1..1000")
        if not 0 <= pause_seconds <= 60:
            raise ValueError("Funding request pause must be between 0 and 60 seconds")
        self.reader = reader
        self.store = store
        self.page_size = page_size
        self.max_pages = max_pages
        self.pause_seconds = pause_seconds

    async def run(
        self, symbol: str, start_ms: int, end_ms: int
    ) -> FundingBackfillReport:
        if (
            symbol not in SYMBOLS
            or type(start_ms) is not int
            or type(end_ms) is not int
            or start_ms < 0
            or end_ms <= start_ms
            or start_ms % 60_000
            or end_ms % 60_000
        ):
            raise ValueError("Funding backfill requires a valid 1m-aligned UTC window")
        server_ms = await self.reader.server_time_ms()
        if type(server_ms) is not int or server_ms < 0 or end_ms > server_ms:
            raise FundingGap("Funding backfill window extends beyond Binance server time")

        events = await self.reader.fetch_funding_range(
            symbol,
            start_ms,
            end_ms,
            page_size=self.page_size,
            max_pages=self.max_pages,
            pause_seconds=self.pause_seconds,
        )
        if any(
            event.symbol != symbol or not start_ms <= event.funding_time_ms < end_ms
            for event in events
        ):
            raise FundingGap("Funding REST response escaped the requested symbol/window")
        ordered = sorted(events, key=lambda event: (event.funding_time_ms, event.rate_type))
        outcomes = self.store.upsert_many(ordered)
        self.store.complete_window(symbol, start_ms, end_ms, ordered)
        readiness = self.store.coverage_readiness(symbol, start_ms, end_ms)
        if readiness["status"] != "READY":
            raise FundingGap(f"Funding coverage verification failed: {readiness}")
        return FundingBackfillReport(
            symbol=symbol,
            start_ms=start_ms,
            end_ms=end_ms,
            fetched_events=len(ordered),
            created_events=outcomes.count("CREATED"),
            reused_events=outcomes.count("DUPLICATE"),
            readiness="READY",
        )
