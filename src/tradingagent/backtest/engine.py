"""Chronological, next-bar backtest engine sharing production execution logic."""

import json
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import overload

from tradingagent.domain.models import Candle, Portfolio
from tradingagent.trading.execution import ExecutionModel, OrderSide, SimulatedFill
from tradingagent.trading.ledger import AtomicLedger
from tradingagent.trading.pipeline import TradingPipeline
from tradingagent.trading.risk import SessionRiskState
from tradingagent.trading.strategy import DecisionAction, StrategyRequest

StrategyCallback = Callable[[Sequence[Candle], bool], "BacktestOrderIntent | None"]
ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Explicit run-level assumptions that affect reported results."""

    initial_capital: Decimal
    periods_per_year: int = 35_040
    engine_version: str = "backtest-v1"
    strategy_version: str = "unspecified"
    strategy_configuration_json: str = "{}"

    def __post_init__(self) -> None:
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if self.periods_per_year <= 0:
            raise ValueError("periods_per_year must be positive")
        if not self.engine_version:
            raise ValueError("engine_version is required")
        if not self.strategy_version:
            raise ValueError("strategy_version is required")
        try:
            parsed = json.loads(self.strategy_configuration_json)
        except json.JSONDecodeError as exc:
            raise ValueError("strategy_configuration_json must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("strategy_configuration_json must contain a JSON object")


@dataclass(frozen=True, slots=True)
class BacktestOrderIntent:
    """Order intent emitted after a closed candle and eligible on the next bar."""

    decision_id: str
    action: DecisionAction
    quantity: Decimal
    stop_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    atr: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.decision_id:
            raise ValueError("decision_id is required")
        if self.action not in (DecisionAction.ENTER_LONG, DecisionAction.EXIT_LONG):
            raise ValueError("only entry and exit intents can be executed")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.stop_price is not None and self.stop_price <= 0:
            raise ValueError("stop_price must be positive")
        if self.take_profit_price is not None and self.take_profit_price <= 0:
            raise ValueError("take_profit_price must be positive")


@dataclass(frozen=True, slots=True)
class BacktestRunSnapshot:
    """Canonical immutable inputs required to reproduce a run."""

    run_id: str
    run_fingerprint: str
    data_fingerprint: str
    engine_version: str
    execution_model_version: str
    configuration_json: str
    candle_count: int
    first_open_time: datetime
    last_close_time: datetime
    sharpe_annualization: str


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """Close-time marked portfolio state."""

    time: datetime
    cash: Decimal
    btc: Decimal
    mark_price: Decimal
    equity: Decimal
    drawdown: Decimal


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    """One completed long round-trip."""

    entry_fill: SimulatedFill
    entry_time: datetime
    exit_fill: SimulatedFill
    exit_time: datetime
    exit_reason: str
    gross_pnl: Decimal
    net_pnl: Decimal


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    """Complete performance and explicit cost report."""

    initial_equity: Decimal
    final_equity: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    gross_return: Decimal
    net_return: Decimal
    total_fees: Decimal
    total_spread: Decimal
    total_slippage: Decimal
    maximum_drawdown: Decimal
    sharpe_ratio: Decimal
    profit_factor: Decimal | None
    win_rate: Decimal
    average_win: Decimal
    average_loss: Decimal
    average_win_loss_ratio: Decimal | None
    exposure: Decimal
    turnover: Decimal
    buy_and_hold_return: Decimal
    trade_count: int
    positive_return: bool


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Self-contained deterministic report."""

    snapshot: BacktestRunSnapshot
    trades: tuple[BacktestTrade, ...]
    fills: tuple[SimulatedFill, ...]
    equity_curve: tuple[EquityPoint, ...]
    metrics: BacktestMetrics
    warnings: tuple[str, ...]


@dataclass(slots=True)
class _OpenTrade:
    fill: SimulatedFill
    time: datetime
    stop: Decimal | None
    target: Decimal | None
    entry_cost: Decimal


