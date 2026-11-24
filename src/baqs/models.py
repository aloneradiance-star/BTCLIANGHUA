"""Public market data and versioned, auditable regime snapshots."""

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from baqs.db import Base

json_type = JSON().with_variant(JSONB(), "postgresql")


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (
        CheckConstraint("high >= low", name="high_low"),
        CheckConstraint("high >= open AND high >= close", name="high_ohlc"),
        CheckConstraint("low <= open AND low <= close", name="low_ohlc"),
        CheckConstraint("volume >= 0", name="volume_nonnegative"),
        CheckConstraint("timeframe IN ('1m','5m','15m','1h')", name="timeframe"),
        Index("ix_candles_recent", "symbol", "timeframe", "open_time"),
    )

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    close_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    quote_volume: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    trade_count: Mapped[int | None]
    is_final: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RegimeSnapshot(Base):
    __tablename__ = "regime_snapshots"
    __table_args__ = (
        CheckConstraint(
            "regime IN ('BULL_TREND','BEAR_TREND','RANGE','BREAKOUT','HIGH_RISK','TRANSITION')",
            name="valid_regime",
        ),
        UniqueConstraint("symbol", "timeframe", "candle_close_time", "classifier_version"),
        Index("ix_regimes_recent", "symbol", "timeframe", "candle_close_time"),
        ForeignKeyConstraint(
            ["symbol", "timeframe", "candle_open_time"],
            ["candles.symbol", "candles.timeframe", "candles.open_time"],
            name="fk_regime_candle",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    candle_open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    candle_close_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    regime: Mapped[str] = mapped_column(String(24), nullable=False)
    features: Mapped[dict[str, object]] = mapped_column(json_type, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(json_type, nullable=False)
    classifier_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FundingRate(Base):
    """Immutable public funding event; rate_type is part of the natural key."""

    __tablename__ = "funding_rates"
    __table_args__ = (
        CheckConstraint("mark_price > 0", name="mark_price_positive"),
        CheckConstraint("rate_type IN ('Regular','Special')", name="valid_rate_type"),
        Index("ix_funding_rates_recent", "symbol", "funding_time"),
    )

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    funding_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    rate_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    funding_rate: Mapped[Decimal] = mapped_column(Numeric(20, 12), nullable=False)
    mark_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    source: Mapped[str] = mapped_column(String(48), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FundingCoverageRecord(Base):
    """Immutable completeness manifest for an exact [start_time, end_time) window."""

    __tablename__ = "funding_coverage"
    __table_args__ = (
        CheckConstraint("end_time > start_time", name="valid_window"),
        CheckConstraint("event_count >= 0", name="event_count_nonnegative"),
        Index("ix_funding_coverage_recent", "symbol", "end_time"),
    )

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    expected_timestamps_ms: Mapped[list[int]] = mapped_column(json_type, nullable=False)
    expected_event_fingerprints: Mapped[list[str]] = mapped_column(json_type, nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source: Mapped[str] = mapped_column(String(48), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FundingEventRecord(Base):
    """Exact public funding history; mark_price is the exchange's charge-time mark."""

    __tablename__ = "funding_events"
    __table_args__ = (
        UniqueConstraint("symbol", "funding_time_ms", name="uq_funding_symbol_time"),
        CheckConstraint("mark_price > 0", name="funding_mark_positive"),
        CheckConstraint("rate_type IN ('Regular','Special')", name="funding_rate_type"),
        Index("ix_funding_recent", "symbol", "funding_time_ms"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    funding_time_ms: Mapped[int] = mapped_column(nullable=False)
    funding_rate: Mapped[Decimal] = mapped_column(Numeric(28, 18), nullable=False)
    mark_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    rate_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="BINANCE_PUBLIC_REST")


class FundingCoverageManifest(Base):
    """A range is READY only after pagination exhaustion and exact persisted timestamp match."""

    __tablename__ = "funding_coverage_manifests"
    __table_args__ = (
        UniqueConstraint("symbol", "start_ms", "end_ms", name="uq_funding_coverage_window"),
        CheckConstraint("end_ms > start_ms", name="funding_window_positive"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    start_ms: Mapped[int] = mapped_column(nullable=False)
    end_ms: Mapped[int] = mapped_column(nullable=False)
    expected_timestamps_ms: Mapped[list[int]] = mapped_column(json_type, nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_exhausted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
