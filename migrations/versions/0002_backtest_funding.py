"""Add immutable public funding events and exact completeness manifests.

Revision ID: 0002_backtest_funding
Revises: 0001_public
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002_backtest_funding"
down_revision: str | None = "0001_public"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

json_type = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "funding_rates",
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("funding_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rate_type", sa.String(16), nullable=False),
        sa.Column("funding_rate", sa.Numeric(20, 12), nullable=False),
        sa.Column("mark_price", sa.Numeric(28, 12), nullable=False),
        sa.Column("source", sa.String(48), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("symbol", "funding_time", "rate_type", name="pk_funding_rates"),
        sa.CheckConstraint("mark_price > 0", name="ck_funding_rates_mark_price_positive"),
        sa.CheckConstraint(
            "rate_type IN ('Regular','Special')",
            name="ck_funding_rates_valid_rate_type",
        ),
    )
    op.create_index("ix_funding_rates_recent", "funding_rates", ["symbol", "funding_time"])
    op.create_table(
        "funding_coverage",
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_timestamps_ms", json_type, nullable=False),
        sa.Column("expected_event_fingerprints", json_type, nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(48), nullable=False),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("symbol", "start_time", "end_time", name="pk_funding_coverage"),
        sa.CheckConstraint("end_time > start_time", name="ck_funding_coverage_valid_window"),
        sa.CheckConstraint("event_count >= 0", name="ck_funding_coverage_event_count_nonnegative"),
    )
    op.create_index("ix_funding_coverage_recent", "funding_coverage", ["symbol", "end_time"])


def downgrade() -> None:
    op.drop_index("ix_funding_coverage_recent", table_name="funding_coverage")
    op.drop_table("funding_coverage")
    op.drop_index("ix_funding_rates_recent", table_name="funding_rates")
    op.drop_table("funding_rates")
