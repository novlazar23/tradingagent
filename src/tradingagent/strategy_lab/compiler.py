"""Compile the constrained strategy DSL into deterministic Freqtrade Python."""

import re

from tradingagent.strategy_lab.models import StrategySpec, StrategyStatus

_CONDITION = re.compile(
    r"^(?P<left>[a-z][a-z0-9_]*)\s*(?P<op><=|>=|<|>)\s*(?P<right>[a-z][a-z0-9_]*|\d+(?:\.\d+)?)$"
)


def _condition(value: str) -> str:
    match = _CONDITION.fullmatch(value.strip().lower())
    if match is None:
        raise ValueError(f"unsupported strategy condition: {value!r}")
    left, operator, right = match.group("left", "op", "right")
    if right.replace(".", "", 1).isdigit():
        return f"(dataframe[{left!r}] {operator} {right})"
    return f"(dataframe[{left!r}] {operator} dataframe[{right!r}])"


def _class_name(name: str) -> str:
    return (
        "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", name) if part) + "Strategy"
    )


def _indicator_lines(spec: StrategySpec) -> str:
    names = {
        token
        for rule in (*spec.entry_rules, *spec.exit_rules)
        for token in re.findall(r"\b[a-z][a-z0-9_]*\b", rule.lower())
        if token not in {"close", "open", "high", "low", "volume"}
    }
    lines: list[str] = []
    for name in sorted(names):
        if name == "rsi":
            period = int(spec.indicators.get("rsi", {}).get("period", 14))
            lines.append(f'        dataframe["rsi"] = ta.RSI(dataframe, timeperiod={period})')
        elif (match := re.fullmatch(r"ema_(\d+)", name)) is not None:
            lines.append(
                f'        dataframe["{name}"] = ta.EMA(dataframe, timeperiod={int(match.group(1))})'
            )
        else:
            raise ValueError(f"unsupported strategy indicator: {name!r}")
    return "\n".join(lines) or "        return dataframe"


def _number(value: object, default: float) -> str:
    try:
        return format(float(str(value)), ".12g")
    except (TypeError, ValueError):
        return str(default)


def compile_freqtrade_strategy(spec: StrategySpec) -> str:
    if spec.status is not StrategyStatus.APPROVED:
        raise ValueError("strategy must be approved before compilation")
    entry = " & ".join(_condition(rule) for rule in spec.entry_rules)
    exit_ = " & ".join(_condition(rule) for rule in spec.exit_rules)
    evidence = "\n".join(
        f"# Evidence: {segment.start_seconds}-{segment.end_seconds}s {segment.text}"
        for segment in spec.evidence
    )
    take_profit = _number(spec.risk.get("take_profit"), 0.0)
    stop_loss = _number(spec.risk.get("stop_loss"), 0.02)
    return f'''"""Generated from {spec.source_url}; review artifact before use."""
from pandas import DataFrame
import talib.abstract as ta
from freqtrade.strategy import IStrategy


class {_class_name(spec.name)}(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "{spec.timeframe}"
    can_short = False
    startup_candle_count = 200
    minimal_roi = {{"0": {take_profit}}}
    stoploss = -{stop_loss}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
{_indicator_lines(spec)}
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[{entry}, "enter_long"] = 1
        dataframe.loc[{entry}, "enter_tag"] = {spec.name + ":entry"!r}
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[{exit_}, "exit_long"] = 1
        dataframe.loc[{exit_}, "exit_tag"] = {spec.name + ":exit"!r}
        return dataframe

{evidence}
'''
