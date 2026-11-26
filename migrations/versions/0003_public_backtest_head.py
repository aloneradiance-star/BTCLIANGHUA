"""Join the public market stream and historical funding schema revisions."""

revision: str = "0003_public_backtest"
down_revision: tuple[str, str] = ("0002_market_stream", "0002_backtest_funding")
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    """Record both schema revisions as applied."""


def downgrade() -> None:
    """Restore the independent revision markers."""
