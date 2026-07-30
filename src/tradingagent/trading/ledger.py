"""Atomic in-memory Decimal ledger implementing portfolio invariants."""

from dataclasses import dataclass
from datetime import datetime

from tradingagent.domain.models import LedgerEntry, Portfolio
from tradingagent.trading.execution import OrderSide, SimulatedFill


class LedgerInvariantError(ValueError):
    """Raised when an entire fill transaction must be rejected."""


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    """Immutable transaction boundary suitable for persistence."""

    portfolio: Portfolio
    entries: tuple[LedgerEntry, ...]
    fills: tuple[SimulatedFill, ...]


class AtomicLedger:
    """Apply a fill all-or-nothing while preserving long/flat balances."""

    def __init__(self, initial_portfolio: Portfolio) -> None:
        self._snapshot = LedgerSnapshot(initial_portfolio, (), ())

    def snapshot(self) -> LedgerSnapshot:
        """Return the last committed ledger state."""
        return self._snapshot

    def apply_fill(self, fill: SimulatedFill, *, occurred_at: datetime) -> LedgerSnapshot:
        """Validate and atomically commit trade movements and a separate fee."""
        before = self._snapshot
        entries: list[LedgerEntry] = []
        if fill.side is OrderSide.BUY:
            cash = before.portfolio.cash - fill.notional - fill.fee
            btc = before.portfolio.btc + fill.quantity
            entries.extend(
                (
                    LedgerEntry("trade", "USDT", -fill.notional, occurred_at, fill.fill_id),
                    LedgerEntry("trade", "BTC", fill.quantity, occurred_at, fill.fill_id),
                )
            )
        else:
            if fill.quantity > before.portfolio.btc:
                raise LedgerInvariantError("sale exceeds available BTC")
            cash = before.portfolio.cash + fill.notional - fill.fee
            btc = before.portfolio.btc - fill.quantity
            entries.extend(
                (
                    LedgerEntry("trade", "BTC", -fill.quantity, occurred_at, fill.fill_id),
                    LedgerEntry("trade", "USDT", fill.notional, occurred_at, fill.fill_id),
                )
            )
        entries.append(LedgerEntry("fee", "USDT", -fill.fee, occurred_at, fill.fill_id))
        try:
            portfolio = Portfolio(cash=cash, btc=btc)
        except ValueError as exc:
            raise LedgerInvariantError(str(exc)) from exc
        committed = LedgerSnapshot(
            portfolio,
            (*before.entries, *entries),
            (*before.fills, fill),
        )
        self._snapshot = committed
        return committed
