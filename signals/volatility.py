"""
Section 3.1 — Volatility signal.

Input : current at-the-money implied volatility, and recent realized volatility.
Output: premium classified ELEVATED or DEPRESSED relative to the underlying's
        own recent behaviour.

Elevated premium favours premium-collecting structures; depressed premium favours
paying for premium rather than selling it (Section 3.3).

All maths is plain Python — no numpy dependency, so the reading is auditable
line by line.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from config import SCORING, SIGNALS

ELEVATED = "premium elevated"
DEPRESSED = "premium depressed"


@dataclass
class VolatilityReading:
    condition: str            # ELEVATED | DEPRESSED
    atm_iv: float | None      # current at-the-money implied volatility (annualized)
    realized_vol: float | None  # trailing realized volatility (annualized)
    iv_rv_ratio: float | None
    iv_average: float | None   # trailing average of prior ATM IV observations
    iv_change_ratio: float | None  # today's IV / trailing average
    sample_size: int          # prior IV observations available

    @property
    def is_elevated(self) -> bool:
        return self.condition == ELEVATED


def realized_volatility(
    closes: list[float], window: int | None = None
) -> float | None:
    """
    Annualized close-to-close realized volatility over `window` sessions.

    Returns None when there is not enough history to fill the window.
    """
    periods = window or SIGNALS.realized_vol_window
    if len(closes) < periods + 1:
        return None

    recent = closes[-(periods + 1):]
    returns: list[float] = []
    for prev, curr in zip(recent, recent[1:]):
        if prev <= 0 or curr <= 0:
            return None
        returns.append(math.log(curr / prev))

    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(SIGNALS.annualization_days)


def atm_implied_volatility(contracts, spot: float) -> float | None:
    """
    At-the-money implied volatility for the nearest eligible expiry.

    Uses the call and put closest to spot on the front eligible expiry and
    averages whichever of the two report an IV.
    """
    usable = [c for c in contracts if c.implied_volatility is not None and c.implied_volatility > 0]
    if not usable:
        return None

    front = min(c.expiry for c in usable)
    front_contracts = [c for c in usable if c.expiry == front]
    if not front_contracts:
        return None

    ivs: list[float] = []
    for right in ("C", "P"):
        side = [c for c in front_contracts if c.right == right]
        if side:
            nearest = min(side, key=lambda c: abs(c.strike - spot))
            ivs.append(float(nearest.implied_volatility))
    if not ivs:
        return None
    return sum(ivs) / len(ivs)


def evaluate(
    closes: list[float],
    contracts,
    spot: float,
    prior_atm_ivs: list[float] | None = None,
) -> VolatilityReading:
    """
    Classify premium as elevated or depressed.

    `prior_atm_ivs` are previous sessions' ATM IV readings for this underlying,
    most recent first, loaded from the iv_history table. They feed check 2
    (volatility stable) — an IV spiking into entry signals an unpriced event.
    """
    atm_iv = atm_implied_volatility(contracts, spot)
    rv = realized_volatility(closes)
    ratio = (atm_iv / rv) if (atm_iv is not None and rv) else None

    priors = list(prior_atm_ivs or [])[: SIGNALS.iv_average_window]
    iv_average = (sum(priors) / len(priors)) if priors else None
    change_ratio = (atm_iv / iv_average) if (atm_iv is not None and iv_average) else None

    condition = (
        ELEVATED
        if ratio is not None and ratio >= SCORING.premium_rich_iv_rv_ratio
        else DEPRESSED
    )

    return VolatilityReading(
        condition=condition,
        atm_iv=atm_iv,
        realized_vol=rv,
        iv_rv_ratio=ratio,
        iv_average=iv_average,
        iv_change_ratio=change_ratio,
        sample_size=len(priors),
    )
