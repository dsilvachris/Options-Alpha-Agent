"""
Section 3.3 — Strategy selection matrix.

Implements exactly the lookup table in the spec:

    Premium elevated + clear uptrend    -> bull put credit spread
    Premium elevated + clear downtrend  -> bear call credit spread
    Premium elevated + range-bound      -> iron condor
    Premium depressed + any             -> no new premium-selling trade; stand
                                           aside, or a small defined-risk
                                           directional debit spread only if
                                           trend conviction is high

The matrix output determines only which structure is *eligible*. It does not
authorize a trade — eligibility is passed into the Opportunity Evaluation
Framework (Section 4) and then the Risk Gate (Section 3.4).
"""
from __future__ import annotations

from dataclasses import dataclass

from config import SIGNALS
from signals.trend import DOWNTREND, RANGE_BOUND, UPTREND, TrendReading
from signals.volatility import ELEVATED, VolatilityReading

# Structure identifiers.
BULL_PUT_CREDIT_SPREAD = "bull put credit spread"
BEAR_CALL_CREDIT_SPREAD = "bear call credit spread"
IRON_CONDOR = "iron condor"
BULL_CALL_DEBIT_SPREAD = "bull call debit spread"
BEAR_PUT_DEBIT_SPREAD = "bear put debit spread"
NO_STRUCTURE = "no structure"

#: Structures that collect premium (all defined-risk).
CREDIT_STRUCTURES = frozenset(
    {BULL_PUT_CREDIT_SPREAD, BEAR_CALL_CREDIT_SPREAD, IRON_CONDOR}
)
DEBIT_STRUCTURES = frozenset({BULL_CALL_DEBIT_SPREAD, BEAR_PUT_DEBIT_SPREAD})

#: Directional bias of each structure: +1 bullish, -1 bearish, 0 neutral.
STRUCTURE_BIAS = {
    BULL_PUT_CREDIT_SPREAD: 1,
    BEAR_CALL_CREDIT_SPREAD: -1,
    IRON_CONDOR: 0,
    BULL_CALL_DEBIT_SPREAD: 1,
    BEAR_PUT_DEBIT_SPREAD: -1,
    NO_STRUCTURE: 0,
}


@dataclass
class MatrixResult:
    structure: str
    rationale: str
    eligible: bool

    @property
    def bias(self) -> int:
        return STRUCTURE_BIAS.get(self.structure, 0)

    @property
    def is_credit(self) -> bool:
        return self.structure in CREDIT_STRUCTURES


def high_conviction(trend: TrendReading) -> bool:
    """Trend conviction used by the depressed-premium branch of the matrix."""
    if trend.separation is None:
        return False
    return abs(trend.separation) > (
        SIGNALS.trend_clarity_threshold * SIGNALS.high_conviction_multiple
    )


def select(vol: VolatilityReading, trend: TrendReading) -> MatrixResult:
    """Apply the Section 3.3 matrix to a pair of signal readings."""
    if vol.condition == ELEVATED:
        if trend.condition == UPTREND:
            return MatrixResult(
                BULL_PUT_CREDIT_SPREAD,
                "premium elevated with a clear uptrend: collect premium with "
                "defined risk below the market",
                True,
            )
        if trend.condition == DOWNTREND:
            return MatrixResult(
                BEAR_CALL_CREDIT_SPREAD,
                "premium elevated with a clear downtrend: collect premium with "
                "defined risk above the market",
                True,
            )
        if trend.condition == RANGE_BOUND:
            return MatrixResult(
                IRON_CONDOR,
                "premium elevated with no clear trend: collect premium on both "
                "sides with defined risk both ways",
                True,
            )

    # Premium depressed — no new premium-selling trade.
    if high_conviction(trend):
        if trend.condition == UPTREND:
            return MatrixResult(
                BULL_CALL_DEBIT_SPREAD,
                "premium depressed but trend conviction is high: a small "
                "defined-risk bullish debit spread is eligible",
                True,
            )
        if trend.condition == DOWNTREND:
            return MatrixResult(
                BEAR_PUT_DEBIT_SPREAD,
                "premium depressed but trend conviction is high: a small "
                "defined-risk bearish debit spread is eligible",
                True,
            )

    return MatrixResult(
        NO_STRUCTURE,
        "premium depressed without high trend conviction: stand aside",
        False,
    )
