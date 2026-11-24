"""Manual, read-only, reproducible S01 historical research replay.

Loads exact finalized 1m execution bars, 15m feature warm-up, completed 1h
context and a fingerprint-verified funding manifest. No source data is written.
"""

import argparse
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from baqs.backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    BacktestResult,
    BacktestUnavailable,
    CostModel,
    FundingCoverage,
    FundingEvent,
)
from baqs.db import make_engine
from baqs.market_data.funding import FundingStore
from baqs.market_data.repository import CandleStore
from baqs.market_data.schema import (
    INTERVAL_MS,
    SYMBOLS,
    CandleGap,
    FinalCandle,
    FundingGap,
    InvalidCandle,
)
from baqs.models import Candle
from baqs.settings import get_settings
from baqs.strategies.s01 import S01Config, S01TrendPullbackStrategy

WARMUP_15M_BARS = 2920
WARMUP_1H_BARS = 120
MAX_EXECUTION_BARS = 50_000
LIMITATIONS = (
    "Research simulation only; not financial advice or a profitability claim.",
    "Execution uses 1m OHLC bars; intrabar price paths are unknown.",
    "Ambiguous stop/target touches resolve conservatively as stop-first.",
    "Partial fills, order-book liquidity, market impact and exchange outages are not modelled.",
    "Liquidation, margin tiers, borrow costs and cross-position portfolio risk are not modelled.",
    "Fees and slippage are explicit user assumptions, not verified historical fills.",
    "Funding uses only persisted exchange events and their associated mark prices.",
    "Gross notional is capped at 1x equity; leverage and live execution are disabled.",
)


@dataclass(frozen=True, slots=True)
class _LoadedInputs:
    execution_bars: tuple[FinalCandle, ...]
    signal_bars_15m: tuple[FinalCandle, ...]
    context_bars_1h: tuple[FinalCandle, ...]
    funding_events: tuple[FundingEvent, ...]
    funding_coverage: FundingCoverage
    provenance: dict[str, object]
    warmup: dict[str, int]


@dataclass(frozen=True, slots=True)
class S01ResearchReport:
    status: Literal["READY", "UNAVAILABLE"]
    symbol: str
    start_ms: int
    end_ms: int
    strategy_version: str
    strategy_parameters: dict[str, str]
    reason: str | None
    warmup: dict[str, int]
    data_provenance: dict[str, object]
    cost_assumptions: dict[str, str]
    result: BacktestResult | None
    limitations: tuple[str, ...] = LIMITATIONS


def _milliseconds(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)  # SQLite drops timezone metadata in tests.
    return int(value.timestamp() * 1000)


def _candle_digest(bars: tuple[FinalCandle, ...]) -> str:
    payload = [
        [
            bar.open_ms,
            bar.close_ms,
            *(str(value) for value in bar.fingerprint()),
            bar.source,
        ]
        for bar in bars
    ]
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return sha256(encoded).hexdigest()