class _HistoryView(Sequence[Candle]):
    """Reusable read-only prefix view over immutable execution bars."""

    __slots__ = ("_bars", "_length")

    def __init__(self, bars: tuple[Candle, ...]) -> None:
        self._bars = bars
        self._length = 0

    def advance(self) -> None:
        self._length += 1

    def __len__(self) -> int:
        return self._length

    @overload
    def __getitem__(self, index: int) -> Candle: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[Candle]: ...

    def __getitem__(self, index: int | slice) -> Candle | Sequence[Candle]:
        if isinstance(index, slice):
            return self._bars[: self._length][index]
        normalized = index if index >= 0 else self._length + index
        if normalized < 0 or normalized >= self._length:
            raise IndexError("history index out of range")
        return self._bars[normalized]

    def __iter__(self) -> Iterator[Candle]:
        for index in range(self._length):
            yield self._bars[index]


class BacktestEngine:
    """Consume closed candles in order and execute decisions on the next candle."""

    def __init__(
        self,
        *,
        execution_model: ExecutionModel,
        config: BacktestConfig,
        pipeline: TradingPipeline | None = None,
    ) -> None:
        self.execution_model = execution_model
        self.config = config
        self.pipeline = pipeline
        if pipeline is not None and pipeline.execution is not execution_model:
            raise ValueError("backtest pipeline must share the configured execution model")

    def run(self, candles: Iterable[Candle], strategy: StrategyCallback) -> BacktestResult:
        """Run one deterministic simulation.

        The callback only receives candles whose close event has occurred.
        Orders it emits are queued until the following candle's open.
        """
        bars = tuple(candles)
        self._validate_bars(bars)
        snapshot = self._snapshot(bars)
        ledger = AtomicLedger(Portfolio(self.config.initial_capital, ZERO))
        pending: BacktestOrderIntent | None = None
        open_trade: _OpenTrade | None = None
        trades: list[BacktestTrade] = []
        curve: list[EquityPoint] = []
        peak = self.config.initial_capital
        exposed_bars = 0
        history = _HistoryView(bars)
        risk_state = SessionRiskState.initial(self.config.initial_capital)

        for index, bar in enumerate(bars):
            history.advance()
            if pending is not None:
                open_trade = self._execute_intent(
                    pending, bar, ledger, open_trade, trades, reason="signal"
                )
                pending = None

            if open_trade is not None:
                exposed_bars += 1
                reason, trigger = self._protective_trigger(bar, open_trade)
                if reason is not None and trigger is not None:
                    intent = BacktestOrderIntent(
                        decision_id=f"protective:{open_trade.fill.decision_id}:{index}",
                        action=DecisionAction.EXIT_LONG,
                        quantity=ledger.snapshot().portfolio.btc,
                        atr=None,
                    )
                    open_trade = self._execute_intent(
                        intent, bar, ledger, open_trade, trades, reason=reason, price=trigger
                    )

            portfolio = ledger.snapshot().portfolio
            equity = portfolio.equity(bar.close)
            peak = max(peak, equity)
            curve.append(
                EquityPoint(
                    bar.close_time,
                    portfolio.cash,
                    portfolio.btc,
                    bar.close,
                    equity,
                    (peak - equity) / peak,
                )
            )
            candidate = strategy(history, portfolio.btc > 0)
            if isinstance(candidate, StrategyRequest):
                if self.pipeline is None:
                    raise ValueError("StrategyRequest callback requires a shared pipeline")
                request = candidate
                if request.has_position != (portfolio.btc > 0):
                    request = replace(request, has_position=portfolio.btc > 0)
                plan = self.pipeline.plan(
                    request=request,
                    portfolio=portfolio,
                    risk_state=risk_state,
                    reference_price=bar.close,
                    atr=None,
                    now=bar.close_time,
                )
                approval = plan.approval
                decision_intent = (
                    BacktestOrderIntent(
                        plan.decision.decision_id,
                        plan.decision.action,
                        approval.quantity,
                        approval.stop_price,
                        approval.take_profit_price,
                    )
                    if approval is not None and approval.approved
                    else None
                )
                if decision_intent is not None:
                    if decision_intent.action is DecisionAction.ENTER_LONG:
                        risk_state = self.pipeline.risk.record_entry(risk_state, bar.close_time)
                    else:
                        risk_state = self.pipeline.risk.record_exit(risk_state, bar.close_time)
            else:
                decision_intent = candidate
            if decision_intent is not None:
                pending = decision_intent

        metrics = self._metrics(
            bars, tuple(curve), tuple(trades), ledger.snapshot().fills, exposed_bars
        )
        warnings = (
            "Backtest results are vulnerable to overfitting.",
            "Validate the strategy on a separate out-of-sample period before use.",
            "Positive historical return is not approval for live trading.",
        )
        return BacktestResult(
            snapshot=snapshot,
            trades=tuple(trades),
            fills=ledger.snapshot().fills,
            equity_curve=tuple(curve),
            metrics=metrics,
            warnings=warnings,
        )

    def _execute_intent(
        self,
        intent: BacktestOrderIntent,
        bar: Candle,
        ledger: AtomicLedger,
        open_trade: _OpenTrade | None,
        trades: list[BacktestTrade],
        *,
        reason: str,
        price: Decimal | None = None,
    ) -> _OpenTrade | None:
        portfolio = ledger.snapshot().portfolio
        if intent.action is DecisionAction.ENTER_LONG:
            if open_trade is not None:
                return open_trade
            side = OrderSide.BUY
        else:
            if open_trade is None:
                return None
            side = OrderSide.SELL
        fill = self.execution_model.fill(
            side=side,
            reference_price=price or bar.open,
            requested_quantity=min(intent.quantity, portfolio.btc)
            if side is OrderSide.SELL
            else intent.quantity,
            atr=intent.atr,
            decision_id=intent.decision_id,
        )
        ledger.apply_fill(fill, occurred_at=bar.open_time)
        if side is OrderSide.BUY:
            return _OpenTrade(
                fill,
                bar.open_time,
                intent.stop_price,
                intent.take_profit_price,
                fill.fee + fill.spread_cost + fill.slippage_cost,
            )
        assert open_trade is not None
        gross = (fill.reference_price - open_trade.fill.reference_price) * fill.quantity
        net = fill.notional - fill.fee - open_trade.fill.notional - open_trade.fill.fee
        trades.append(
            BacktestTrade(
                open_trade.fill,
                open_trade.time,
                fill,
                bar.open_time,
                reason,
                gross,
                net,
            )
        )
        return None

    @staticmethod
    def _protective_trigger(bar: Candle, trade: _OpenTrade) -> tuple[str | None, Decimal | None]:
        stop_hit = trade.stop is not None and bar.low <= trade.stop
        target_hit = trade.target is not None and bar.high >= trade.target
        if stop_hit:  # conservative ordering when both occur in an OHLC bar
            assert trade.stop is not None
            return "stop_loss", min(bar.open, trade.stop)
        if target_hit:
            return "take_profit", trade.target
        return None, None

    def _snapshot(self, bars: tuple[Candle, ...]) -> BacktestRunSnapshot:
        data_hasher = sha256()
        for b in bars:
            candle_payload = {
                "source": b.source,
                "dataset_id": b.dataset_id,
                "symbol": b.symbol,
                "timeframe": b.timeframe,
                "open_time": b.open_time.isoformat(),
                "close_time": b.close_time.isoformat(),
                "open": str(b.open),
                "high": str(b.high),
                "low": str(b.low),
                "close": str(b.close),
                "volume": str(b.volume),
                "source_fingerprint": b.source_fingerprint,
            }
            data_hasher.update(
                json.dumps(candle_payload, sort_keys=True, separators=(",", ":")).encode()
            )
            data_hasher.update(b"\n")
        data_fingerprint = data_hasher.hexdigest()
        configuration_json = json.dumps(
            {
                "backtest": asdict(self.config),
                "execution_model_version": self.execution_model.version,
                "costs": self.execution_model.config.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        run_fingerprint = sha256(f"{data_fingerprint}:{configuration_json}".encode()).hexdigest()
        return BacktestRunSnapshot(
            run_id=run_fingerprint,
            run_fingerprint=run_fingerprint,
            data_fingerprint=data_fingerprint,
            engine_version=self.config.engine_version,
            execution_model_version=self.execution_model.version,
            configuration_json=configuration_json,
            candle_count=len(bars),
            first_open_time=bars[0].open_time,
            last_close_time=bars[-1].close_time,
            sharpe_annualization=(
                "close-to-close equity returns; zero risk-free rate; "
                f"sqrt({self.config.periods_per_year}) annualization"
            ),
        )

    def _metrics(
        self,
        bars: tuple[Candle, ...],
        curve: tuple[EquityPoint, ...],
        trades: tuple[BacktestTrade, ...],
        fills: tuple[SimulatedFill, ...],
        exposed_bars: int,
    ) -> BacktestMetrics:
        final = curve[-1].equity
        fees = sum((fill.fee for fill in fills), ZERO)
        spread = sum((fill.spread_cost for fill in fills), ZERO)
        slippage = sum((fill.slippage_cost for fill in fills), ZERO)
        net_pnl = final - self.config.initial_capital
        gross_pnl = net_pnl + fees + spread + slippage
        wins = tuple(trade.net_pnl for trade in trades if trade.net_pnl > 0)
        losses = tuple(trade.net_pnl for trade in trades if trade.net_pnl < 0)
        gross_profit = sum(wins, ZERO)
        gross_loss = -sum(losses, ZERO)
        realized_pnl = sum((trade.net_pnl for trade in trades), ZERO)
        average_win = gross_profit / Decimal(len(wins)) if wins else ZERO
        average_loss = sum(losses, ZERO) / Decimal(len(losses)) if losses else ZERO
        turnover = sum((fill.notional for fill in fills), ZERO) / self.config.initial_capital
        buy_hold = bars[-1].close / bars[0].open - ONE
        return BacktestMetrics(
            initial_equity=self.config.initial_capital,
            final_equity=final,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            realized_pnl=realized_pnl,
            unrealized_pnl=net_pnl - realized_pnl,
            gross_return=gross_pnl / self.config.initial_capital,
            net_return=net_pnl / self.config.initial_capital,
            total_fees=fees,
            total_spread=spread,
            total_slippage=slippage,
            maximum_drawdown=max((point.drawdown for point in curve), default=ZERO),
            sharpe_ratio=self._sharpe(curve),
            profit_factor=(gross_profit / gross_loss if gross_loss else None),
            win_rate=Decimal(len(wins)) / Decimal(len(trades)) if trades else ZERO,
            average_win=average_win,
            average_loss=average_loss,
            average_win_loss_ratio=(
                average_win / abs(average_loss) if average_win and average_loss else None
            ),
            exposure=Decimal(exposed_bars) / Decimal(len(bars)),
            turnover=turnover,
            buy_and_hold_return=buy_hold,
            trade_count=len(trades),
            positive_return=final > self.config.initial_capital,
        )

    def _sharpe(self, curve: tuple[EquityPoint, ...]) -> Decimal:
        decimal_returns = [
            curve[i].equity / curve[i - 1].equity - ONE
            for i in range(1, len(curve))
            if curve[i - 1].equity > 0
        ]
        if len(decimal_returns) < 2:
            return ZERO
        mean = sum(decimal_returns, ZERO) / Decimal(len(decimal_returns))
        variance = sum(((value - mean) ** 2 for value in decimal_returns), ZERO) / Decimal(
            len(decimal_returns) - 1
        )
        if variance == 0:
            return ZERO
        return mean / variance.sqrt() * Decimal(self.config.periods_per_year).sqrt()

    @staticmethod
    def _validate_bars(bars: tuple[Candle, ...]) -> None:
        if not bars:
            raise ValueError("at least one candle is required")
        if any(not bar.is_closed for bar in bars):
            raise ValueError("backtests accept only closed candles")
        if any(bar.timeframe != "15m" for bar in bars):
            raise ValueError("execution candles must use the 15m timeframe")
        times = [bar.open_time for bar in bars]
        if times != sorted(times) or len(set(times)) != len(times):
            raise ValueError("candles must be unique and strictly chronological")
        if any(
            bars[index].open_time != bars[index - 1].close_time for index in range(1, len(bars))
        ):
            raise ValueError("candle history contains gaps")
