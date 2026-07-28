"""Create versioned configuration and Decimal ledger foundation."""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import JSON, Column, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint

revision: str = "0001_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the initial schema without destructive or live-trading constructs."""
    op.create_table(
        "schema_versions",
        Column("version", String(64), primary_key=True),
        Column("applied_at", DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "configuration_snapshots",
        Column("id", String(36), primary_key=True),
        Column("version_hash", String(64), nullable=False, unique=True),
        Column("payload", JSON, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "orders",
        Column("id", String(36), primary_key=True),
        Column("session_id", String(36), nullable=False, index=True),
        Column("side", String(4), nullable=False),
        Column("status", String(16), nullable=False),
        Column("quantity", Numeric(38, 18), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("idempotency_key", String(128), nullable=False, unique=True),
    )
    op.create_table(
        "fills",
        Column("id", String(36), primary_key=True),
        Column("order_id", String(36), ForeignKey("orders.id"), nullable=False),
        Column("price", Numeric(38, 18), nullable=False),
        Column("quantity", Numeric(38, 18), nullable=False),
        Column("fee", Numeric(38, 18), nullable=False),
        Column("occurred_at", DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "cash_ledger",
        Column("id", String(36), primary_key=True),
        Column("session_id", String(36), nullable=False, index=True),
        Column("entry_type", String(24), nullable=False),
        Column("asset", String(4), nullable=False),
        Column("amount", Numeric(38, 18), nullable=False),
        Column("reference_id", String(128), nullable=False),
        Column("occurred_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint("reference_id", "entry_type", name="uq_ledger_ref_type"),
    )
    op.create_table(
        "positions",
        Column("session_id", String(36), primary_key=True),
        Column("btc_quantity", Numeric(38, 18), nullable=False),
        Column("average_entry_price", Numeric(38, 18)),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "audit_events",
        Column("id", String(36), primary_key=True),
        Column("event_type", String(64), nullable=False, index=True),
        Column("subject_id", String(128), nullable=False),
        Column("payload", JSON, nullable=False),
        Column("occurred_at", DateTime(timezone=True), nullable=False),
        Column("correlation_id", String(128)),
        Column("message", Text),
    )


def downgrade() -> None:
    """Drop initial tables in reverse dependency order."""
    for table in (
        "audit_events",
        "positions",
        "cash_ledger",
        "fills",
        "orders",
        "configuration_snapshots",
        "schema_versions",
    ):
        op.drop_table(table)
