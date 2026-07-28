"""Long/flat risk sizing, loss limits and persistent drawdown kill switch."""

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256

from tradingagent.config import RiskConfig
from tradingagent.domain.models import Portfolio
from tradingagent.trading.execution import ExecutionModel


@dataclass(frozen=True, slots=True)
class RiskAuditEvent:
    """Immutable risk state transition for the session audit trail."""

    event_type: str
    occurred_at: datetime
    details: str


@dataclass(frozen=True, slots=True)
class SessionRiskState:
    """Persistable state required to enforce limits across restarts."""

    peak_equity: Decimal
    paused: bool
    pause_reason: str | None
    entries_by_day: tuple[tuple[date, int], ...]
    daily_start_equity: tuple[tuple[date, Decimal], ...]
    last_exit_at: datetime | None
    audit_events: tuple[RiskAuditEvent, ...]

    @classmethod
    def initial(cls, equity: Decimal) -> "SessionRiskState":
        """Create risk state for a positive starting portfolio."""
        if equity <= 0:
            raise ValueError("initial equity must be positive")
        return cls(equity, False, None, (), (), None, ())


@dataclass(frozen=True, slots=True)
class RiskApproval:
    """Decision from the sole component authorized to release order intent."""

    approved: bool
    risk_check_id: str
    quantity: Decimal
    stop_price: Decimal | None
    take_profit_price: Decimal | None
    reasons: tuple[str, ...]


