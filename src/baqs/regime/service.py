"""Research-only, database-backed, restart-safe regime observation pipeline.

No Binance authentication or trading API. Only finalized, contiguous public
candles may produce an auditable 5m RegimeSnapshot. Readiness failures are
UNAVAILABLE, never forcibly interpreted as a market-state prediction.
"""

from datetime import UTC, datetime

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from baqs.indicators import compute_features
from baqs.market_data.repository import CandleStore
from baqs.market_data.schema import INTERVAL_MS, SYMBOLS, FinalCandle
from baqs.models import Candle, RegimeSnapshot
from baqs.regime.gate import FIVE_MIN_MS, GateState, advance
from baqs.regime.rules import REGIME_VERSION, RegimeUnavailable, classify_candidate

REQUIRED = {"5m": 3, "15m": 2920, "1h": 120}


def _as_milliseconds(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)  # SQLite test driver loses timezone
    return int(value.timestamp() * 1000)


def _to_final_candle(item: Candle) -> FinalCandle:
    return FinalCandle(
        symbol=item.symbol,
        timeframe=item.timeframe,
        open_ms=_as_milliseconds(item.open_time),
        close_ms=_as_milliseconds(item.close_time),
        open=item.open,
        high=item.high,
        low=item.low,
        close=item.close,
        volume=item.volume,
        quote_volume=(
            item.quote_volume if item.quote_volume is not None else item.volume * item.close
        ),
        trade_count=item.trade_count if item.trade_count is not None else 0,
        source="REST" if item.source != "WS" else "WS",
    )


