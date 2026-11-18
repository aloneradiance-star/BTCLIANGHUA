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
            existing = session.get(Candle, (
                candle.symbol, candle.timeframe, candle.open_time
            ))
            if existing is None:
                session.add(Candle(
                    symbol=candle.symbol, timeframe=candle.timeframe,
                    open_time=candle.open_time, close_time=candle.close_time,
                    open=candle.open, high=candle.high, low=candle.low, close=candle.close,
                    volume=candle.volume, quote_volume=candle.quote_volume,
                    trade_count=candle.trade_count, is_final=True, source=candle.source,
                ))
                session.commit()
                return "CREATED"
            old_fingerprint = (
                existing.open, existing.high, existing.low, existing.close, existing.volume,
                existing.quote_volume, existing.trade_count,
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
            value = session.scalar(select(Candle.open_time).where(
                Candle.symbol == symbol, Candle.timeframe == timeframe,
                Candle.is_final.is_(True),
            ).order_by(Candle.open_time.desc()).limit(1))
        return None if value is None else _milliseconds(value)

    def gap_ranges(
        self, symbol: str, timeframe: str, *, window: int = 240,
    ) -> list[tuple[int, int]]:
        """List missing half-open windows between recent finalized candles."""
        step = INTERVAL_MS[timeframe]
        with Session(self.engine) as session:
            values = session.scalars(select(Candle.open_time).where(
                Candle.symbol == symbol, Candle.timeframe == timeframe,
                Candle.is_final.is_(True),
            ).order_by(Candle.open_time.desc()).limit(window)).all()
        times = sorted(_milliseconds(value) for value in values)
        return [
            (older + step, newer)
            for older, newer in zip(times, times[1:], strict=False)
            if newer > older + step
        ]

    def health(
        self, symbol: str, timeframe: str, now_ms: int, *, window: int = 240,
    ) -> dict[str, object]:
        step = INTERVAL_MS[timeframe]
        with Session(self.engine) as session:
            rows = session.scalars(select(Candle).where(
                Candle.symbol == symbol, Candle.timeframe == timeframe,
                Candle.is_final.is_(True),
            ).order_by(Candle.open_time.desc()).limit(window)).all()
        if not rows:
            return {"status": "NO_DATA", "latest_open_ms": None, "gap_count": None}
        times = sorted(_milliseconds(row.open_time) for row in rows)
        gaps = sum((b - a) // step - 1 for a, b in zip(times, times[1:], strict=False) if b > a + step)
        stale = now_ms - (times[-1] + step) > 2 * step
        status = "GAP" if gaps else "STALE" if stale else "READY"
        return {
            "status": status, "latest_open_ms": times[-1],
            "gap_count": gaps, "checked_bars": len(times),
        }