class RiskEngine:
    """Enforce configured sizing and long/flat portfolio invariants."""

    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def approve_entry(
        self,
        *,
        portfolio: Portfolio,
        state: SessionRiskState,
        entry_price: Decimal,
        estimated_fee_rate: Decimal,
        execution_model: ExecutionModel | None = None,
        now: datetime,
        atr: Decimal | None = None,
    ) -> RiskApproval:
        """Size an entry to the smallest risk, exposure and cash limit."""
        self._require_utc(now)
        reasons: list[str] = []
        if state.paused:
            reasons.append(f"session paused: {state.pause_reason}")
        if portfolio.btc > 0:
            reasons.append("existing long position prevents another entry")
        if entry_price <= 0 or estimated_fee_rate < 0:
            reasons.append("invalid price or fee")
        day_entries = dict(state.entries_by_day).get(now.date(), 0)
        if day_entries >= self.config.maximum_entries_per_utc_day:
            reasons.append("daily entry limit reached")
        if state.last_exit_at is not None:
            elapsed = (now - state.last_exit_at).total_seconds()
            if elapsed < self.config.cooldown_seconds:
                reasons.append("risk cooldown active")
        stop_distance = self._stop_distance(entry_price, atr)
        if stop_distance <= 0:
            reasons.append("stop distance must be positive")
        if reasons:
            return self._approval(False, Decimal(0), None, None, reasons)

        equity = portfolio.equity(entry_price)
        risk_quantity = equity * self.config.risk_per_trade_fraction / stop_distance
        exposure_quantity = equity * self.config.maximum_position_fraction / entry_price
        reserve = equity * self.config.minimum_cash_reserve_fraction
        unit_cash_cost = (
            execution_model.worst_buy_unit_cost(reference_price=entry_price, atr=atr)
            if execution_model is not None
            else entry_price * (Decimal(1) + estimated_fee_rate)
        )
        cash_quantity = max(
            Decimal(0),
            (portfolio.cash - reserve) / unit_cash_cost,
        )
        quantity = min(risk_quantity, exposure_quantity, cash_quantity)
        if execution_model is not None:
            quantity = execution_model.round_quantity(quantity)
        if quantity <= 0:
            return self._approval(False, Decimal(0), None, None, ["insufficient cash"])
        stop = entry_price - stop_distance
        take_profit = (
            entry_price
            + self._configured_distance(
                entry_price,
                self.config.take_profit_distance,
                atr,
            )
            if self.config.take_profit_distance is not None
            else None
        )
        return self._approval(True, quantity, stop, take_profit, ["all risk limits passed"])

    def approve_exit(self, *, portfolio: Portfolio, requested_quantity: Decimal) -> RiskApproval:
        """Approve only a positive sale no larger than available BTC."""
        reasons = []
        if requested_quantity <= 0:
            reasons.append("exit quantity must be positive")
        if requested_quantity > portfolio.btc:
            reasons.append("sale exceeds available BTC")
        return self._approval(
            not reasons,
            requested_quantity if not reasons else Decimal(0),
            None,
            None,
            reasons or ["risk-reducing exit approved"],
        )

    def observe_equity(
        self, state: SessionRiskState, equity: Decimal, now: datetime
    ) -> SessionRiskState:
        """Latch the kill switch on session drawdown or UTC-day loss."""
        self._require_utc(now)
        if equity < 0:
            raise ValueError("equity cannot be negative")
        peak = max(state.peak_equity, equity)
        drawdown = (peak - equity) / peak
        daily_starts = dict(state.daily_start_equity)
        daily_start = daily_starts.setdefault(now.date(), equity)
        daily_loss = (daily_start - equity) / daily_start
        updated_starts = tuple(sorted(daily_starts.items(), key=lambda item: item[0]))
        if state.paused:
            return replace(state, peak_equity=peak, daily_start_equity=updated_starts)
        if drawdown >= self.config.maximum_session_drawdown_fraction:
            event_type = "drawdown_kill_switch"
            pause_reason = "maximum session drawdown exceeded"
            details = f"drawdown={drawdown}; limit={self.config.maximum_session_drawdown_fraction}"
        elif daily_loss >= self.config.maximum_daily_loss_fraction:
            event_type = "daily_loss_kill_switch"
            pause_reason = "maximum daily loss exceeded"
            details = f"daily_loss={daily_loss}; limit={self.config.maximum_daily_loss_fraction}"
        else:
            return replace(state, peak_equity=peak, daily_start_equity=updated_starts)
        event = RiskAuditEvent(event_type, now, details)
        return replace(
            state,
            peak_equity=peak,
            paused=True,
            pause_reason=pause_reason,
            daily_start_equity=updated_starts,
            audit_events=(*state.audit_events, event),
        )

    def record_entry(self, state: SessionRiskState, now: datetime) -> SessionRiskState:
        """Increment the persistent UTC-day entry counter after a committed fill."""
        self._require_utc(now)
        entries = dict(state.entries_by_day)
        entries[now.date()] = entries.get(now.date(), 0) + 1
        return replace(
            state,
            entries_by_day=tuple(sorted(entries.items(), key=lambda item: item[0])),
        )

    def record_exit(self, state: SessionRiskState, now: datetime) -> SessionRiskState:
        """Record an exit timestamp used by the persistent cooldown check."""
        self._require_utc(now)
        return replace(state, last_exit_at=now)

    def resume(self, state: SessionRiskState, *, actor: str) -> SessionRiskState:
        """Explicitly clear a latched kill switch and record the administrator."""
        if not actor:
            raise ValueError("resume actor is required")
        event = RiskAuditEvent(
            "risk_resume",
            datetime.now(UTC),
            f"explicit resume by {actor}",
        )
        return replace(
            state,
            paused=False,
            pause_reason=None,
            audit_events=(*state.audit_events, event),
        )

    def _stop_distance(self, price: Decimal, atr: Decimal | None) -> Decimal:
        return self._configured_distance(price, self.config.stop_distance, atr)

    def _configured_distance(
        self, price: Decimal, configured: Decimal | None, atr: Decimal | None
    ) -> Decimal:
        if configured is None:
            return Decimal(0)
        if self.config.stop_mode == "percent":
            return price * configured
        if atr is None or atr <= 0:
            return Decimal(0)
        return atr * configured

    def _approval(
        self,
        approved: bool,
        quantity: Decimal,
        stop: Decimal | None,
        take_profit: Decimal | None,
        reasons: list[str],
    ) -> RiskApproval:
        identifier = sha256(
            f"{approved}:{quantity}:{stop}:{take_profit}:{'|'.join(reasons)}".encode()
        ).hexdigest()
        return RiskApproval(approved, identifier, quantity, stop, take_profit, tuple(reasons))

    @staticmethod
    def _require_utc(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("risk timestamps must be timezone-aware UTC")
