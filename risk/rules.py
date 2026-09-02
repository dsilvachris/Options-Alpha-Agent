"""
Section 3.4 — Risk management rules, applied as the independent Risk Gate.

This gate runs after the opportunity scorer and before any order reaches Alpaca.
It is not advisory and it cannot be bypassed: a structure with a perfect score of
100 is still refused if it breaches a limit here. `evaluate()` is the only
sanctioned path to an order, and execution/ refuses to place anything whose
approval did not come from this module.

Constraints implemented
-----------------------
Position sizing   Maximum possible loss on a single trade capped at a fixed
                  percentage of paper account equity.
Portfolio limits  A maximum number of concurrent open positions.
Event avoidance   No new position on an underlying with a scheduled event
                  before expiry.
Exit discipline   Every approved position carries a profit target, a stop-loss
                  as a multiple of premium collected, and a hard time-based exit.
Circuit breaker   Daily halt after N order attempts or a drawdown threshold.
"""
from __future__ import annotations

import bootstrap  # noqa: F401  - must precede `logging.*` submodule imports

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from config import (EXPIRY_DAY_CLOSE_TIME, HARD_CLOSE_AT, RISK, UNIVERSE,
                    past_hard_close, today_et)
from decision.structure import ProposedStructure
from logging.events import EventLog, Stage
from logging.store import Store

APPROVED = "APPROVED"
REFUSED = "REFUSED"
#: Refusal reason when the US market is closed.
MARKET_CLOSED = "MARKET_CLOSED"


@dataclass
class ExitPlan:
    """Pre-defined exits attached to every approved position (Section 3.4)."""

    profit_target_credit: float   # buy back at or below this cost to close
    stop_loss_credit: float       # exit when cost to close reaches this
    time_exit_dte: int
    credit_collected: float

    def to_dict(self) -> dict:
        return {
            "profit_target_cost_to_close": round(self.profit_target_credit, 2),
            "stop_loss_cost_to_close": round(self.stop_loss_credit, 2),
            # Kept so exit plans stored before the rename still parse. It is
            # the configured DTE floor, not a rule that can fire at -1.
            "time_exit_dte": self.time_exit_dte,
            "expiry_day_close_et": f"{EXPIRY_DAY_CLOSE_TIME:%H:%M} ET",
            "credit_collected": round(self.credit_collected, 2),
        }

    def describe(self) -> str:
        """
        The plan as a judge reads it on a decision card.

        The third clause names the rule that actually flattens the position: the
        expiry-day close. The DTE rule it used to name cannot fire at
        time_exit_dte = -1, so printing "-1 DTE" described nothing.
        """
        return (
            f"take profit at {RISK.profit_target_pct_of_credit:.0%} of credit "
            f"(close <= ${self.profit_target_credit:.2f}), "
            f"stop at {RISK.stop_loss_multiple_of_credit:g}x credit "
            f"(close >= ${self.stop_loss_credit:.2f}), "
            f"flatten at {EXPIRY_DAY_CLOSE_TIME:%H:%M} ET on the expiry date"
        )


@dataclass
class RiskDecision:
    approved: bool
    contracts: int
    max_loss_total: float
    max_loss_cap: float
    reasons: list[str] = field(default_factory=list)
    exit_plan: ExitPlan | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return APPROVED if self.approved else REFUSED

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons) if self.reasons else ""


def build_exit_plan(structure: ProposedStructure, contracts: int) -> ExitPlan:
    """Profit target, stop-loss and time exit for a credit structure."""
    credit = structure.credit * contracts
    return ExitPlan(
        profit_target_credit=credit * (1 - RISK.profit_target_pct_of_credit),
        stop_loss_credit=credit * RISK.stop_loss_multiple_of_credit,
        time_exit_dte=RISK.time_exit_dte,
        credit_collected=credit,
    )


def circuit_breaker_state(store: Store, equity: float | None) -> tuple[bool, str]:
    """
    Daily circuit breaker.

    Halts all new orders after the configured number of order attempts, or a
    drawdown against the day's starting equity. Returns (halted, reason).
    """
    state = store.day_state()
    if state.get("halted"):
        return True, str(state.get("halt_reason") or "day halted")

    attempts = int(state.get("order_attempts") or 0)
    if attempts >= RISK.max_order_attempts_per_day:
        reason = (
            f"daily order attempt limit reached "
            f"({attempts}/{RISK.max_order_attempts_per_day})"
        )
        return True, reason

    starting = state.get("starting_equity")
    if starting and equity is not None and float(starting) > 0:
        drawdown = (float(starting) - equity) / float(starting)
        if drawdown >= RISK.max_daily_drawdown_pct:
            reason = (
                f"daily drawdown {drawdown:.2%} reached the "
                f"{RISK.max_daily_drawdown_pct:.2%} limit "
                f"(start ${float(starting):,.2f} -> now ${equity:,.2f})"
            )
            return True, reason

    return False, ""


def trip_circuit_breaker(store: Store, events: EventLog, reason: str) -> None:
    """Record the halt as an event and persist it for the rest of the day."""
    state = store.day_state()
    if not state.get("halted"):
        store.halt_day(reason)
        events.emit(
            Stage.CIRCUIT_BREAKER,
            f"CIRCUIT BREAKER TRIPPED — no further orders today: {reason}",
            payload={"reason": reason},
        )


