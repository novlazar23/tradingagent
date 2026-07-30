"""Shared Decimal-only execution cost and simulated fill model."""

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from hashlib import sha256

from tradingagent.config import CostConfig

BPS = Decimal("10000")


class OrderSide(StrEnum):
    """Supported long/flat order directions."""

    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    """Fully decomposed deterministic fill with audit lineage."""

    fill_id: str
    side: OrderSide
    reference_price: Decimal
    fill_price: Decimal
    quantity: Decimal
    notional: Decimal
    fee: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    execution_model_version: str
    decision_id: str | None = None
    risk_check_id: str | None = None


class ExecutionModel:
    """Apply spread, fixed/ATR slippage, precision and taker fees."""

    def __init__(self, config: CostConfig, version: str = "execution-v1") -> None:
        self.config = config
        self.version = version
        self._rounding = {
            "ROUND_DOWN": ROUND_DOWN,
            "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
        }[config.rounding_mode]

    def fill(
        self,
        *,
        side: OrderSide,
        reference_price: Decimal,
        requested_quantity: Decimal,
        atr: Decimal | None,
        fill_id: str | None = None,
        decision_id: str | None = None,
        risk_check_id: str | None = None,
    ) -> SimulatedFill:
        """Simulate a market fill and expose each modeled cost separately."""
        if reference_price <= 0 or requested_quantity <= 0:
            raise ValueError("reference price and requested quantity must be positive")
        half_spread = reference_price * self.config.spread_bps / (BPS * 2)
        if self.config.slippage_model == "fixed_bps":
            slippage = reference_price * self.config.slippage_bps / BPS
        else:
            if atr is None or atr <= 0:
                raise ValueError("positive ATR is required for atr_scaled slippage")
            assert self.config.atr_slippage_multiplier is not None
            slippage = atr * self.config.atr_slippage_multiplier
        direction = Decimal(1) if side is OrderSide.BUY else Decimal(-1)
        raw_price = reference_price + direction * (half_spread + slippage)
        fill_price = raw_price.quantize(self.config.price_quantum, rounding=self._rounding)
        quantity = requested_quantity.quantize(
            self.config.quantity_quantum, rounding=self._rounding
        )
        if quantity <= 0:
            raise ValueError("quantity rounds to zero")
        notional = fill_price * quantity
        if notional < self.config.minimum_order_value:
            raise ValueError("order value is below configured minimum")
        fee = notional * self.config.taker_fee_rate
        generated_id = sha256(
            f"{self.version}:{side}:{reference_price}:{quantity}:{fill_price}".encode()
        ).hexdigest()
        return SimulatedFill(
            fill_id=fill_id or generated_id,
            side=side,
            reference_price=reference_price,
            fill_price=fill_price,
            quantity=quantity,
            notional=notional,
            fee=fee,
            spread_cost=half_spread * quantity,
            slippage_cost=slippage * quantity,
            execution_model_version=self.version,
            decision_id=decision_id,
            risk_check_id=risk_check_id,
        )

    def worst_buy_unit_cost(self, *, reference_price: Decimal, atr: Decimal | None) -> Decimal:
        """Return the rounded all-in cash cost of one BTC for entry sizing."""
        if reference_price <= 0:
            raise ValueError("reference price must be positive")
        half_spread = reference_price * self.config.spread_bps / (BPS * 2)
        if self.config.slippage_model == "fixed_bps":
            slippage = reference_price * self.config.slippage_bps / BPS
        else:
            if atr is None or atr <= 0:
                raise ValueError("positive ATR is required for atr_scaled slippage")
            assert self.config.atr_slippage_multiplier is not None
            slippage = atr * self.config.atr_slippage_multiplier
        fill_price = (reference_price + half_spread + slippage).quantize(
            self.config.price_quantum, rounding=self._rounding
        )
        return fill_price * (Decimal(1) + self.config.taker_fee_rate)

    def round_quantity(self, quantity: Decimal) -> Decimal:
        """Apply the exact execution quantity precision before risk approval."""
        return quantity.quantize(self.config.quantity_quantum, rounding=self._rounding)
