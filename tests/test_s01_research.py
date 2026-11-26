"""Read-only S01 data adapter, causal context and deterministic replay tests."""

from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, Inexact, localcontext

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from baqs.backtest.engine import CostModel, EntrySignal
from baqs.backtest.research import S01ResearchService, report_json
from baqs.market_data.funding import FundingStore
from baqs.market_data.repository import CandleStore
from baqs.market_data.schema import FinalCandle, FundingRateRecord
from baqs.models import Candle, FundingCoverageRecord, FundingRate, RegimeSnapshot
from baqs.strategies.s01 import S01TrendPullbackStrategy

MINUTE = 60_000
QUARTER = 15 * MINUTE
HOUR = 60 * MINUTE
START = QUARTER * 100_000
END = START + 30 * MINUTE


def candle(
    symbol: str,
    timeframe: str,
    open_ms: int,
    *,
    step: int,
    open_price: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100",
) -> FinalCandle:
    return FinalCandle(
        symbol,
        timeframe,
        open_ms,
        open_ms + step,
        Decimal(open_price),
        Decimal(high),
        Decimal(low),
        Decimal(close),
        Decimal(10),
        Decimal(1000),
        10,
        "REST",
    )


def seed_history(
    engine: Engine,
    *,
    missing_1m: int | None = None,
    add_future_hour: bool = False,
) -> None:
    store = CandleStore(engine)
    warmup_15m_start = START - 2920 * QUARTER
    bars_15m = [
        candle("BTCUSDT", "15m", warmup_15m_start + i * QUARTER, step=QUARTER) for i in range(2922)
    ]
    warmup_1h_start = START - 120 * HOUR
    bars_1h = [candle("BTCUSDT", "1h", warmup_1h_start + i * HOUR, step=HOUR) for i in range(120)]
    if add_future_hour:
        bars_1h.append(candle("BTCUSDT", "1h", START, step=HOUR))
    bars_1m = [
        candle(
            "BTCUSDT",
            "1m",
            START + i * MINUTE,
            step=MINUTE,
            high="125" if i == 1 else "101",
            low="99",
            close="120" if i == 1 else "100",
        )
        for i in range(30)
        if i != missing_1m
    ]
    store.upsert_many(bars_15m)
    store.upsert_many(bars_1h)
    store.upsert_many(bars_1m)


def seed_funding(
    engine: Engine,
    *,
    include_special: bool = True,
    funding_time_ms: int = START + MINUTE,
) -> None:
    records = [
        FundingRateRecord(
            "BTCUSDT",
            funding_time_ms,
            Decimal("0.001"),
            Decimal(100),
            "Regular",
        )
    ]
    if include_special:
        records.append(
            FundingRateRecord(
                "BTCUSDT",
                funding_time_ms,
                Decimal("0.0005"),
                Decimal(101),
                "Special",
            )
        )
    store = FundingStore(engine)
    store.upsert_many(records)
    store.complete_window("BTCUSDT", START, END, records)


def service(engine: Engine) -> S01ResearchService:
    return S01ResearchService(
        engine,
        costs=CostModel(Decimal(5), Decimal(2)),
    )


def table_counts(engine: Engine) -> tuple[int, int, int, int]:
    with Session(engine) as session:
        return (
            session.scalar(select(func.count()).select_from(Candle)) or 0,
            session.scalar(select(func.count()).select_from(FundingRate)) or 0,
            session.scalar(select(func.count()).select_from(FundingCoverageRecord)) or 0,
            session.scalar(select(func.count()).select_from(RegimeSnapshot)) or 0,
        )