def evaluate(
    *,
    structure: ProposedStructure,
    equity: float,
    open_positions: int,
    same_direction_positions: int,
    proposed_bias: int,
    corporate_actions: list[dict],
    store: Store,
    events: EventLog,
    today: date | None = None,
    market_open: bool = True,
) -> RiskDecision:
    """
    The Risk Gate. Independently validates a proposed trade before order placement.

    Runs every constraint, collecting all reasons rather than stopping at the
    first, so the decision card records the complete picture.
    """
    today = today or today_et()
    reasons: list[str] = []

    # -- market session ----------------------------------------------------
    # No new position is opened out of hours. Out-of-session quotes are wide and
    # stale, and Alpaca would queue the order to fill at the next open at a price
    # nobody evaluated.
    if not market_open:
        reasons.append(
            f"{MARKET_CLOSED}: the US market is closed; no new position is "
            "opened out of session"
        )

    # -- end-of-window deadline -------------------------------------------
    if past_hard_close():
        reasons.append(
            f"end-of-window deadline: no new positions at or after "
            f"{HARD_CLOSE_AT:%Y-%m-%d %H:%M %Z}"
        )

    # -- circuit breaker ---------------------------------------------------
    halted, halt_reason = circuit_breaker_state(store, equity)
    if halted:
        trip_circuit_breaker(store, events, halt_reason)
        reasons.append(f"circuit breaker active: {halt_reason}")

    # -- defined risk invariant -------------------------------------------
    if not structure.short_legs or len(structure.long_legs) < len(structure.short_legs):
        reasons.append(
            "structure is not fully defined-risk: every short leg must be paired "
            "with a long leg"
        )
    if structure.max_loss <= 0:
        reasons.append(f"structure has non-positive max loss ${structure.max_loss:.2f}")

    # -- position sizing ---------------------------------------------------
    max_loss_cap = equity * RISK.max_loss_pct_of_equity
    contracts = 0
    if structure.max_loss > 0:
        contracts = int(max_loss_cap // structure.max_loss)
    if contracts < 1:
        reasons.append(
            f"position sizing: one contract risks ${structure.max_loss:.2f}, above the "
            f"per-trade cap ${max_loss_cap:.2f} "
            f"({RISK.max_loss_pct_of_equity:.1%} of ${equity:,.2f} equity)"
        )
        contracts = 0
    max_loss_total = round(structure.max_loss * contracts, 2)
    if contracts >= 1 and max_loss_total > max_loss_cap:
        reasons.append(
            f"position sizing: total max loss ${max_loss_total:.2f} exceeds cap "
            f"${max_loss_cap:.2f}"
        )

    # -- portfolio limits --------------------------------------------------
    if open_positions + 1 > RISK.max_concurrent_positions:
        reasons.append(
            f"portfolio limit: {open_positions} open positions, maximum is "
            f"{RISK.max_concurrent_positions}"
        )
    if proposed_bias != 0 and same_direction_positions + 1 > RISK.max_same_direction_positions:
        reasons.append(
            f"directional exposure: {same_direction_positions} positions already "
            f"biased {proposed_bias:+d}, maximum is {RISK.max_same_direction_positions}"
        )

    # -- event avoidance ---------------------------------------------------
    if corporate_actions:
        kinds = ", ".join(
            str(a.get("ca_type") or a.get("type") or "event") for a in corporate_actions[:4]
        )
        reasons.append(
            f"event avoidance: {len(corporate_actions)} scheduled corporate action(s) "
            f"on {structure.symbol} before expiry ({kinds})"
        )
    if structure.symbol.upper() not in {s.upper() for s in RISK.etf_symbols}:
        reasons.append(
            f"event avoidance: {structure.symbol} is not a configured ETF and no "
            "earnings calendar is available through the Alpaca MCP server, so "
            "'no earnings before expiry' cannot be verified"
        )

    # -- expiry sanity -----------------------------------------------------
    # Pinned to the DTE floor of the tradable window, not to RISK.time_exit_dte.
    # The exit constant is -1, which can no longer refuse anything, so this gate
    # would have been dead weight tracking it. Chain selection already filters on
    # UNIVERSE.min_dte; this is the second layer, checking the structure that was
    # actually built rather than the contracts it was built from.
    if structure.dte < UNIVERSE.min_dte:
        reasons.append(
            f"expiry: {structure.dte} DTE is inside the {UNIVERSE.min_dte} DTE "
            f"floor of the tradable expiry window"
        )

    approved = not reasons
    exit_plan = build_exit_plan(structure, contracts) if approved else None

    return RiskDecision(
        approved=approved,
        contracts=contracts if approved else 0,
        max_loss_total=max_loss_total if approved else 0.0,
        max_loss_cap=max_loss_cap,
        reasons=reasons,
        exit_plan=exit_plan,
        detail={
            "equity": equity,
            "max_loss_per_contract": structure.max_loss,
            "contracts": contracts,
            "open_positions": open_positions,
            "same_direction_positions": same_direction_positions,
            "proposed_bias": proposed_bias,
            "circuit_breaker_halted": halted,
            "exit_plan": exit_plan.to_dict() if exit_plan else None,
        },
    )
