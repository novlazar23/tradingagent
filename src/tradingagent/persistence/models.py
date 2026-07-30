"""Complete durable SQLAlchemy contract for the trading-agent runtime."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DECIMAL = Numeric(38, 18)


class Base(DeclarativeBase):
    """Declarative metadata root used by migrations and repositories."""


class SchemaVersion(Base):
    __tablename__ = "schema_versions"
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConfigurationSnapshot(Base):
    __tablename__ = "configuration_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    version_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MarketDataset(Base):
    __tablename__ = "market_datasets"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_dataset_source_id"),
        Index("ix_market_datasets_page", "id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, default="BTC/USDT")
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CandleRecord(Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "dataset_id",
            "symbol",
            "timeframe",
            "open_time",
            name="uq_candles_identity",
        ),
        Index("ix_candles_lookup", "dataset_id", "symbol", "timeframe", "open_time"),
        Index(
            "ix_candles_closed_close",
            "dataset_id",
            "timeframe",
            "is_closed",
            "close_time",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("market_datasets.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(4), nullable=False)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    close_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(DECIMAL, nullable=False)
    high: Mapped[Decimal] = mapped_column(DECIMAL, nullable=False)
    low: Mapped[Decimal] = mapped_column(DECIMAL, nullable=False)
    close: Mapped[Decimal] = mapped_column(DECIMAL, nullable=False)
    volume: Mapped[Decimal] = mapped_column(DECIMAL, nullable=False)
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CandleRevision(Base):
    __tablename__ = "candle_revisions"
    __table_args__ = (Index("ix_candle_revisions_candle_time", "candle_id", "revised_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candle_id: Mapped[str] = mapped_column(ForeignKey("candles.id"), nullable=False)
    before: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    after: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    revised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DataGap(Base):
    __tablename__ = "data_gaps"
    __table_args__ = (
        UniqueConstraint("dataset_id", "timeframe", "start_time", name="uq_data_gap"),
        Index("ix_data_gaps_open", "dataset_id", "resolved_at"),
        Index("ix_data_gaps_page", "dataset_id", "id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("market_datasets.id"), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(4), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StrategyDefinition(Base):
    __tablename__ = "strategy_definitions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    configuration: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    configuration_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FeatureValue(Base):
    __tablename__ = "feature_values"
    __table_args__ = (
        UniqueConstraint("candle_id", "feature_name", "version_hash", name="uq_feature_cache"),
        Index("ix_feature_values_candle", "candle_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candle_id: Mapped[str] = mapped_column(ForeignKey("candles.id"), nullable=False)
    feature_name: Mapped[str] = mapped_column(String(128), nullable=False)
    version_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    request: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    result: Mapped[dict[str, object] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PaperSession(Base):
    __tablename__ = "paper_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    request: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    last_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SignalDecisionRecord(Base):
    __tablename__ = "signal_decisions"
    __table_args__ = (
        UniqueConstraint("paper_session_id", "candle_id", name="uq_paper_candle_decision"),
        Index("ix_signal_decisions_session_time", "paper_session_id", "decided_at"),
        Index("ix_signal_decisions_page", "paper_session_id", "id"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    paper_session_id: Mapped[str | None] = mapped_column(ForeignKey("paper_sessions.id"))
    backtest_run_id: Mapped[str | None] = mapped_column(ForeignKey("backtest_runs.id"))
    candle_id: Mapped[str | None] = mapped_column(ForeignKey("candles.id"))
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    score: Mapped[Decimal] = mapped_column(DECIMAL, nullable=False)
    explanation: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BacktestEvent(Base):
    __tablename__ = "backtest_events"
    __table_args__ = (Index("ix_backtest_events_run_time", "run_id", "event_time"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("backtest_runs.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)


class BacktestMetric(Base):
    __tablename__ = "backtest_metrics"
    __table_args__ = (UniqueConstraint("run_id", "name", name="uq_backtest_metric"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("backtest_runs.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[Decimal | None] = mapped_column(DECIMAL)
    payload: Mapped[dict[str, object] | None] = mapped_column(JSON)


class OrderRecord(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("paper_sessions.id"), nullable=False, index=True
    )
    decision_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("signal_decisions.id"))
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(DECIMAL, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)


class FillRecord(Base):
    __tablename__ = "fills"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.id"), nullable=False, unique=True, index=True
    )
    price: Mapped[Decimal] = mapped_column(DECIMAL, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(DECIMAL, nullable=False)
    fee: Mapped[Decimal] = mapped_column(DECIMAL, nullable=False)
    details: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PositionRecord(Base):
    __tablename__ = "positions"
    session_id: Mapped[str] = mapped_column(ForeignKey("paper_sessions.id"), primary_key=True)
    btc_quantity: Mapped[Decimal] = mapped_column(DECIMAL, nullable=False)
    average_entry_price: Mapped[Decimal | None] = mapped_column(DECIMAL)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CashLedgerRecord(Base):
    __tablename__ = "cash_ledger"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "reference_id",
            "entry_type",
            "asset",
            name="uq_ledger_ref_type_asset",
        ),
        Index("ix_cash_ledger_session_time", "session_id", "occurred_at"),
        Index("ix_cash_ledger_page", "session_id", "id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("paper_sessions.id"), nullable=False)
    entry_type: Mapped[str] = mapped_column(String(24), nullable=False)
    asset: Mapped[str] = mapped_column(String(4), nullable=False)
    amount: Mapped[Decimal] = mapped_column(DECIMAL, nullable=False)
    reference_id: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    __table_args__ = (UniqueConstraint("session_id", "observed_at", name="uq_equity_session_time"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("paper_sessions.id"), nullable=False)
    equity: Mapped[Decimal] = mapped_column(DECIMAL, nullable=False)
    cash: Mapped[Decimal] = mapped_column(DECIMAL, nullable=False)
    btc_quantity: Mapped[Decimal] = mapped_column(DECIMAL, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JobRecord(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("kind", "idempotency_key", name="uq_job_kind_idempotency"),
        Index("ix_jobs_claim", "status", "available_at", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    maximum_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[dict[str, object] | None] = mapped_column(JSON)
    error_class: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditEventRecord(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_subject_time", "subject_id", "occurred_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    message: Mapped[str | None] = mapped_column(Text)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("operation", "idempotency_key", name="uq_idempotency_operation_key"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    result_type: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
