"""Persist public market stream health across the collector and API processes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_market_stream"
down_revision: str = "0001_public"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_stream_state",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("last_message_ms", sa.BigInteger()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market_stream_state")),
    )


def downgrade() -> None:
    op.drop_table("market_stream_state")
