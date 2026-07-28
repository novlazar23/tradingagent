"""Deterministic Decimal technical indicators without third-party TA defaults."""

from decimal import Decimal, localcontext

from tradingagent.domain import Candle
from tradingagent.features.models import IndicatorConfig, IndicatorContribution, IndicatorResult


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def _clamp(value: Decimal) -> Decimal:
    return max(Decimal("-1"), min(Decimal("1"), value))


def _ema_series(values: list[Decimal], period: int) -> list[Decimal]:
    alpha = Decimal(2) / Decimal(period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(alpha * value + (Decimal(1) - alpha) * output[-1])
    return output


def _rsi(values: list[Decimal], period: int) -> Decimal | None:
    if len(values) <= period:
        return None
    changes = [current - previous for previous, current in zip(values, values[1:], strict=False)]
    gains = [max(change, Decimal(0)) for change in changes]
    losses = [max(-change, Decimal(0)) for change in changes]
    average_gain = _mean(gains[:period])
    average_loss = _mean(losses[:period])
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
    if average_loss == 0:
        return Decimal(100) if average_gain > 0 else Decimal(50)
    return Decimal(100) - Decimal(100) / (Decimal(1) + average_gain / average_loss)


def _atr(candles: list[Candle], period: int) -> Decimal | None:
    if len(candles) <= period:
        return None
    ranges = [
        max(
            candle.high - candle.low,
            abs(candle.high - previous.close),
            abs(candle.low - previous.close),
        )
        for previous, candle in zip(candles, candles[1:], strict=False)
    ]
    value = _mean(ranges[:period])
    for true_range in ranges[period:]:
        value = (value * (period - 1) + true_range) / period
    return value


def calculate_indicators(
    candles: list[Candle] | tuple[Candle, ...], config: IndicatorConfig
) -> IndicatorResult:
    """Calculate the latest configured snapshot from closed, same-timeframe candles.

    Insufficient history returns a non-tradeable result rather than partial evidence.
    """
    if not candles:
        raise ValueError("at least one candle is required")
    ordered = sorted(candles, key=lambda candle: candle.close_time)
    timeframe = ordered[0].timeframe
    if any(not candle.is_closed or candle.timeframe != timeframe for candle in ordered):
        raise ValueError("indicators require closed candles from one timeframe")
    closes = [candle.close for candle in ordered]
    volumes = [candle.volume for candle in ordered]
    sma = _mean(closes[-config.sma_period :]) if len(closes) >= config.sma_period else None
    ema = _ema_series(closes, config.ema_period)[-1]
    rsi = _rsi(closes, config.rsi_period)
    fast = _ema_series(closes, config.macd_fast_period)
    slow = _ema_series(closes, config.macd_slow_period)
    macd_series = [
        fast_value - slow_value for fast_value, slow_value in zip(fast, slow, strict=True)
    ]
    signal_series = _ema_series(macd_series, config.macd_signal_period)
    macd_line = macd_series[-1]
    macd_signal = signal_series[-1]
    histogram = macd_line - macd_signal
    middle = upper = lower = bandwidth = position = None
    if len(closes) >= config.bollinger_period:
        window = closes[-config.bollinger_period :]
        middle = _mean(window)
        variance = _mean([(value - middle) ** 2 for value in window])
        with localcontext() as context:
            context.prec = 34
            deviation = variance.sqrt()
        upper = middle + config.bollinger_stddev * deviation
        lower = middle - config.bollinger_stddev * deviation
        assert middle is not None and upper is not None and lower is not None
        bandwidth = (upper - lower) / middle
        band_range = upper - lower
        position = (closes[-1] - lower) / band_range if band_range else Decimal("0.5")
    atr = _atr(ordered, config.atr_period)
    volume_average = (
        _mean(volumes[-config.volume_period :]) if len(volumes) >= config.volume_period else None
    )
    relative_volume = None
    if volume_average is not None and volume_average != 0:
        relative_volume = volumes[-1] / volume_average
    tradeable = len(ordered) >= config.warmup
    price = closes[-1]
    evidence = {
        "sma": IndicatorContribution(
            _clamp((price - sma) / sma) if sma else Decimal(0),
            "close relative to simple moving average",
            {"close": price, "sma": sma},
        ),
        "ema": IndicatorContribution(
            _clamp((price - ema) / ema) if ema else Decimal(0),
            "close relative to exponential moving average",
            {"close": price, "ema": ema},
        ),
        "rsi": IndicatorContribution(
            _clamp((Decimal(50) - rsi) / Decimal(50)) if rsi is not None else Decimal(0),
            "mean-reversion score around RSI 50",
            {"rsi": rsi},
        ),
        "macd": IndicatorContribution(
            _clamp(histogram / max(abs(macd_line), abs(macd_signal), Decimal("1e-18"))),
            "MACD histogram relative to MACD magnitude",
            {"line": macd_line, "signal": macd_signal, "histogram": histogram},
        ),
        "bollinger": IndicatorContribution(
            _clamp(Decimal(1) - Decimal(2) * position) if position is not None else Decimal(0),
            "mean-reversion score from normalized Bollinger position",
            {"position": position, "bandwidth": bandwidth},
        ),
        "atr": IndicatorContribution(
            Decimal(0),
            "ATR is risk evidence and has no directional bias",
            {"atr": atr, "atr_fraction": atr / price if atr is not None else None},
        ),
        "volume": IndicatorContribution(
            _clamp(relative_volume - Decimal(1)) if relative_volume is not None else Decimal(0),
            "relative volume above or below its configured average",
            {"relative_volume": relative_volume, "volume_average": volume_average},
        ),
    }
    contribution = _mean([item.score for item in evidence.values()]) if tradeable else Decimal(0)
    raw: dict[str, Decimal | None] = {
        "sma": sma,
        "ema": ema,
        "rsi": rsi,
        "macd_line": macd_line,
        "macd_signal": macd_signal,
        "macd_histogram": histogram,
        "bollinger_middle": middle,
        "bollinger_upper": upper,
        "bollinger_lower": lower,
        "bollinger_bandwidth": bandwidth,
        "bollinger_position": position,
        "atr": atr,
        "volume_average": volume_average,
        "relative_volume": relative_volume,
    }
    reason = (
        f"trend/momentum contribution {contribution}"
        if tradeable
        else f"warm-up requires {config.warmup} candles; received {len(ordered)}"
    )
    return IndicatorResult(
        timeframe=timeframe,
        available_at=ordered[-1].close_time,
        version=config.version,
        tradeable=tradeable,
        contribution=contribution,
        reason=reason,
        sma=sma,
        ema=ema,
        rsi=rsi,
        macd_line=macd_line,
        macd_signal=macd_signal,
        macd_histogram=histogram,
        bollinger_middle=middle,
        bollinger_upper=upper,
        bollinger_lower=lower,
        bollinger_bandwidth=bandwidth,
        bollinger_position=position,
        atr=atr,
        volume_average=volume_average,
        relative_volume=relative_volume,
        raw_values=raw,
        contributions=evidence,
    )
