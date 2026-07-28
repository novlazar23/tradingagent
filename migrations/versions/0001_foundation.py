"""Create the complete trading-agent persistence contract."""

from collections.abc import Sequence

from alembic import op

from tradingagent.persistence.models import Base

revision: str = "0001_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create all domain, runtime, audit and idempotency tables and indexes."""
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    """Drop all application tables in dependency-safe metadata order."""
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)
