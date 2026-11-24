"""Create public candles and regime snapshots.

Revision ID: 0001_public
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001_public"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

json_type = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "candles",
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("close_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(28, 12), nullable=False),
        sa.Column("high", sa.Numeric(28, 12), nullable=False),
        sa.Column("low", sa.Numeric(28, 12), nullable=False),
        sa.Column("close", sa.Numeric(28, 12), nullable=False),
        sa.Column("volume", sa.Numeric(28, 12), nullable=False),
        sa.Column("quote_volume", sa.Numeric(28, 12)),
        sa.Column("trade_count", sa.Integer()),
        sa.Column("is_final", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("symbol", "timeframe", "open_time", name="pk_candles"),
        # Pin the names from the original migration, including the applied prefix.
        sa.CheckConstraint("high >= low", name=op.f("ck_candles_ck_candles_high_low")),
        sa.CheckConstraint(
            "high >= open AND high >= close", name=op.f("ck_candles_ck_candles_high_ohlc")
        ),
        sa.CheckConstraint(
            "low <= open AND low <= close", name=op.f("ck_candles_ck_candles_low_ohlc")
        ),
        sa.CheckConstraint("volume >= 0", name=op.f("ck_candles_ck_candles_volume_nonnegative")),
        sa.CheckConstraint(
            "timeframe IN ('1m','5m','15m','1h')", name=op.f("ck_candles_ck_candles_timeframe")
        ),
    )
    op.create_index("ix_candles_recent", "candles", ["symbol", "timeframe", "open_time"])
    op.create_table(
        "regime_snapshots",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("candle_open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("candle_close_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("regime", sa.String(24), nullable=False),
        sa.Column("features", json_type, nullable=False),
        sa.Column("reasons", json_type, nullable=False),
        sa.Column("classifier_version", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_regime_snapshots"),
        sa.ForeignKeyConstraint(
            ["symbol", "timeframe", "candle_open_time"],
            ["candles.symbol", "candles.timeframe", "candles.open_time"],
            name="fk_regime_candle",
        ),
        sa.UniqueConstraint(
            "symbol",
            "timeframe",
            "candle_close_time",
            "classifier_version",
            name="uq_regime_snapshots_symbol",
        ),
        sa.CheckConstraint(
            "regime IN ('BULL_TREND','BEAR_TREND','RANGE','BREAKOUT','HIGH_RISK','TRANSITION')",
            name=op.f("ck_regime_snapshots_ck_regime_snapshots_valid_regime"),
        ),
    )
    op.create_index(
        "ix_regimes_recent",
        "regime_snapshots",
        ["symbol", "timeframe", "candle_close_time"],
    )


def downgrade() -> None:
    op.drop_index("ix_regimes_recent", table_name="regime_snapshots")
    op.drop_table("regime_snapshots")
    op.drop_index("ix_candles_recent", table_name="candles")
    op.drop_table("candles")
