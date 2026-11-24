"""Immutable public funding storage and exact-window completeness manifests."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from baqs.market_data.schema import (
    SYMBOLS,
    FundingConflict,
    FundingGap,
    FundingRateRecord,
)
from baqs.models import FundingCoverageRecord, FundingRate


def _datetime(timestamp_ms: int) -> datetime:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)


def _milliseconds(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def _fingerprint(record: FundingRateRecord) -> str:
    return sha256("|".join(record.fingerprint()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class FundingWindowSnapshot:
    symbol: str
    start_ms: int
    end_ms: int
    events: tuple[FundingRateRecord, ...]
    expected_timestamps_ms: tuple[int, ...]
    expected_event_fingerprints: tuple[str, ...]
    complete: bool


class FundingStore:
    """Stores exact exchange funding observations and immutable completeness manifests."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def upsert_many(self, records: Sequence[FundingRateRecord]) -> list[str]:
        """Idempotently persist a batch; same-time Regular/Special events remain distinct."""
        ordered = sorted(
            records, key=lambda record: (record.funding_time_ms, record.rate_type)
        )
        keys = [
            (record.symbol, record.funding_time_ms, record.rate_type)
            for record in ordered
        ]
        if len(keys) != len(set(keys)):
            raise FundingConflict("Duplicate funding event key in one batch")
        outcomes: list[str] = []
        with Session(self.engine) as session:
            for record in ordered:
                existing = session.get(
                    FundingRate,
                    (
                        record.symbol,
                        _datetime(record.funding_time_ms),
                        record.rate_type,
                    ),
                )
                if existing is None:
                    session.add(
                        FundingRate(
                            symbol=record.symbol,
                            funding_time=_datetime(record.funding_time_ms),
                            rate_type=record.rate_type,
                            funding_rate=record.funding_rate,
                            mark_price=record.mark_price,
                            source=record.source,
                        )
                    )
                    outcomes.append("CREATED")
                    continue
                if (
                    existing.funding_rate != record.funding_rate
                    or existing.mark_price != record.mark_price
                    or existing.source != record.source
                ):
                    raise FundingConflict(
                        "Funding event conflicts with immutable persisted source data"
                    )
                outcomes.append("DUPLICATE")
            session.commit()
        return outcomes

    def upsert(self, record: FundingRateRecord) -> str:
        return self.upsert_many([record])[0]

    @staticmethod
    def _row_record(row: FundingRate) -> FundingRateRecord:
        return FundingRateRecord(
            symbol=row.symbol,
            funding_time_ms=_milliseconds(row.funding_time),
            funding_rate=row.funding_rate,
            mark_price=row.mark_price,
            rate_type=row.rate_type,
            source=row.source,
        )

    def complete_window(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
        records: Sequence[FundingRateRecord],
    ) -> str:
        """Certify a half-open window only after stored events match source fingerprints."""
        if (
            symbol not in SYMBOLS
            or type(start_ms) is not int
            or type(end_ms) is not int
            or start_ms < 0
            or end_ms <= start_ms
            or start_ms % 60_000
            or end_ms % 60_000
        ):
            raise ValueError(
                "Funding coverage window must be a valid 1m-aligned UTC interval"
            )
        ordered = sorted(
            records, key=lambda record: (record.funding_time_ms, record.rate_type)
        )
        if any(
            record.symbol != symbol
            or not start_ms <= record.funding_time_ms < end_ms
            for record in ordered
        ):
            raise FundingGap("Funding record is outside the requested symbol/window")
        keys = [(record.funding_time_ms, record.rate_type) for record in ordered]
        if len(keys) != len(set(keys)):
            raise FundingConflict("Duplicate funding event key in coverage window")

        start, end = _datetime(start_ms), _datetime(end_ms)
        expected_times = [record.funding_time_ms for record in ordered]
        expected_fingerprints = [_fingerprint(record) for record in ordered]
        with Session(self.engine) as session:
            rows = session.scalars(
                select(FundingRate)
                .where(
                    FundingRate.symbol == symbol,
                    FundingRate.funding_time >= start,
                    FundingRate.funding_time < end,
                )
                .order_by(FundingRate.funding_time, FundingRate.rate_type)
            ).all()
            stored = [self._row_record(row) for row in rows]
            stored_fingerprints = [_fingerprint(record) for record in stored]
            if stored_fingerprints != expected_fingerprints:
                raise FundingGap(
                    "Persisted funding events do not match complete source response"
                )

            coverage = session.get(FundingCoverageRecord, (symbol, start, end))
            if coverage is not None:
                if coverage.complete:
                    if (
                        coverage.expected_timestamps_ms != expected_times
                        or coverage.expected_event_fingerprints != expected_fingerprints
                    ):
                        raise FundingConflict("Existing complete funding manifest conflicts")
                    return "DUPLICATE"
                coverage.expected_timestamps_ms = expected_times
                coverage.expected_event_fingerprints = expected_fingerprints
                coverage.event_count = len(ordered)
                coverage.complete = True
                coverage.source = "BINANCE_PUBLIC_FAPI_FUNDING_RATE"
            else:
                session.add(
                    FundingCoverageRecord(
                        symbol=symbol,
                        start_time=start,
                        end_time=end,
                        expected_timestamps_ms=expected_times,
                        expected_event_fingerprints=expected_fingerprints,
                        event_count=len(ordered),
                        complete=True,
                        source="BINANCE_PUBLIC_FAPI_FUNDING_RATE",
                    )
                )
            session.commit()
            return "CREATED"

    def coverage_readiness(
        self, symbol: str, start_ms: int, end_ms: int
    ) -> dict[str, object]:
        if (
            symbol not in SYMBOLS
            or type(start_ms) is not int
            or type(end_ms) is not int
            or start_ms < 0
            or end_ms <= start_ms
            or start_ms % 60_000
            or end_ms % 60_000
        ):
            raise ValueError("Invalid funding readiness window")
        start, end = _datetime(start_ms), _datetime(end_ms)
        with Session(self.engine) as session:
            coverage = session.get(FundingCoverageRecord, (symbol, start, end))
            if coverage is None:
                return {
                    "status": "NO_COVERAGE",
                    "symbol": symbol,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                }
            if not coverage.complete:
                return {
                    "status": "INCOMPLETE",
                    "symbol": symbol,
                    "event_count": coverage.event_count,
                }
            rows = session.scalars(
                select(FundingRate)
                .where(
                    FundingRate.symbol == symbol,
                    FundingRate.funding_time >= start,
                    FundingRate.funding_time < end,
                )
                .order_by(FundingRate.funding_time, FundingRate.rate_type)
            ).all()
            stored = [self._row_record(row) for row in rows]
            actual_times = [_milliseconds(row.funding_time) for row in rows]
            actual_fingerprints = [_fingerprint(record) for record in stored]
            if (
                actual_times != coverage.expected_timestamps_ms
                or actual_fingerprints != coverage.expected_event_fingerprints
                or len(rows) != coverage.event_count
            ):
                return {
                    "status": "GAP",
                    "symbol": symbol,
                    "expected_events": coverage.event_count,
                    "present_events": len(rows),
                }
            return {
                "status": "READY",
                "symbol": symbol,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "event_count": len(rows),
                "expected_timestamps_ms": actual_times,
                "source": coverage.source,
            }

    def load_window(
        self, symbol: str, start_ms: int, end_ms: int
    ) -> FundingWindowSnapshot:
        """Return funding events only after verifying coverage and fingerprints atomically."""
        start, end = _datetime(start_ms), _datetime(end_ms)
        with Session(self.engine) as session:
            coverage = session.get(FundingCoverageRecord, (symbol, start, end))
            if coverage is None or not coverage.complete:
                raise FundingGap("Funding window has no complete coverage manifest")
            rows = session.scalars(
                select(FundingRate)
                .where(
                    FundingRate.symbol == symbol,
                    FundingRate.funding_time >= start,
                    FundingRate.funding_time < end,
                )
                .order_by(FundingRate.funding_time, FundingRate.rate_type)
            ).all()
            records = tuple(self._row_record(row) for row in rows)
            times = tuple(record.funding_time_ms for record in records)
            fingerprints = tuple(_fingerprint(record) for record in records)
            if (
                list(times) != coverage.expected_timestamps_ms
                or list(fingerprints) != coverage.expected_event_fingerprints
                or len(records) != coverage.event_count
            ):
                raise FundingGap("Funding manifest changed while loading backtest inputs")
            return FundingWindowSnapshot(
                symbol=symbol,
                start_ms=start_ms,
                end_ms=end_ms,
                events=records,
                expected_timestamps_ms=times,
                expected_event_fingerprints=fingerprints,
                complete=True,
            )