def _source_counts(bars: tuple[FinalCandle, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for bar in bars:
        counts[bar.source] = counts.get(bar.source, 0) + 1
    return dict(sorted(counts.items()))


def _load_candles(
    engine: Engine,
    *,
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
    expected_bars: int,
) -> tuple[FinalCandle, ...]:
    """Load a strict half-open series; missing execution metadata is not fabricated."""
    step = INTERVAL_MS[timeframe]
    start = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
    end = datetime.fromtimestamp(end_ms / 1000, tz=UTC)
    with Session(engine) as session:
        rows = session.scalars(
            select(Candle)
            .where(
                Candle.symbol == symbol,
                Candle.timeframe == timeframe,
                Candle.open_time >= start,
                Candle.open_time < end,
                Candle.is_final.is_(True),
            )
            .order_by(Candle.open_time)
        ).all()

    if len(rows) != expected_bars:
        raise BacktestUnavailable(
            f"{timeframe.upper()}_ROW_COUNT_MISMATCH expected={expected_bars} actual={len(rows)}"
        )

    result: list[FinalCandle] = []
    for index, row in enumerate(rows):
        open_ms = _milliseconds(row.open_time)
        close_ms = _milliseconds(row.close_time)
        expected_open = start_ms + index * step
        if open_ms != expected_open or close_ms != expected_open + step:
            raise BacktestUnavailable(f"{timeframe.upper()}_GAP_OR_MISALIGNED_CLOSE")
        if not row.is_final:
            raise BacktestUnavailable(f"{timeframe.upper()}_NON_FINAL_CANDLE")
        if row.quote_volume is None:
            raise BacktestUnavailable(f"{timeframe.upper()}_MISSING_QUOTE_VOLUME")
        if row.trade_count is None:
            raise BacktestUnavailable(f"{timeframe.upper()}_MISSING_TRADE_COUNT")
        if row.source not in {"REST", "WS"}:
            raise BacktestUnavailable(f"{timeframe.upper()}_UNKNOWN_SOURCE")
        result.append(
            FinalCandle(
                symbol=row.symbol,
                timeframe=row.timeframe,
                open_ms=open_ms,
                close_ms=close_ms,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                quote_volume=row.quote_volume,
                trade_count=row.trade_count,
                source=row.source,
            )
        )
    return tuple(result)


def _require_ready(
    store: CandleStore,
    symbol: str,
    timeframe: str,
    end_ms: int,
    bars: int,
) -> None:
    readiness = store.history_readiness(
        symbol,
        timeframe,
        end_ms,
        required_bars=bars,
        require_latest=False,
    )
    if readiness["status"] != "READY" and not (
        readiness["invalid_metadata_count"] or readiness["invalid_close_count"]
    ):
        raise BacktestUnavailable(f"{timeframe.upper()}_HISTORY_{readiness['status']}")


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _json_value(value: object) -> object:
    """Convert dataclasses and Decimals to deterministic JSON-compatible values."""
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if is_dataclass(value):
        return _json_value(asdict(cast(Any, value)))
    return value


def report_json(report: S01ResearchReport) -> str:
    """Serialize a report deterministically, preserving exact Decimal strings."""
    return json.dumps(_json_value(report), sort_keys=True, separators=(",", ":"))


class S01ResearchService:
    """Read-only adapter from verified database history to the P2-01 event engine."""

    def __init__(
        self,
        engine: Engine,
        *,
        costs: CostModel,
        config: BacktestConfig | None = None,
        strategy_config: S01Config | None = None,
    ) -> None:
        resolved_config = config if config is not None else BacktestConfig()
        resolved_strategy = strategy_config if strategy_config is not None else S01Config()
        if resolved_config.max_notional_fraction > Decimal(1):
            raise ValueError("P2-03 does not permit leverage or notional above 1x")
        self.engine = engine
        self.costs = costs
        self.config = resolved_config
        self.strategy = S01TrendPullbackStrategy(resolved_strategy)
        self.candle_store = CandleStore(engine)
        self.funding_store = FundingStore(engine)

    @property
    def strategy_parameters(self) -> dict[str, str]:
        config = self.strategy.config
        return {
            "adx_min": _decimal_text(config.adx_min),
            "long_rsi_min": _decimal_text(config.long_rsi_min),
            "long_rsi_max": _decimal_text(config.long_rsi_max),
            "short_rsi_min": _decimal_text(config.short_rsi_min),
            "short_rsi_max": _decimal_text(config.short_rsi_max),
            "volume_ratio_min": _decimal_text(config.volume_ratio_min),
            "stop_atr_buffer": _decimal_text(config.stop_atr_buffer),
        }

    @property
    def cost_assumptions(self) -> dict[str, str]:
        return {
            "taker_fee_bps_each_side": _decimal_text(self.costs.taker_fee_bps),
            "adverse_slippage_bps_each_side": _decimal_text(self.costs.slippage_bps),
            "initial_equity": _decimal_text(self.config.initial_equity),
            "risk_fraction": _decimal_text(self.config.risk_fraction),
            "max_notional_fraction": _decimal_text(self.config.max_notional_fraction),
            "reward_risk": _decimal_text(self.config.reward_risk),
        }

    def _load_inputs(self, symbol: str, start_ms: int, end_ms: int) -> _LoadedInputs:
        if (
            symbol not in SYMBOLS
            or type(start_ms) is not int
            or type(end_ms) is not int
            or start_ms < 0
            or end_ms <= start_ms
            or start_ms % INTERVAL_MS["15m"]
            or end_ms % INTERVAL_MS["15m"]
            or (end_ms - start_ms) % INTERVAL_MS["1m"]
        ):
            raise ValueError("Research window must be a positive, 15m-aligned UTC interval")
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        if end_ms > now_ms:
            raise BacktestUnavailable("EVALUATION_WINDOW_IN_FUTURE")
        execution_count = (end_ms - start_ms) // INTERVAL_MS["1m"]
        evaluation_15m_count = (end_ms - start_ms) // INTERVAL_MS["15m"]
        if not 1 <= execution_count <= MAX_EXECUTION_BARS:
            raise ValueError(f"Execution window must contain 1..{MAX_EXECUTION_BARS} 1m bars")

        warmup_15m_start = start_ms - WARMUP_15M_BARS * INTERVAL_MS["15m"]
        if warmup_15m_start < 0:
            raise BacktestUnavailable("15M_WARMUP_BEFORE_EPOCH")

        hour_ms = INTERVAL_MS["1h"]
        first_completed_hour = (start_ms // hour_ms) * hour_ms
        last_completed_hour = (end_ms // hour_ms) * hour_ms
        warmup_1h_start = first_completed_hour - WARMUP_1H_BARS * hour_ms
        if warmup_1h_start < 0:
            raise BacktestUnavailable("1H_WARMUP_BEFORE_EPOCH")
        context_1h_count = (last_completed_hour - warmup_1h_start) // hour_ms
        signal_15m_count = WARMUP_15M_BARS + evaluation_15m_count

        _require_ready(self.candle_store, symbol, "1m", end_ms, execution_count)
        _require_ready(self.candle_store, symbol, "15m", end_ms, signal_15m_count)
        _require_ready(self.candle_store, symbol, "1h", last_completed_hour, context_1h_count)

        execution = _load_candles(
            self.engine,
            symbol=symbol,
            timeframe="1m",
            start_ms=start_ms,
            end_ms=end_ms,
            expected_bars=execution_count,
        )
        signal_start = warmup_15m_start
        signal_bars = _load_candles(
            self.engine,
            symbol=symbol,
            timeframe="15m",
            start_ms=signal_start,
            end_ms=end_ms,
            expected_bars=signal_15m_count,
        )
        context_bars = _load_candles(
            self.engine,
            symbol=symbol,
            timeframe="1h",
            start_ms=warmup_1h_start,
            end_ms=last_completed_hour,
            expected_bars=context_1h_count,
        )

        funding_snapshot = self.funding_store.load_window(symbol, start_ms, end_ms)
        funding_events = tuple(
            FundingEvent(
                symbol=record.symbol,
                timestamp_ms=record.funding_time_ms,
                rate=record.funding_rate,
                mark_price=record.mark_price,
                rate_type=record.rate_type,
            )
            for record in funding_snapshot.events
        )
        coverage = FundingCoverage(
            start_ms=start_ms,
            end_ms=end_ms,
            expected_timestamps_ms=funding_snapshot.expected_timestamps_ms,
            complete=funding_snapshot.complete,
        )

        execution_digest = _candle_digest(execution)
        signal_digest = _candle_digest(signal_bars)
        context_digest = _candle_digest(context_bars)
        funding_digest = sha256(
            "|".join(funding_snapshot.expected_event_fingerprints).encode("utf-8")
        ).hexdigest()
        provenance: dict[str, object] = {
            "execution_1m": {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "bar_count": len(execution),
                "sha256": execution_digest,
                "source_counts": _source_counts(execution),
            },
            "signal_15m": {
                "warmup_start_ms": signal_start,
                "evaluation_start_ms": start_ms,
                "end_ms": end_ms,
                "warmup_bars": WARMUP_15M_BARS,
                "evaluation_bars": evaluation_15m_count,
                "bar_count": len(signal_bars),
                "sha256": signal_digest,
                "source_counts": _source_counts(signal_bars),
            },
            "completed_context_1h": {
                "warmup_start_ms": warmup_1h_start,
                "context_end_ms": last_completed_hour,
                "preceding_closed_bars": WARMUP_1H_BARS,
                "bar_count": len(context_bars),
                "latest_completed_close_ms": (context_bars[-1].close_ms if context_bars else None),
                "sha256": context_digest,
                "source_counts": _source_counts(context_bars),
            },
            "funding": {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "complete_manifest": funding_snapshot.complete,
                "event_count": len(funding_events),
                "expected_timestamps_ms": list(funding_snapshot.expected_timestamps_ms),
                "source": "BINANCE_PUBLIC_FAPI_FUNDING_RATE",
                "fingerprints_sha256": funding_digest,
            },
        }
        warmup = {
            "required_preceding_15m_bars": WARMUP_15M_BARS,
            "present_15m_bars": len(signal_bars),
            "evaluation_15m_bars": evaluation_15m_count,
            "required_preceding_closed_1h_bars": WARMUP_1H_BARS,
            "present_1h_bars": len(context_bars),
            "first_completed_hour_ms": first_completed_hour,
            "last_completed_hour_ms": last_completed_hour,
        }
        return _LoadedInputs(
            execution_bars=execution,
            signal_bars_15m=signal_bars,
            context_bars_1h=context_bars,
            funding_events=funding_events,
            funding_coverage=coverage,
            provenance=provenance,
            warmup=warmup,
        )

    def run(self, symbol: str, start_ms: int, end_ms: int) -> S01ResearchReport:
        """Run an exact historical sample; return UNAVAILABLE for any data-quality gap."""
        if (
            symbol not in SYMBOLS
            or type(start_ms) is not int
            or type(end_ms) is not int
            or start_ms < 0
            or end_ms <= start_ms
            or start_ms % INTERVAL_MS["15m"]
            or end_ms % INTERVAL_MS["15m"]
        ):
            raise ValueError("Research window must be a positive, 15m-aligned UTC interval")
        try:
            inputs = self._load_inputs(symbol, start_ms, end_ms)
            signals = self.strategy.generate_signals(
                list(inputs.signal_bars_15m),
                list(inputs.context_bars_1h),
            )
            result = BacktestEngine().run(
                inputs.execution_bars,
                signals,
                inputs.funding_events,
                inputs.funding_coverage,
                self.costs,
                self.config,
            )
        except (BacktestUnavailable, CandleGap, FundingGap, InvalidCandle, ValueError) as exc:
            return S01ResearchReport(
                status="UNAVAILABLE",
                symbol=symbol,
                start_ms=start_ms,
                end_ms=end_ms,
                strategy_version=self.strategy.config.strategy_version,
                strategy_parameters=self.strategy_parameters,
                reason=str(exc),
                warmup={},
                data_provenance={},
                cost_assumptions=self.cost_assumptions,
                result=None,
            )

        return S01ResearchReport(
            status="READY",
            symbol=symbol,
            start_ms=start_ms,
            end_ms=end_ms,
            strategy_version=self.strategy.config.strategy_version,
            strategy_parameters=self.strategy_parameters,
            reason=None,
            warmup=inputs.warmup,
            data_provenance=inputs.provenance,
            cost_assumptions=self.cost_assumptions,
            result=result,
        )


def _decimal_argument(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("Expected a decimal number") from exc
    if not result.is_finite():
        raise argparse.ArgumentTypeError("Expected a finite decimal number")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one read-only, reproducible S01 historical backtest"
    )
    parser.add_argument("--symbol", required=True, choices=sorted(SYMBOLS))
    parser.add_argument("--start-ms", required=True, type=int)
    parser.add_argument("--end-ms", required=True, type=int)
    parser.add_argument("--taker-fee-bps", required=True, type=_decimal_argument)
    parser.add_argument("--slippage-bps", required=True, type=_decimal_argument)
    parser.add_argument("--initial-equity", type=_decimal_argument, default=Decimal("10000"))
    parser.add_argument("--risk-fraction", type=_decimal_argument, default=Decimal("0.005"))
    return parser.parse_args()


def main() -> None:
    """Manual PAPER-only CLI. Database access is read-only; output goes to stdout."""
    args = parse_args()
    settings = get_settings()
    if settings.environment != "PAPER" or settings.live_enabled:
        raise RuntimeError("Historical research is permitted only in PAPER mode")
    costs = CostModel(args.taker_fee_bps, args.slippage_bps)
    config = BacktestConfig(
        initial_equity=args.initial_equity,
        risk_fraction=args.risk_fraction,
        max_notional_fraction=Decimal(1),
    )
    engine = make_engine(settings)
    try:
        report = S01ResearchService(engine, costs=costs, config=config).run(
            args.symbol,
            args.start_ms,
            args.end_ms,
        )
        print(report_json(report))
        if report.status != "READY":
            raise SystemExit(2)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
