"""Deterministic event-driven backtesting.

The Sharpe ratio uses close-to-close equity returns, a zero risk-free rate and
``periods_per_year`` from :class:`BacktestConfig` (35,040 for 15-minute crypto
bars by default). This convention deliberately reflects a continuously traded
market and is recorded in every immutable run snapshot.
"""

from tradingagent.backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    BacktestMetrics,
    BacktestOrderIntent,
    BacktestResult,
    BacktestRunSnapshot,
    BacktestTrade,
    EquityPoint,
)

__all__ = [
    "BacktestConfig",
    "BacktestEngine",
    "BacktestMetrics",
    "BacktestOrderIntent",
    "BacktestResult",
    "BacktestRunSnapshot",
    "BacktestTrade",
    "EquityPoint",
]
