"""
Selects the specific strikes and expiry for the structure the matrix made eligible.

Every structure built here is defined-risk by construction: each short leg is
paired with a long leg further out of the money in the same expiry and right, so
there is no code path that can produce a naked position. `build()` refuses to
return a structure whose legs are not fully paired.

Strike selection
----------------
Short strike: the strike whose |delta| is closest to, but not above, the
configured ceiling (config.SCORING.max_short_delta). That maximises collected
credit while still satisfying check 6.
Long strike : the strike closest to `config.UNIVERSE.spread_width_points` further
out of the money. Width drives both max loss and the check 5 / check 7 tradeoff
described in the config comment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from config import SCORING, UNIVERSE, today_et
from decision.matrix import (
    BEAR_CALL_CREDIT_SPREAD,
    BEAR_PUT_DEBIT_SPREAD,
    BULL_CALL_DEBIT_SPREAD,
    BULL_PUT_CREDIT_SPREAD,
    CREDIT_STRUCTURES,
    IRON_CONDOR,
    MatrixResult,
)
from perception.market import OptionContract

CONTRACT_MULTIPLIER = 100.0


@dataclass
class Leg:
    contract: OptionContract
    side: str            # 'sell' (short) or 'buy' (long)
    position_intent: str  # 'sell_to_open' | 'buy_to_open'
    ratio_qty: int = 1

    @property
    def symbol(self) -> str:
        return self.contract.symbol

    @property
    def is_short(self) -> bool:
        return self.side == "sell"

    def to_mcp_leg(self, closing: bool = False) -> dict:
        """Leg payload for the place_option_order MCP tool."""
        if closing:
            side = "buy" if self.side == "sell" else "sell"
            intent = "buy_to_close" if self.side == "sell" else "sell_to_close"
        else:
            side, intent = self.side, self.position_intent
        return {
            "symbol": self.symbol,
            "ratio_qty": str(self.ratio_qty),
            "side": side,
            "position_intent": intent,
        }


@dataclass
class ProposedStructure:
    """A concrete, priced, defined-risk structure — one contract's worth."""

    symbol: str
    structure: str
    expiry: date
    dte: int
    legs: list[Leg] = field(default_factory=list)
    credit: float = 0.0        # net credit per contract at the MID (entry price)
    credit_at_cross: float = 0.0  # net credit if the spread were crossed (floor)
    width: float = 0.0         # widest spread width in points
    max_loss: float = 0.0      # per contract, in dollars
    max_profit: float = 0.0    # per contract, in dollars
    breakevens: list[float] = field(default_factory=list)
    total_spread: float = 0.0  # summed leg bid-ask, in points
    short_delta: float = 0.0   # max |delta| across short legs
    min_open_interest: int | None = None
    is_credit: bool = True
    note: str = ""

    @property
    def short_legs(self) -> list[Leg]:
        return [l for l in self.legs if l.is_short]

    @property
    def long_legs(self) -> list[Leg]:
        return [l for l in self.legs if not l.is_short]

    @property
    def credit_to_width(self) -> float:
        return (self.credit / (self.width * CONTRACT_MULTIPLIER)) if self.width else 0.0

    @property
    def spread_pct_of_credit(self) -> float:
        if self.credit <= 0:
            return float("inf")
        return (self.total_spread * CONTRACT_MULTIPLIER) / self.credit

    @property
    def breakeven(self) -> float:
        return self.breakevens[0] if self.breakevens else 0.0

    def describe(self) -> str:
        parts = []
        for leg in self.legs:
            action = "SELL" if leg.is_short else "BUY"
            parts.append(f"{action} {leg.contract.strike:g}{leg.contract.right}")
        return f"{self.structure} {self.expiry:%Y-%m-%d} [" + " / ".join(parts) + "]"


class StructureError(Exception):
    """Raised when no compliant structure can be built from the chain."""


def _eligible_expiries(contracts: list[OptionContract], today: date) -> list[date]:
    """Expiries inside the DTE window and strictly before the configured cutoff."""
    out = set()
    for contract in contracts:
        dte = (contract.expiry - today).days
        if (UNIVERSE.min_dte <= dte <= UNIVERSE.max_dte
                and contract.expiry <= UNIVERSE.expiry_cutoff):
            out.add(contract.expiry)
    return sorted(out)


def _quotable(contract: OptionContract) -> bool:
    return contract.mid is not None and contract.mid > 0


