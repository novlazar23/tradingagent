"""Confirmed-pivot chart patterns and closed-candle formations."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tradingagent.domain import Candle
from tradingagent.features.models import PatternConfig, PatternDetection, PatternDirection
from tradingagent.features.timeframes import align_closed_candles


@dataclass(frozen=True, slots=True)
class _Pivot:
    index: int
    price: Decimal
    kind: str


def _similar(left: Decimal, right: Decimal, tolerance: Decimal) -> bool:
    return abs(left - right) / max(abs(left), abs(right)) <= tolerance


def _confidence(error: Decimal, tolerance: Decimal) -> Decimal:
    if tolerance == 0:
        return Decimal(1) if error == 0 else Decimal(0)
    return max(Decimal(0), min(Decimal(1), Decimal(1) - error / tolerance))


class PatternEngine:
    """Detect explainable patterns from information available at a decision time."""

    def __init__(self, config: PatternConfig) -> None:
        self.config = config

    def detect(
        self, candles: list[Candle] | tuple[Candle, ...], decision_time: datetime
    ) -> tuple[PatternDetection, ...]:
        """Return detections whose confirmation candles closed by ``decision_time``."""
        visible = list(align_closed_candles(candles, decision_time))
        if not visible:
            return ()
        if any(candle.timeframe != visible[0].timeframe for candle in visible):
            raise ValueError("pattern detection requires one timeframe")
        detections = self._candlesticks(visible)
        detections.extend(self._chart_patterns(visible))
        unique: dict[tuple[str, datetime], PatternDetection] = {}
        for detection in detections:
            unique[(detection.name, detection.available_at)] = detection
        return tuple(sorted(unique.values(), key=lambda item: (item.available_at, item.name)))

    def _make(
        self,
        candles: list[Candle],
        name: str,
        direction: PatternDirection,
        start: int,
        end: int,
        confidence: Decimal,
        levels: dict[str, Decimal],
        invalidation: Decimal | None,
        evidence: tuple[str, ...],
    ) -> PatternDetection:
        return PatternDetection(
            name=name,
            direction=direction,
            confidence=max(Decimal(0), min(Decimal(1), confidence)),
            start_time=candles[start].open_time,
            end_time=candles[end].close_time,
            available_at=candles[end].close_time,
            timeframe=candles[end].timeframe,
            confirmed=True,
            price_levels=levels,
            invalidation_level=invalidation,
            evidence=evidence,
            version=self.config.version,
        )

    def _candlesticks(self, candles: list[Candle]) -> list[PatternDetection]:
        found: list[PatternDetection] = []
        for index, current in enumerate(candles):
            span = current.high - current.low
            body = abs(current.close - current.open)
            upper = current.high - max(current.open, current.close)
            lower = min(current.open, current.close) - current.low
            if span and body / span <= self.config.doji_body_ratio:
                found.append(
                    self._make(
                        candles,
                        "doji",
                        "neutral",
                        index,
                        index,
                        Decimal("0.8"),
                        {"high": current.high, "low": current.low},
                        current.low,
                        ("body is small relative to full candle range",),
                    )
                )
            body_floor = max(body, span * Decimal("0.01"))
            if lower >= self.config.wick_body_ratio * body_floor and upper <= body_floor:
                name = "hammer" if current.close >= current.open else "hanging_man"
                direction: PatternDirection = "bullish" if name == "hammer" else "bearish"
                found.append(
                    self._make(
                        candles,
                        name,
                        direction,
                        index,
                        index,
                        Decimal("0.75"),
                        {"wick_low": current.low},
                        current.low,
                        ("long lower wick and compact upper wick",),
                    )
                )
            if upper >= self.config.wick_body_ratio * body_floor and lower <= body_floor:
                name = "inverted_hammer" if current.close >= current.open else "shooting_star"
                direction = "bullish" if name == "inverted_hammer" else "bearish"
                found.append(
                    self._make(
                        candles,
                        name,
                        direction,
                        index,
                        index,
                        Decimal("0.75"),
                        {"wick_high": current.high},
                        current.high,
                        ("long upper wick and compact lower wick",),
                    )
                )
            if index:
                previous = candles[index - 1]
                if (
                    previous.close < previous.open
                    and current.close > current.open
                    and current.open <= previous.close
                    and current.close >= previous.open
                ):
                    found.append(
                        self._make(
                            candles,
                            "bullish_engulfing",
                            "bullish",
                            index - 1,
                            index,
                            Decimal("0.9"),
                            {},
                            current.low,
                            ("bullish body contains preceding bearish body",),
                        )
                    )
                if (
                    previous.close > previous.open
                    and current.close < current.open
                    and current.open >= previous.close
                    and current.close <= previous.open
                ):
                    found.append(
                        self._make(
                            candles,
                            "bearish_engulfing",
                            "bearish",
                            index - 1,
                            index,
                            Decimal("0.9"),
                            {},
                            current.high,
                            ("bearish body contains preceding bullish body",),
                        )
                    )
            if index >= 2:
                first, middle = candles[index - 2], candles[index - 1]
                midpoint = (first.open + first.close) / 2
                if (
                    first.close < first.open
                    and abs(middle.close - middle.open) <= abs(first.close - first.open) / 2
                    and current.close > current.open
                    and current.close >= midpoint
                ):
                    found.append(
                        self._make(
                            candles,
                            "morning_star",
                            "bullish",
                            index - 2,
                            index,
                            Decimal("0.85"),
                            {"first_midpoint": midpoint},
                            min(item.low for item in (first, middle, current)),
                            ("bearish candle, small star, bullish recovery",),
                        )
                    )
                if (
                    first.close > first.open
                    and abs(middle.close - middle.open) <= abs(first.close - first.open) / 2
                    and current.close < current.open
                    and current.close <= midpoint
                ):
                    found.append(
                        self._make(
                            candles,
                            "evening_star",
                            "bearish",
                            index - 2,
                            index,
                            Decimal("0.85"),
                            {"first_midpoint": midpoint},
                            max(item.high for item in (first, middle, current)),
                            ("bullish candle, small star, bearish reversal",),
                        )
                    )
        return found

    def _pivots(self, candles: list[Candle]) -> tuple[list[_Pivot], list[_Pivot]]:
        window = self.config.pivot_window
        highs: list[_Pivot] = []
        lows: list[_Pivot] = []
        for index in range(window, len(candles) - window):
            neighborhood = candles[index - window : index + window + 1]
            if candles[index].high == max(item.high for item in neighborhood):
                highs.append(_Pivot(index, candles[index].high, "high"))
            if candles[index].low == min(item.low for item in neighborhood):
                lows.append(_Pivot(index, candles[index].low, "low"))
        return highs, lows

    def _chart_patterns(self, candles: list[Candle]) -> list[PatternDetection]:
        highs, lows = self._pivots(candles)
        found: list[PatternDetection] = []
        last = len(candles) - 1
        pattern_sets: tuple[tuple[list[_Pivot], str, PatternDirection], ...] = (
            (lows, "double_bottom", "bullish"),
            (highs, "double_top", "bearish"),
        )
        for pivots, name, direction in pattern_sets:
            for left, right in zip(pivots, pivots[1:], strict=False):
                if right.index - left.index < self.config.minimum_separation:
                    continue
                error = abs(left.price - right.price) / max(left.price, right.price)
                between = candles[left.index + 1 : right.index]
                neckline = (
                    max(item.high for item in between)
                    if direction == "bullish"
                    else min(item.low for item in between)
                )
                breakout = (
                    candles[last].close > neckline * (1 + self.config.breakout_tolerance)
                    if direction == "bullish"
                    else candles[last].close < neckline * (1 - self.config.breakout_tolerance)
                )
                levels_match = _similar(
                    left.price, right.price, self.config.similar_level_tolerance
                )
                if levels_match and breakout:
                    found.append(
                        self._make(
                            candles,
                            name,
                            direction,
                            left.index,
                            last,
                            _confidence(error, self.config.similar_level_tolerance),
                            {"left": left.price, "right": right.price, "neckline": neckline},
                            min(left.price, right.price)
                            if direction == "bullish"
                            else max(left.price, right.price),
                            (
                                "two confirmed similar pivots",
                                "last close confirms neckline breakout",
                            ),
                        )
                    )
        found.extend(self._head_shoulders(candles, highs, lows))
        found.extend(self._triangles(candles, highs, lows))
        found.extend(self._zones(candles, highs, lows))
        return found

    def _head_shoulders(
        self, candles: list[Candle], highs: list[_Pivot], lows: list[_Pivot]
    ) -> list[PatternDetection]:
        found: list[PatternDetection] = []
        last = len(candles) - 1
        pattern_sets: tuple[tuple[list[_Pivot], str, PatternDirection], ...] = (
            (highs, "head_and_shoulders", "bearish"),
            (lows, "inverse_head_and_shoulders", "bullish"),
        )
        for pivots, name, direction in pattern_sets:
            for left, head, right in zip(pivots, pivots[1:], pivots[2:], strict=False):
                shoulders_match = _similar(
                    left.price, right.price, self.config.similar_level_tolerance
                )
                head_extreme = (
                    head.price > max(left.price, right.price)
                    if direction == "bearish"
                    else head.price < min(left.price, right.price)
                )
                segment = candles[left.index : right.index + 1]
                neckline = (
                    min(item.low for item in segment)
                    if direction == "bearish"
                    else max(item.high for item in segment)
                )
                broken = (
                    candles[last].close < neckline
                    if direction == "bearish"
                    else candles[last].close > neckline
                )
                if shoulders_match and head_extreme and broken:
                    found.append(
                        self._make(
                            candles,
                            name,
                            direction,
                            left.index,
                            last,
                            Decimal("0.85"),
                            {
                                "left_shoulder": left.price,
                                "head": head.price,
                                "right_shoulder": right.price,
                                "neckline": neckline,
                            },
                            head.price,
                            (
                                "three confirmed pivots form shoulders and head",
                                "neckline is broken by a closed candle",
                            ),
                        )
                    )
        return found

    def _triangles(
        self, candles: list[Candle], highs: list[_Pivot], lows: list[_Pivot]
    ) -> list[PatternDetection]:
        if len(highs) < 2 or len(lows) < 2:
            return []
        high_left, high_right = highs[-2:]
        low_left, low_right = lows[-2:]
        start = min(high_left.index, low_left.index)
        last = len(candles) - 1
        flat_high = _similar(high_left.price, high_right.price, self.config.similar_level_tolerance)
        flat_low = _similar(low_left.price, low_right.price, self.config.similar_level_tolerance)
        descending_high = high_right.price < high_left.price
        ascending_low = low_right.price > low_left.price
        direction: PatternDirection
        if descending_high and ascending_low:
            name, direction = "symmetrical_triangle", "neutral"
        elif flat_high and ascending_low:
            name, direction = "ascending_triangle", "bullish"
        elif descending_high and flat_low:
            name, direction = "descending_triangle", "bearish"
        else:
            return []
        upper, lower = high_right.price, low_right.price
        confirmed = candles[last].close > upper or candles[last].close < lower
        if not confirmed:
            return []
        actual_direction: PatternDirection = "bullish" if candles[last].close > upper else "bearish"
        return [
            self._make(
                candles,
                name,
                actual_direction if direction == "neutral" else direction,
                start,
                last,
                Decimal("0.75"),
                {"upper": upper, "lower": lower},
                lower if actual_direction == "bullish" else upper,
                ("confirmed pivot trendlines converge", "closed candle confirms breakout"),
            )
        ]

    def _zones(
        self, candles: list[Candle], highs: list[_Pivot], lows: list[_Pivot]
    ) -> list[PatternDetection]:
        found: list[PatternDetection] = []
        zone_sets: tuple[tuple[list[_Pivot], str, PatternDirection], ...] = (
            (lows, "horizontal_support", "bullish"),
            (highs, "horizontal_resistance", "bearish"),
        )
        for pivots, name, direction in zone_sets:
            if len(pivots) >= 2 and _similar(
                pivots[-2].price, pivots[-1].price, self.config.similar_level_tolerance
            ):
                level = (pivots[-2].price + pivots[-1].price) / 2
                found.append(
                    self._make(
                        candles,
                        name,
                        direction,
                        pivots[-2].index,
                        pivots[-1].index + self.config.pivot_window,
                        Decimal("0.7"),
                        {"level": level},
                        level,
                        ("two confirmed pivots occupy the same horizontal zone",),
                    )
                )
        return found
