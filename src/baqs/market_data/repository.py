"""Immutable finalized-candle storage and window-based health checks."""

from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from baqs.market_data.schema import INTERVAL_MS, CandleConflict, FinalCandle
from baqs.models import Candle

StoreResult = Literal["CREATED", "DUPLICATE", "PROMOTED"]


def _milliseconds(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)  # SQLite test driver drops tzinfo
    return int(value.timestamp() * 1000)


class CandleStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def upsert(self, candle: FinalCandle) -> StoreResult:
        with Session(self.engine) as session:
            existing = session.get(Candle, (candle.symbol, candle.timeframe, candle.open_time))
            if existing is None:
                session.add(
                    Candle(
                        symbol=candle.symbol,
                        timeframe=candle.timeframe,
                        open_time=candle.open_time,
                        close_time=candle.close_time,
                        open=candle.open,
                        high=candle.high,
                        low=candle.low,
                        close=candle.close,
                        volume=candle.volume,
                        quote_volume=candle.quote_volume,
                        trade_count=candle.trade_count,
                        is_final=True,
                        source=candle.source,
                    )
                )
                session.commit()
                return "CREATED"
            old_fingerprint = (
                existing.open,
                existing.high,
                existing.low,
                existing.close,
                existing.volume,
                existing.quote_volume,
                existing.trade_count,
            )
            # SQLite fixture NUMERIC conversion is approximate; tests use whole-number prices.
            if existing.is_final:
                if old_fingerprint != candle.fingerprint() or (
                    _milliseconds(existing.close_time) != candle.close_ms
                ):
                    raise CandleConflict("Finalized candle conflicts with persisted data")
                return "DUPLICATE"
            existing.open = candle.open
            existing.high = candle.high
            existing.low = candle.low
            existing.close = candle.close
            existing.volume = candle.volume
            existing.quote_volume = candle.quote_volume
            existing.trade_count = candle.trade_count
            existing.close_time = candle.close_time
            existing.is_final = True
            existing.source = candle.source
            session.commit()
            return "PROMOTED"

    def latest_open_ms(self, symbol: str, timeframe: str) -> int | None:
        with Session(self.engine) as session:
            value = session.scalar(
                select(Candle.open_time)
                .where(
                    Candle.symbol == symbol,
                    Candle.timeframe == timeframe,
                    Candle.is_final.is_(True),
                )
                .order_by(Candle.open_time.desc())
                .limit(1)
            )
        return None if value is None else _milliseconds(value)

    def gap_ranges(
        self,
        symbol: str,
        timeframe: str,
        *,
        window: int = 240,
    ) -> list[tuple[int, int]]:
        """List missing half-open windows between recent finalized candles."""
        step = INTERVAL_MS[timeframe]
        with Session(self.engine) as session:
            values = session.scalars(
                select(Candle.open_time)
                .where(
                    Candle.symbol == symbol,
                    Candle.timeframe == timeframe,
                    Candle.is_final.is_(True),
                )
                .order_by(Candle.open_time.desc())
                .limit(window)
            ).all()
        times = sorted(_milliseconds(value) for value in values)
        return [
            (older + step, newer)
            for older, newer in zip(times, times[1:], strict=False)
            if newer > older + step
        ]


    def history_readiness(
        self,
        symbol: str,
        timeframe: str,
        anchored_end_ms: int,
        *,
        required_bars: int,
        require_latest: bool = False,
    ) -> dict[str, object]:
        """Verify every closed candle in a fixed UTC half-open history window.

        Not equivalent to `health()`: this checks full feature warm-up coverage.
        When require_latest=True, the window must end at the most recent closed bar.
        """
        if symbol not in {"BTCUSDT", "ETHUSDT"} or timeframe not in INTERVAL_MS:
            raise ValueError("Unknown research feed")
        step = INTERVAL_MS[timeframe]
        if (
            type(required_bars) is not int
            or not 1 <= required_bars <= 10_000
            or type(anchored_end_ms) is not int
            or anchored_end_ms < required_bars * step
            or anchored_end_ms % step
        ):
            raise ValueError("Invalid historical readiness window")
        start_ms = anchored_end_ms - required_bars * step
        start = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
        end = datetime.fromtimestamp(anchored_end_ms / 1000, tz=UTC)
        with Session(self.engine) as session:
            values = session.scalars(
                select(Candle.open_time)
                .where(
                    Candle.symbol == symbol,
                    Candle.timeframe == timeframe,
                    Candle.is_final.is_(True),
                    Candle.open_time >= start,
                    Candle.open_time < end,
                )
                .order_by(Candle.open_time)
            ).all()
        times = [_milliseconds(value) for value in values]
        # Account for a stale final bar independently from warm-up completeness.
        last_available = self.latest_open_ms(symbol, timeframe)
        if last_available is None:
            status = "NO_DATA"
        elif require_latest and last_available < anchored_end_ms - step:
            status = "STALE"
        elif len(times) == required_bars and all(
            timestamp == start_ms + i * step for i, timestamp in enumerate(times)
        ):
            status = "READY"
        elif not times or times[0] > start_ms:
            status = "INSUFFICIENT"
        else:
            status = "GAP"
        return {
            "status": status,
            "symbol": symbol,
            "timeframe": timeframe,
            "required_bars": required_bars,
            "present_bars": len(times),
            "anchored_end_ms": anchored_end_ms,
            "earliest_expected_open_ms": start_ms,
            "last_expected_open_ms": anchored_end_ms - step,
            "first_missing_open_ms": next(
                (
                    start_ms + i * step
                    for i in range(required_bars)
                    if i >= len(times) or times[i] != start_ms + i * step
                ),
                None,
            ),
        }

    def health(
        self,
        symbol: str,
        timeframe: str,
        now_ms: int,
        *,
        window: int = 240,
    ) -> dict[str, object]:
        step = INTERVAL_MS[timeframe]
        with Session(self.engine) as session:
            rows = session.scalars(
                select(Candle)
                .where(
                    Candle.symbol == symbol,
                    Candle.timeframe == timeframe,
                    Candle.is_final.is_(True),
                )
                .order_by(Candle.open_time.desc())
                .limit(window)
            ).all()
        if not rows:
            return {"status": "NO_DATA", "latest_open_ms": None, "gap_count": None}
        times = sorted(_milliseconds(row.open_time) for row in rows)
        gaps = sum(
            (b - a) // step - 1 for a, b in zip(times, times[1:], strict=False) if b > a + step
        )
        stale = now_ms - (times[-1] + step) > 2 * step
        status = "GAP" if gaps else "STALE" if stale else "READY"
        return {
            "status": status,
            "latest_open_ms": times[-1],
            "gap_count": gaps,
            "checked_bars": len(times),
        }