class RegimeResearchService:
    """One active writer only. Database unique keys prevent duplicate snapshots.

    Multi-writer distributed locking is NOT implemented in Phase 1.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.store = CandleStore(engine)

    def _load(
        self, session: Session, symbol: str, timeframe: str, end_ms: int, count: int,
    ) -> list[FinalCandle]:
        cutoff = datetime.fromtimestamp(end_ms / 1000, tz=UTC)
        items = session.scalars(
            select(Candle)
            .where(
                Candle.symbol == symbol,
                Candle.timeframe == timeframe,
                Candle.is_final.is_(True),
                Candle.close_time <= cutoff,
            )
            .order_by(Candle.open_time.desc())
            .limit(count)
        ).all()
        if len(items) != count:
            raise RegimeUnavailable(f"{timeframe.upper()}_INSUFFICIENT_ROWS")
        result = [_to_final_candle(item) for item in reversed(items)]
        step = INTERVAL_MS[timeframe]
        expected = end_ms - count * step
        if any(
            bar.open_ms != expected + i * step
            or bar.close_ms != expected + (i + 1) * step
            for i, bar in enumerate(result)
        ):
            raise RegimeUnavailable(f"{timeframe.upper()}_GAP_OR_UNFINALIZED")
        return result

    def _last_snapshot(self, session: Session, symbol: str) -> RegimeSnapshot | None:
        return session.scalar(
            select(RegimeSnapshot)
            .where(
                RegimeSnapshot.symbol == symbol,
                RegimeSnapshot.timeframe == "5m",
                RegimeSnapshot.classifier_version == REGIME_VERSION,
            )
            .order_by(RegimeSnapshot.candle_close_time.desc())
            .limit(1)
        )

    def observe(self, symbol: str, *, close_ms: int, clock_ms: int) -> dict[str, object]:
        """Persist one closed 5m observation after strict as-of readiness.

        When replaying fixtures, the caller provides an explicit matching clock.
        Actual runner supplies UTC wall clock and never evaluates a future 5m bar.
        """
        if symbol not in SYMBOLS:
            raise ValueError("Unsupported research symbol")
        if (
            type(close_ms) is not int or close_ms < 0 or close_ms % FIVE_MIN_MS
            or type(clock_ms) is not int or not close_ms <= clock_ms < close_ms + 2 * FIVE_MIN_MS
        ):
            raise RegimeUnavailable("STALE_OR_FUTURE_5M_CLOSE")

        ends = {
            timeframe: close_ms // INTERVAL_MS[timeframe] * INTERVAL_MS[timeframe]
            for timeframe in REQUIRED
        }
        for timeframe, count in REQUIRED.items():
            readiness = self.store.history_readiness(
                symbol, timeframe, ends[timeframe],
                required_bars=count, require_latest=False,
            )
            if readiness["status"] != "READY":
                raise RegimeUnavailable(
                    f"{timeframe.upper()}_HISTORY_{readiness['status']}"
                )

        with Session(self.engine) as session:
            previous = self._last_snapshot(session, symbol)
            if previous is not None:
                last_close = _as_milliseconds(previous.candle_close_time)
                if last_close > close_ms:
                    raise RegimeUnavailable("OUT_OF_ORDER_REGIME_OBSERVATION")
                if last_close == close_ms:
                    return self._serialize(previous)

            bars5 = self._load(session, symbol, "5m", ends["5m"], REQUIRED["5m"])
            bars15 = self._load(session, symbol, "15m", ends["15m"], REQUIRED["15m"])
            bars1h = self._load(session, symbol, "1h", ends["1h"], REQUIRED["1h"])
            features15 = compute_features(bars15)
            features1h = compute_features(bars1h)
            candidate = classify_candidate(bars15, features15, features1h)

            if previous is None:
                gate_state = GateState()
            else:
                saved_state = previous.features.get("gate_state")
                try:
                    gate_state = GateState.from_dict(saved_state)
                except ValueError as exc:
                    raise RegimeUnavailable("CORRUPT_PERSISTED_GATE_STATE") from exc
            if gate_state.last_processed_ms is not None and previous is not None:
                if gate_state.last_processed_ms != _as_milliseconds(
                    previous.candle_close_time
                ):
                    raise RegimeUnavailable("GATE_TIMESTAMP_MISMATCH")

            transition = advance(gate_state, candidate.regime, close_ms)
            snapshot = RegimeSnapshot(
                symbol=symbol,
                timeframe="5m",
                candle_open_time=bars5[-1].open_time,
                candle_close_time=bars5[-1].close_time,
                classifier_version=REGIME_VERSION,
                regime=transition.state.current,
                features={
                    "candidate": candidate.regime,
                    "metrics": candidate.metrics,
                    "gate_state": transition.state.as_dict(),
                    "source_15m_close_ms": bars15[-1].close_ms,
                    "source_1h_close_ms": bars1h[-1].close_ms,
                    "signal_allowed": False,
                    "probability_calibrated": False,
                },
                reasons=list(candidate.reasons + transition.reasons),
            )
            session.add(snapshot)
            try:
                session.commit()
            except IntegrityError as exc:
                # A concurrent writer must never silently overwrite state.
                session.rollback()
                raise RegimeUnavailable("DUPLICATE_OR_CONCURRENT_OBSERVATION") from exc
            return self._serialize(snapshot)

    @staticmethod
    def _serialize(snapshot: RegimeSnapshot) -> dict[str, object]:
        return {
            "status": "READY",
            "symbol": snapshot.symbol,
            "regime": snapshot.regime,
            "timeframe": snapshot.timeframe,
            "candle_close_time": snapshot.candle_close_time.replace(
                tzinfo=snapshot.candle_close_time.tzinfo or UTC
            ).isoformat(),
            "classifier_version": snapshot.classifier_version,
            "features": snapshot.features,
            "reasons": snapshot.reasons,
            "execution_enabled": False,
        }

    def latest(self, symbol: str, *, clock_ms: int) -> dict[str, object]:
        """Current read-only regime, rejecting stale snapshots and invalid feed history."""
        if symbol not in SYMBOLS:
            raise ValueError("Unsupported symbol")
        with Session(self.engine) as session:
            last = self._last_snapshot(session, symbol)
            if last is None:
                return {"status": "UNAVAILABLE", "reason": "NO_CLASSIFICATION"}
            close_ms = _as_milliseconds(last.candle_close_time)
            if close_ms > clock_ms or clock_ms - close_ms >= 2 * FIVE_MIN_MS:
                return {
                    "status": "UNAVAILABLE",
                    "reason": "STALE_CLASSIFICATION",
                    "last_candle_close_time": last.candle_close_time.isoformat(),
                }
            if not isinstance(last.features, dict) or not isinstance(
                last.features.get("gate_state"), dict
            ):
                return {"status": "UNAVAILABLE", "reason": "UNVERIFIED_LEGACY_SNAPSHOT"}
            for timeframe, count in REQUIRED.items():
                end = clock_ms // INTERVAL_MS[timeframe] * INTERVAL_MS[timeframe]
                readiness = self.store.history_readiness(
                    symbol, timeframe, end, required_bars=count, require_latest=True,
                )
                if readiness["status"] != "READY":
                    return {
                        "status": "UNAVAILABLE",
                        "reason": f"{timeframe.upper()}_HISTORY_{readiness['status']}",
                    }
            return self._serialize(last)