def test_successful_replay_is_deterministic_and_performs_zero_database_writes(
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_history(db_engine, add_future_hour=True)
    seed_funding(db_engine)
    before = table_counts(db_engine)

    def deterministic_signal(
        self: S01TrendPullbackStrategy,
        bars15: list[FinalCandle],
        bars1h: list[FinalCandle],
    ) -> list[EntrySignal]:
        assert len(bars15) == 2922
        assert len(bars1h) == 120
        # The latest completed 1h candle is at the evaluation start. The
        # future 1h candle persisted in the DB must never enter this context.
        assert bars1h[-1].close_ms == START
        return [EntrySignal("BTCUSDT", START, "LONG", Decimal(90), "S01-0.1")]

    monkeypatch.setattr(S01TrendPullbackStrategy, "generate_signals", deterministic_signal)
    runner = service(db_engine)
    first = runner.run("BTCUSDT", START, END)
    second = runner.run("BTCUSDT", START, END)

    assert first.status == "READY"
    assert first.result is not None
    assert first.result.metrics.trade_count == 1
    assert first.result.trades[0].entry_time_ms == START
    assert first.result.trades[0].exit_reason == "TAKE_PROFIT"
    assert first.result.trades[0].funding_cost > 0
    assert first.warmup["required_preceding_15m_bars"] == 2920
    assert first.warmup["required_preceding_closed_1h_bars"] == 120
    assert first.strategy_parameters["adx_min"] == "23"
    assert first.data_provenance["completed_context_1h"]["latest_completed_close_ms"] == START
    assert report_json(first) == report_json(second)
    assert table_counts(db_engine) == before


def test_real_s01_signal_replay_is_independent_of_caller_decimal_context(db_engine: Engine) -> None:
    def priced_bar(
        timeframe: str,
        open_ms: int,
        step: int,
        price: Decimal,
        *,
        low: str | None = None,
        high: str | None = None,
    ) -> FinalCandle:
        return candle(
            "BTCUSDT",
            timeframe,
            open_ms,
            step=step,
            open_price=str(price),
            close=str(price),
            low=low if low is not None else str(price - Decimal("0.02")),
            high=high if high is not None else str(price + Decimal("0.02")),
        )

    store = CandleStore(db_engine)
    warmup = [
        priced_bar("15m", START + (i - 2920) * QUARTER, QUARTER, Decimal(100) + Decimal(i) / 100)
        for i in range(2919)
    ]
    store.upsert_many(
        warmup
        + [
            priced_bar("15m", START - QUARTER, QUARTER, Decimal("129.00")),
            priced_bar("15m", START, QUARTER, Decimal("129.11"), low="129.00"),
            priced_bar("15m", START + QUARTER, QUARTER, Decimal("129.20")),
        ]
    )
    store.upsert_many(
        [
            priced_bar("1h", START + (i - 120) * HOUR, HOUR, Decimal(100) + Decimal(i) / 10)
            for i in range(120)
        ]
    )
    store.upsert_many(
        [
            priced_bar(
                "1m",
                START + i * MINUTE,
                MINUTE,
                Decimal("129.12"),
                high="130" if i == 16 else None,
            )
            for i in range(30)
        ]
    )
    seed_funding(db_engine, funding_time_ms=START + QUARTER + MINUTE)
    runner = service(db_engine)
    reference = runner.run("BTCUSDT", START, END)
    assert reference.status == "READY"
    assert reference.result is not None
    (trade,) = reference.result.trades
    assert trade.side == "LONG"
    assert trade.entry_time_ms == START + QUARTER
    assert trade.exit_reason == "TAKE_PROFIT"
    assert trade.funding_cost > 0
    expected = report_json(reference)

    for trap_inexact in (False, True):
        with localcontext() as caller:
            caller.prec = 9
            caller.rounding = ROUND_DOWN
            caller.capitals = 0
            caller.traps[Inexact] = trap_inexact
            caller.clear_flags()
            assert report_json(runner.run("BTCUSDT", START, END)) == expected
            assert caller.prec == 9 and caller.rounding == ROUND_DOWN
            assert caller.capitals == 0 and caller.traps[Inexact] is trap_inexact
            assert not caller.flags[Inexact]


@pytest.mark.parametrize(
    ("timeframe", "open_ms", "column"),
    [
        ("1m", START + 7 * MINUTE, "open_time"),
        ("15m", START - 100 * QUARTER, "close_time"),
        ("1h", START - HOUR, "close_time"),
    ],
)
def test_submillisecond_candle_timestamps_block_replay(
    db_engine: Engine, timeframe: str, open_ms: int, column: str
) -> None:
    seed_history(db_engine)
    seed_funding(db_engine)
    step = {"1m": MINUTE, "15m": QUARTER, "1h": HOUR}[timeframe]
    opened = datetime.fromtimestamp(open_ms / 1000, tz=UTC)
    expected = opened if column == "open_time" else opened + timedelta(milliseconds=step)
    with db_engine.begin() as conn:
        conn.execute(
            update(Candle)
            .where(
                Candle.symbol == "BTCUSDT",
                Candle.timeframe == timeframe,
                Candle.open_time == opened,
            )
            .values({column: expected + timedelta(microseconds=1)})
        )
    report = service(db_engine).run("BTCUSDT", START, END)
    assert report.status == "UNAVAILABLE"
    assert report.result is None
    assert report.reason == f"{timeframe.upper()}_GAP_OR_MISALIGNED_CLOSE"


def test_research_preserves_exact_funding_timestamp_when_order_is_clear(db_engine: Engine) -> None:
    seed_history(db_engine)
    seed_funding(db_engine, funding_time_ms=START + MINUTE + 1)
    report = service(db_engine).run("BTCUSDT", START, END)
    assert report.status == "READY"
    assert report.result is not None
    assert report.data_provenance["funding"]["expected_timestamps_ms"] == [START + MINUTE + 1] * 2


def test_missing_execution_history_returns_unavailable(db_engine: Engine) -> None:
    report = service(db_engine).run("BTCUSDT", START, END)
    assert report.status == "UNAVAILABLE"
    assert report.result is None
    assert report.reason == "1M_HISTORY_NO_DATA"


def test_missing_execution_minute_returns_unavailable(db_engine: Engine) -> None:
    seed_history(db_engine, missing_1m=7)
    seed_funding(db_engine)
    report = service(db_engine).run("BTCUSDT", START, END)
    assert report.status == "UNAVAILABLE"
    assert report.result is None
    assert report.reason == "1M_HISTORY_GAP"


@pytest.mark.parametrize(
    ("timeframe", "column", "reason"),
    [
        ("1m", "quote_volume", "1M_MISSING_QUOTE_VOLUME"),
        ("1m", "trade_count", "1M_MISSING_TRADE_COUNT"),
        ("15m", "quote_volume", "15M_MISSING_QUOTE_VOLUME"),
        ("15m", "trade_count", "15M_MISSING_TRADE_COUNT"),
        ("1h", "quote_volume", "1H_MISSING_QUOTE_VOLUME"),
        ("1h", "trade_count", "1H_MISSING_TRADE_COUNT"),
    ],
)
def test_missing_execution_metadata_is_not_synthesized(
    db_engine: Engine,
    timeframe: str,
    column: str,
    reason: str,
) -> None:
    seed_history(db_engine)
    seed_funding(db_engine)
    step = {"1m": MINUTE, "15m": QUARTER, "1h": HOUR}[timeframe]
    open_ms = {
        "1m": START,
        "15m": START - 2920 * QUARTER,
        "1h": START - 120 * HOUR,
    }[timeframe]
    with Session(db_engine) as session:
        session.execute(
            update(Candle)
            .where(
                Candle.symbol == "BTCUSDT",
                Candle.timeframe == timeframe,
                Candle.open_time == candle("BTCUSDT", timeframe, open_ms, step=step).open_time,
            )
            .values(**{column: None})
        )
        session.commit()
    report = service(db_engine).run("BTCUSDT", START, END)
    assert report.status == "UNAVAILABLE"
    assert report.reason == reason


def test_missing_funding_manifest_blocks_replay(db_engine: Engine) -> None:
    seed_history(db_engine)
    report = service(db_engine).run("BTCUSDT", START, END)
    assert report.status == "UNAVAILABLE"
    assert report.result is None
    assert "no complete coverage manifest" in (report.reason or "")


def test_tampered_funding_manifest_blocks_replay(db_engine: Engine) -> None:
    seed_history(db_engine)
    seed_funding(db_engine)
    with Session(db_engine) as session:
        session.execute(
            update(FundingRate)
            .where(
                FundingRate.symbol == "BTCUSDT",
                FundingRate.funding_time
                == candle("BTCUSDT", "1m", START + MINUTE, step=MINUTE).open_time,
                FundingRate.rate_type == "Regular",
            )
            .values(mark_price=Decimal(102))
        )
        session.commit()
    report = service(db_engine).run("BTCUSDT", START, END)
    assert report.status == "UNAVAILABLE"
    assert report.result is None
    assert "Funding manifest changed" in (report.reason or "")


@pytest.mark.parametrize("timeframe", ["15m", "1h"])
def test_missing_multitimeframe_warmup_blocks_replay(
    db_engine: Engine,
    timeframe: str,
) -> None:
    seed_history(db_engine)
    seed_funding(db_engine)
    step = QUARTER if timeframe == "15m" else HOUR
    start = START - (2920 * QUARTER if timeframe == "15m" else 120 * HOUR)
    with Session(db_engine) as session:
        session.execute(
            update(Candle)
            .where(
                Candle.symbol == "BTCUSDT",
                Candle.timeframe == timeframe,
                Candle.open_time == candle("BTCUSDT", timeframe, start, step=step).open_time,
            )
            .values(is_final=False)
        )
        session.commit()
    report = service(db_engine).run("BTCUSDT", START, END)
    assert report.status == "UNAVAILABLE"
    assert report.result is None
    assert timeframe.upper() in (report.reason or "")


def test_future_evaluation_window_returns_unavailable(db_engine: Engine) -> None:
    next_quarter = (int(datetime.now(UTC).timestamp() * 1000) // QUARTER + 1) * QUARTER
    report = service(db_engine).run("BTCUSDT", next_quarter, next_quarter + QUARTER)
    assert report.status == "UNAVAILABLE"
    assert report.reason == "EVALUATION_WINDOW_IN_FUTURE"


def test_unaligned_or_reversed_window_is_rejected(db_engine: Engine) -> None:
    with pytest.raises(ValueError, match="15m-aligned"):
        service(db_engine).run("BTCUSDT", START + MINUTE, END)
    with pytest.raises(ValueError, match="15m-aligned"):
        service(db_engine).run("BTCUSDT", END, START)