def _pick_short(side: list[OptionContract], ceiling: float) -> OptionContract | None:
    """Highest-|delta| contract at or below the delta ceiling."""
    scored = [c for c in side if c.delta is not None and _quotable(c)]
    within = [c for c in scored if abs(c.delta) <= ceiling]
    if within:
        return max(within, key=lambda c: abs(c.delta))
    # Nothing satisfies the ceiling. Return the closest miss so the scorer can
    # record a real measured value and fail check 6 explicitly.
    return min(scored, key=lambda c: abs(c.delta)) if scored else None


def _pick_long(
    side: list[OptionContract], short: OptionContract, direction: int
) -> OptionContract | None:
    """
    The long leg, approximately `spread_width_points` further out of the money.

    Picks the available strike closest to the configured target width. Falls
    back to the widest available strike when the chain does not reach the
    target.

    direction=-1 -> further below the short strike (puts)
    direction=+1 -> further above the short strike (calls)
    """
    candidates = [
        c for c in side
        if _quotable(c) and (
            (direction < 0 and c.strike < short.strike)
            or (direction > 0 and c.strike > short.strike)
        )
    ]
    if not candidates:
        return None
    target = UNIVERSE.spread_width_points
    return min(candidates, key=lambda c: abs(abs(c.strike - short.strike) - target))


def _vertical(
    contracts: list[OptionContract], expiry: date, right: str, direction: int
) -> tuple[Leg, Leg]:
    """Build a credit vertical: short nearer the money, long further out."""
    side = [c for c in contracts if c.expiry == expiry and c.right == right]
    short = _pick_short(side, SCORING.max_short_delta)
    if short is None:
        raise StructureError(f"no quotable {right} contracts with greeks for {expiry}")
    long = _pick_long(side, short, direction)
    if long is None:
        raise StructureError(
            f"no long {right} leg available beyond strike {short.strike:g} for {expiry}"
        )
    return (
        Leg(short, "sell", "sell_to_open"),
        Leg(long, "buy", "buy_to_open"),
    )


def _debit_vertical(
    contracts: list[OptionContract], expiry: date, right: str, spot: float, direction: int
) -> tuple[Leg, Leg]:
    """
    Build a debit vertical: long near the money, short further out.

    direction=+1 bull call (long lower strike, short higher)
    direction=-1 bear put  (long higher strike, short lower)
    """
    side = [c for c in contracts if c.expiry == expiry and c.right == right and _quotable(c)]
    if not side:
        raise StructureError(f"no quotable {right} contracts for {expiry}")
    long = min(side, key=lambda c: abs(c.strike - spot))
    short = _pick_long(side, long, direction)
    if short is None:
        raise StructureError(f"no short {right} leg beyond strike {long.strike:g}")
    return (
        Leg(long, "buy", "buy_to_open"),
        Leg(short, "sell", "sell_to_open"),
    )


def _price(legs: list[Leg]) -> tuple[float, float, float]:
    """
    Price the structure.

    Returns (net_at_mid, summed_bid_ask, net_at_cross), all per contract; the
    two net figures are in dollars with positive meaning a credit.

    net_at_mid   every leg at its bid-ask midpoint. This is the entry price.
    net_at_cross every leg at the price that would cross the spread — shorts
                 sold at the bid, longs bought at the ask. This is the worst
                 price the re-quote ladder will ever walk to, never exceeded.
    """
    net_mid = 0.0
    net_cross = 0.0
    spread = 0.0
    for leg in legs:
        mid = leg.contract.mid
        if mid is None:
            raise StructureError(f"missing quote for {leg.symbol}")
        net_mid += mid if leg.is_short else -mid
        if leg.is_short:
            net_cross += leg.contract.bid if leg.contract.bid is not None else mid
        else:
            net_cross -= leg.contract.ask if leg.contract.ask is not None else mid
        spread += leg.contract.spread or 0.0
    return net_mid * CONTRACT_MULTIPLIER, spread, net_cross * CONTRACT_MULTIPLIER


def _min_oi(legs: list[Leg]) -> int | None:
    values = [l.contract.open_interest for l in legs if l.contract.open_interest is not None]
    return min(values) if values else None


def _max_short_delta(legs: list[Leg]) -> float:
    deltas = [abs(l.contract.delta) for l in legs if l.is_short and l.contract.delta is not None]
    return max(deltas) if deltas else 0.0


def _assert_defined_risk(legs: list[Leg], structure: str) -> None:
    """
    Hard invariant: every short leg must be covered by a long leg in the same
    expiry and right, further out of the money. No naked positions, ever.
    """
    for short in [l for l in legs if l.is_short]:
        covers = [
            l for l in legs
            if not l.is_short
            and l.contract.right == short.contract.right
            and l.contract.expiry == short.contract.expiry
            and (
                (short.contract.right == "P" and l.contract.strike < short.contract.strike)
                or (short.contract.right == "C" and l.contract.strike > short.contract.strike)
            )
        ]
        if not covers:
            raise StructureError(
                f"{structure}: short {short.symbol} has no covering long leg — "
                "refusing to build an undefined-risk position"
            )


