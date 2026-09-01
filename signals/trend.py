"""
Section 3.2 — Trend signal.

Input : short-versus-long moving average relationship on daily closes.
Output: CLEAR UPTREND, CLEAR DOWNTREND, or RANGE-BOUND.

This is the moving-average technique from the team's prior equity screening work,
adapted to produce a directional bias rather than a binary buy/sell signal: the
separation between the two averages must exceed a threshold before a direction is
asserted at all, so a flat market reads as range-bound instead of noisily flipping.
"""
from __future__ import annotations

from dataclasses import dataclass

from config import SIGNALS

UPTREND = "clear uptrend"
DOWNTREND = "clear downtrend"
RANGE_BOUND = "range-bound"


@dataclass
class TrendReading:
    condition: str              # UPTREND | DOWNTREND | RANGE_BOUND
    ma_short: float | None
    ma_long: float | None
    separation: float | None    # (ma_short - ma_long) / price, signed
    threshold: float
    price: float | None

    @property
    def is_clear(self) -> bool:
        return self.condition in (UPTREND, DOWNTREND)

    @property
    def direction(self) -> int:
        """+1 bullish, -1 bearish, 0 no directional bias."""
        return {UPTREND: 1, DOWNTREND: -1}.get(self.condition, 0)


def moving_average(closes: list[float], periods: int) -> float | None:
    if len(closes) < periods or periods <= 0:
        return None
    window = closes[-periods:]
    return sum(window) / len(window)


def evaluate(closes: list[float], price: float | None = None) -> TrendReading:
    """Classify trend from the MA relationship, requiring a minimum separation."""
    short = moving_average(closes, SIGNALS.ma_short)
    long = moving_average(closes, SIGNALS.ma_long)
    reference = price if price else (closes[-1] if closes else None)
    threshold = SIGNALS.trend_clarity_threshold

    if short is None or long is None or not reference:
        return TrendReading(RANGE_BOUND, short, long, None, threshold, reference)

    separation = (short - long) / reference

    if abs(separation) <= threshold:
        condition = RANGE_BOUND
    elif separation > 0:
        condition = UPTREND
    else:
        condition = DOWNTREND

    return TrendReading(condition, short, long, separation, threshold, reference)