def build(
    matrix: MatrixResult,
    contracts: list[OptionContract],
    spot: float,
    today: date | None = None,
) -> ProposedStructure:
    """Build the concrete structure the matrix made eligible, or raise."""
    today = today or today_et()
    expiries = _eligible_expiries(contracts, today)
    if not expiries:
        raise StructureError(
            f"no expiry inside {UNIVERSE.min_dte}-{UNIVERSE.max_dte} DTE and on or before "
            f"{UNIVERSE.expiry_cutoff.isoformat()} (inclusive)"
        )
    expiry = expiries[0]
    dte = (expiry - today).days
    name = matrix.structure

    if name == BULL_PUT_CREDIT_SPREAD:
        legs = list(_vertical(contracts, expiry, "P", direction=-1))
        credit, spread, credit_cross = _price(legs)
        width = abs(legs[0].contract.strike - legs[1].contract.strike)
        breakevens = [legs[0].contract.strike - credit / CONTRACT_MULTIPLIER]
    elif name == BEAR_CALL_CREDIT_SPREAD:
        legs = list(_vertical(contracts, expiry, "C", direction=+1))
        credit, spread, credit_cross = _price(legs)
        width = abs(legs[1].contract.strike - legs[0].contract.strike)
        breakevens = [legs[0].contract.strike + credit / CONTRACT_MULTIPLIER]
    elif name == IRON_CONDOR:
        put_legs = list(_vertical(contracts, expiry, "P", direction=-1))
        call_legs = list(_vertical(contracts, expiry, "C", direction=+1))
        legs = put_legs + call_legs
        credit, spread, credit_cross = _price(legs)
        put_width = abs(put_legs[0].contract.strike - put_legs[1].contract.strike)
        call_width = abs(call_legs[1].contract.strike - call_legs[0].contract.strike)
        # Only one side can be breached at expiry, so risk is the wider wing.
        width = max(put_width, call_width)
        breakevens = [
            put_legs[0].contract.strike - credit / CONTRACT_MULTIPLIER,
            call_legs[0].contract.strike + credit / CONTRACT_MULTIPLIER,
        ]
    elif name in (BULL_CALL_DEBIT_SPREAD, BEAR_PUT_DEBIT_SPREAD):
        right = "C" if name == BULL_CALL_DEBIT_SPREAD else "P"
        direction = +1 if name == BULL_CALL_DEBIT_SPREAD else -1
        legs = list(_debit_vertical(contracts, expiry, right, spot, direction))
        credit, spread, credit_cross = _price(legs)  # negative: a debit
        width = abs(legs[0].contract.strike - legs[1].contract.strike)
        debit = -credit / CONTRACT_MULTIPLIER
        breakevens = [
            legs[0].contract.strike + debit
            if right == "C"
            else legs[0].contract.strike - debit
        ]
    else:
        raise StructureError(f"no structure to build for {name!r}")

    _assert_defined_risk(legs, name)

    if name in CREDIT_STRUCTURES:
        if credit <= 0:
            raise StructureError(
                f"{name}: computed net credit {credit:.2f} is not positive; "
                "chain quotes do not support a credit structure"
            )
        max_loss = width * CONTRACT_MULTIPLIER - credit
        max_profit = credit
    else:
        debit_cost = -credit
        if debit_cost <= 0:
            raise StructureError(f"{name}: computed net debit {debit_cost:.2f} is not positive")
        max_loss = debit_cost
        max_profit = width * CONTRACT_MULTIPLIER - debit_cost

    # Round money to cents: mid prices are averages and accumulate float noise
    # that would otherwise surface in decision cards and the dashboard.
    credit = round(credit, 2)
    credit_cross = round(credit_cross, 2)
    max_loss = round(max_loss, 2)
    max_profit = round(max_profit, 2)
    breakevens = [round(b, 2) for b in breakevens]

    return ProposedStructure(
        symbol=legs[0].contract.underlying,
        structure=name,
        expiry=expiry,
        dte=dte,
        legs=legs,
        credit=credit,
        credit_at_cross=credit_cross,
        width=width,
        max_loss=max_loss,
        max_profit=max_profit,
        breakevens=breakevens,
        total_spread=spread,
        short_delta=_max_short_delta(legs),
        min_open_interest=_min_oi(legs),
        is_credit=name in CREDIT_STRUCTURES,
    )
