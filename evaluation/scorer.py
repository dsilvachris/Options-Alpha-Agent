"""
Section 4 — Opportunity Evaluation Framework.

A scored checklist, not a weighted model. Each check is pass/fail and worth a
fixed point value from config.SCORING.points. The score is the plain sum of the
points from checks that passed — there are no fitted coefficients and no tuned
weights anywhere in this module, so a score is always traceable back to the
specific list of conditions met (Section 4.5).

Hard gates
----------
Checks 7 (liquidity), 8 (event clear) and 9 (portfolio fit) are mandatory.
They are evaluated first and short-circuit: if any one fails, the opportunity is
rejected outright and the score sum is never reached (Section 4.2).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config import RISK, SCORING, SIGNALS
from decision.matrix import IRON_CONDOR, MatrixResult
from decision.structure import ProposedStructure
from signals.trend import TrendReading
from signals.volatility import VolatilityReading

TRADE = "TRADE"
WATCH = "WATCH"
REJECT = "REJECT"

CHECK_NAMES = {
    1: "premium rich",
    2: "volatility stable",
    3: "trend clarity",
    4: "directional agreement",
    5: "credit quality",
    6: "probability profile",
    7: "liquidity",
    8: "event clear",
    9: "portfolio fit",
}


@dataclass
class Check:
    """
    One checklist item, in one of three states.

    passed=True                 the condition was tested and held
    passed=False, evaluable     the condition was tested and did not hold
    evaluable=False             the condition COULD NOT BE TESTED — the data
                                needed to answer it does not exist yet

    The third state exists because a check that cannot be evaluated is not the
    same as a check that failed. Scoring a missing measurement as a failure
    penalises a candidate for the agent's own lack of history, which is what
    check 2 was doing on a fresh store. A non-evaluable check is excluded from
    both the numerator and the denominator, and the total is rescaled, so the
    score bands keep meaning the same thing.
    """

    id: int
    name: str
    passed: bool
    measured: str
    threshold: str
    points: int
    hard_gate: bool = False
    evaluable: bool = True

    @property
    def awarded(self) -> int:
        return self.points if (self.evaluable and self.passed) else 0

    @property
    def available(self) -> int:
        """Points this check contributes to the denominator."""
        return self.points if self.evaluable else 0

    @property
    def state(self) -> str:
        if not self.evaluable:
            return "NOT_EVALUABLE"
        return "PASS" if self.passed else "FAIL"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "passed": self.passed,
            "evaluable": self.evaluable,
            "state": self.state,
            "measured": self.measured,
            "threshold": self.threshold,
            "points": self.points,
            "awarded": self.awarded,
            "available": self.available,
            "hard_gate": self.hard_gate,
        }


@dataclass
class PortfolioState:
    """Portfolio facts check 9 needs."""

    open_positions: int
    same_direction_positions: int = 0
    proposed_bias: int = 0


@dataclass
class ScoreResult:
    #: Score on the 0-100 scale the bands are defined against. When a check is
    #: not evaluable this is the raw score rescaled over the available points.
    score: int
    state: str
    checks: list[Check] = field(default_factory=list)
    failed_hard_gate: Check | None = None
    reason: str = ""
    promoting_condition: str = ""
    #: Points actually earned, before rescaling.
    raw_score: int = 0
    #: Points that were available to earn (100 unless a check was skipped).
    available_points: int = 100

    @property
    def rescaled(self) -> bool:
        return self.available_points != 100

    @property
    def not_evaluable(self) -> list[Check]:
        return [c for c in self.checks if not c.evaluable]

    @property
    def checks_dict(self) -> list[dict]:
        return [c.to_dict() for c in self.checks]

    @property
    def passed_ids(self) -> list[int]:
        return [c.id for c in self.checks if c.passed]

    @property
    def failed(self) -> list[Check]:
        """Checks that were tested and did not hold. Excludes non-evaluable."""
        return [c for c in self.checks if c.evaluable and not c.passed]

    def breakdown(self) -> str:
        """Section 4.5 — full component breakdown of the score."""
        header = f"Opportunity score: {self.score}/100 -> {self.state}"
        if self.rescaled:
            header += (f"  (earned {self.raw_score} of {self.available_points} "
                       f"available points, rescaled to 100)")
        lines = [header]
        for check in sorted(self.checks, key=lambda c: c.id):
            mark = check.state
            gate = " [HARD GATE]" if check.hard_gate else ""
            lines.append(
                f"  #{check.id} {check.name:<22} {mark}  "
                f"{check.awarded:>2}/{check.points:<2} pts{gate}\n"
                f"       measured: {check.measured}\n"
                f"       required: {check.threshold}"
            )
        if self.reason:
            lines.append(f"  reason: {self.reason}")
        return "\n".join(lines)


def _fmt(value: float | None, spec: str = ".4f") -> str:
    return format(value, spec) if value is not None else "unavailable"


# ---------------------------------------------------------------------------
# Hard gates (Section 4.2) — evaluated before the score sum.
# ---------------------------------------------------------------------------


def check_7_liquidity(structure: ProposedStructure) -> Check:
    """Spread bid-ask within a percentage of credit, and open interest floor."""
    spread_ok = structure.spread_pct_of_credit <= SCORING.max_spread_pct_of_credit
    oi = structure.min_open_interest
    oi_ok = oi is not None and oi >= SCORING.min_open_interest
    measured = (
        f"bid-ask {structure.total_spread:.2f} pts = "
        f"{structure.spread_pct_of_credit * 100:.1f}% of credit "
        f"${structure.credit:.2f}; min open interest "
        f"{oi if oi is not None else 'unavailable'}"
    )
    return Check(
        7,
        CHECK_NAMES[7],
        bool(spread_ok and oi_ok),
        measured,
        f"bid-ask <= {SCORING.max_spread_pct_of_credit * 100:.0f}% of credit and "
        f"open interest >= {SCORING.min_open_interest} per leg",
        SCORING.points[7],
        hard_gate=True,
    )


def check_8_event_clear(corporate_actions: list[dict], symbol: str) -> Check:
    """No earnings or major scheduled event on the underlying before expiry."""
    blocking = [
        a for a in corporate_actions
        if str(a.get("ca_type") or a.get("type") or "").strip().title()
        in {t.title() for t in RISK.blocking_corporate_actions}
    ] or corporate_actions
    is_etf = symbol.upper() in {s.upper() for s in RISK.etf_symbols}
    passed = len(blocking) == 0
    if is_etf:
        measured = (
            f"{len(blocking)} blocking corporate action(s) before expiry; "
            f"{symbol} is a configured ETF (no issuer earnings)"
        )
    else:
        measured = (
            f"{len(blocking)} blocking corporate action(s) before expiry; "
            f"{symbol} is NOT a configured ETF — no earnings calendar is "
            "available from the Alpaca MCP server"
        )
        passed = False
    return Check(
        8,
        CHECK_NAMES[8],
        passed,
        measured,
        "no earnings or major scheduled event on the underlying before expiry",
        SCORING.points[8],
        hard_gate=True,
    )


def check_9_portfolio_fit(portfolio: PortfolioState) -> Check:
    """Adding the position keeps directional exposure and count inside limits."""
    count_ok = portfolio.open_positions + 1 <= RISK.max_concurrent_positions
    directional_ok = (
        portfolio.proposed_bias == 0
        or portfolio.same_direction_positions + 1 <= RISK.max_same_direction_positions
    )
    measured = (
        f"open positions {portfolio.open_positions} (+1 proposed); "
        f"same-direction positions {portfolio.same_direction_positions} "
        f"(bias {portfolio.proposed_bias:+d})"
    )
    return Check(
        9,
        CHECK_NAMES[9],
        bool(count_ok and directional_ok),
        measured,
        f"position count <= {RISK.max_concurrent_positions} and same-direction "
        f"positions <= {RISK.max_same_direction_positions}",
        SCORING.points[9],
        hard_gate=True,
    )


# ---------------------------------------------------------------------------
# Scored checks.
# ---------------------------------------------------------------------------


def check_1_premium_rich(vol: VolatilityReading) -> Check:
    passed = (
        vol.iv_rv_ratio is not None
        and vol.iv_rv_ratio >= SCORING.premium_rich_iv_rv_ratio
    )
    measured = (
        f"ATM IV {_fmt(vol.atm_iv)} vs realized vol {_fmt(vol.realized_vol)} "
        f"= ratio {_fmt(vol.iv_rv_ratio, '.3f')}"
    )
    return Check(
        1, CHECK_NAMES[1], passed, measured,
        f"ATM IV >= {SCORING.premium_rich_iv_rv_ratio} x 20-day realized vol",
        SCORING.points[1],
    )


def check_2_volatility_stable(vol: VolatilityReading) -> Check:
    """
    Is implied volatility expanding sharply into entry?

    Requires a real trailing average. With one or two prior sessions the
    "average" is a single day or two of noise, not a stability signal — it
    cannot answer the question, so the check is marked NOT EVALUABLE and its
    points leave both the numerator and the denominator rather than failing
    every candidate for history the agent has not accumulated yet.

    The 1.15 threshold is unchanged; once the required sessions exist the check
    evaluates normally.
    """
    required = SIGNALS.iv_average_window

    if vol.sample_size < required:
        return Check(
            2, CHECK_NAMES[2],
            passed=False,
            measured=(f"needs {required} sessions, has {vol.sample_size}"),
            threshold=(f"at least {required} prior sessions of ATM IV, then "
                       f"ATM IV <= {SCORING.iv_stability_max_ratio} x their average"),
            points=SCORING.points[2],
            evaluable=False,
        )

    if vol.iv_change_ratio is None:
        return Check(
            2, CHECK_NAMES[2], False,
            f"{vol.sample_size} prior session(s) recorded but the current ATM IV "
            "could not be read",
            f"ATM IV <= {SCORING.iv_stability_max_ratio} x its trailing average",
            SCORING.points[2],
        )

    passed = vol.iv_change_ratio <= SCORING.iv_stability_max_ratio
    measured = (
        f"ATM IV {_fmt(vol.atm_iv)} vs {vol.sample_size}-session average "
        f"{_fmt(vol.iv_average)} = ratio {_fmt(vol.iv_change_ratio, '.3f')}"
    )
    return Check(
        2, CHECK_NAMES[2], passed, measured,
        f"ATM IV <= {SCORING.iv_stability_max_ratio} x its trailing average",
        SCORING.points[2],
    )


def check_3_trend_clarity(trend: TrendReading, matrix: MatrixResult) -> Check:
    """
    Check 3 is structure-aware: it tests whether the tape confirms the thesis of
    the structure the matrix selected, not whether a trend exists in the abstract.

    Rationale
    ---------
    A directional credit spread is a bet that a trend continues, so it needs a
    clear trend: MA separation ABOVE the threshold.

    An iron condor is the opposite bet — it profits if the underlying stays put.
    The matrix only selects a condor *because* the tape is range-bound, so
    scoring it against "is there a clear trend?" penalised the structure for its
    own premise and capped every condor 15 points below a credit spread. For a
    condor the check therefore passes when MA separation is BELOW the threshold,
    confirming the range-bound condition.

    Both directions are worth the same 15 points: the check asks "is the tape
    behaving the way this structure needs?", and that question is equally
    answered either way.
    """
    if trend.separation is None:
        return Check(
            3, CHECK_NAMES[3], False,
            "MA separation unavailable (insufficient price history)",
            "tape must confirm the selected structure's thesis",
            SCORING.points[3],
        )

    separation = abs(trend.separation)
    wants_range = matrix.structure == IRON_CONDOR

    if wants_range:
        passed = separation <= trend.threshold
        requirement = (
            f"iron condor requires a range-bound tape: "
            f"|MA separation| <= {trend.threshold:.4f} of price"
        )
    else:
        passed = separation > trend.threshold
        requirement = (
            f"directional structure requires a clear trend: "
            f"|MA separation| > {trend.threshold:.4f} of price"
        )

    measured = (
        f"MA separation {_fmt(trend.separation, '.4f')} of price "
        f"(MA short {_fmt(trend.ma_short, '.2f')}, MA long {_fmt(trend.ma_long, '.2f')}); "
        f"structure {matrix.structure} wants "
        f"{'range-bound' if wants_range else 'a clear trend'}"
    )
    return Check(3, CHECK_NAMES[3], passed, measured, requirement, SCORING.points[3])


def check_4_directional_agreement(matrix: MatrixResult, trend: TrendReading) -> Check:
    """
    A neutral structure has no directional bias to disagree with, so an iron
    condor auto-passes. Requiring a directional match from a deliberately
    non-directional structure would penalise it twice over — once here and once
    in check 3.
    """
    if matrix.bias == 0:
        return Check(
            4, CHECK_NAMES[4], True,
            f"{matrix.structure} is non-directional (bias 0): no directional "
            f"agreement required; measured trend {trend.condition}",
            "non-directional structures auto-pass",
            SCORING.points[4],
        )
    passed = matrix.bias == trend.direction
    measured = (
        f"structure bias {matrix.bias:+d} ({matrix.structure}) vs "
        f"trend direction {trend.direction:+d} ({trend.condition})"
    )
    return Check(
        4, CHECK_NAMES[4], passed, measured,
        "structure bias matches the measured trend direction",
        SCORING.points[4],
    )


def check_5_credit_quality(structure: ProposedStructure) -> Check:
    ratio = structure.credit_to_width
    passed = structure.is_credit and ratio >= SCORING.min_credit_to_width
    measured = (
        f"credit ${structure.credit:.2f} / width ${structure.width * 100:.2f} "
        f"= {ratio:.3f}"
        + ("" if structure.is_credit else " (debit structure: no credit collected)")
    )
    return Check(
        5, CHECK_NAMES[5], passed, measured,
        f"credit / spread width >= {SCORING.min_credit_to_width}",
        SCORING.points[5],
    )


def check_6_probability(structure: ProposedStructure) -> Check:
    passed = structure.short_delta <= SCORING.max_short_delta
    measured = f"short-strike delta {structure.short_delta:.3f}"
    return Check(
        6, CHECK_NAMES[6], passed, measured,
        f"short-strike |delta| <= {SCORING.max_short_delta}",
        SCORING.points[6],
    )


# ---------------------------------------------------------------------------
# Orchestration.
# ---------------------------------------------------------------------------


def score(
    *,
    vol: VolatilityReading,
    trend: TrendReading,
    matrix: MatrixResult,
    structure: ProposedStructure,
    corporate_actions: list[dict],
    portfolio: PortfolioState,
) -> ScoreResult:
    """
    Run the checklist and resolve a score band.

    Hard gates are evaluated first and short-circuit before the score sum, per
    Section 4.2. When a gate fails, the soft checks are still recorded for
    explainability but contribute no points.
    """
    gates = [
        check_7_liquidity(structure),
        check_8_event_clear(corporate_actions, structure.symbol),
        check_9_portfolio_fit(portfolio),
    ]
    soft = [
        check_1_premium_rich(vol),
        check_2_volatility_stable(vol),
        check_3_trend_clarity(trend, matrix),
        check_4_directional_agreement(matrix, trend),
        check_5_credit_quality(structure),
        check_6_probability(structure),
    ]
    all_checks = sorted(soft + gates, key=lambda c: c.id)

    # A hard gate must be answerable; an unverifiable mandatory condition is a
    # rejection, never a skip.
    unverifiable_gate = next((g for g in gates if not g.evaluable), None)
    if unverifiable_gate is not None:
        return ScoreResult(
            score=0, state=REJECT, checks=all_checks,
            failed_hard_gate=unverifiable_gate,
            reason=(f"hard gate #{unverifiable_gate.id} {unverifiable_gate.name} "
                    f"could not be evaluated — {unverifiable_gate.measured}"),
            raw_score=0, available_points=0,
        )

    failed_gate = next((g for g in gates if not g.passed), None)
    if failed_gate is not None:
        return ScoreResult(
            score=0,
            state=REJECT,
            checks=all_checks,
            failed_hard_gate=failed_gate,
            reason=(
                f"hard gate #{failed_gate.id} {failed_gate.name} failed — "
                f"{failed_gate.measured}"
            ),
            raw_score=0,
            available_points=sum(c.available for c in all_checks),
        )

    raw = sum(c.awarded for c in all_checks)
    available = sum(c.available for c in all_checks)
    # Rescale so the bands keep their meaning when a check could not be tested:
    # 75 earned out of 90 available is the same quality as 83 out of 100.
    total = round(raw / available * 100) if available else 0

    skipped = [c for c in all_checks if not c.evaluable]
    suffix = ""
    if skipped:
        suffix = (
            f"; {raw}/{available} available points rescaled to {total}/100"
            f" (not evaluable: "
            + ", ".join(f"#{c.id} {c.name}" for c in skipped) + ")"
        )

    if total >= SCORING.trade_band:
        state = TRADE
        reason = f"score {total} is in the execution band (>= {SCORING.trade_band}){suffix}"
    elif total >= SCORING.watch_band:
        state = WATCH
        reason = (
            f"score {total} is in the moderate band "
            f"({SCORING.watch_band}-{SCORING.trade_band - 1}){suffix}"
        )
    else:
        state = REJECT
        reason = (f"score {total} is below the execution threshold "
                  f"({SCORING.watch_band}){suffix}")

    promoting = ""
    if state == WATCH:
        shortfall = SCORING.trade_band - total
        outstanding = [c for c in all_checks if c.evaluable and not c.passed]

        def scaled_with(check: Check) -> int:
            return round((raw + check.points) / available * 100) if available else 0

        sufficient = sorted(
            (c for c in outstanding if scaled_with(c) >= SCORING.trade_band),
            key=lambda c: c.points,
        )
        if sufficient:
            target = sufficient[0]
            promoting = (
                f"check #{target.id} {target.name} must pass "
                f"(+{target.points} raw pts -> score {scaled_with(target)}, "
                f"needs {shortfall}) — requires {target.threshold}"
            )
        elif outstanding:
            needed = ", ".join(f"#{c.id} {c.name} (+{c.points})" for c in outstanding)
            promoting = (
                f"needs {shortfall} more points; no single check suffices — "
                f"outstanding: {needed}"
            )
        else:
            # Everything testable passed; only non-evaluable checks remain.
            waiting = ", ".join(f"#{c.id} {c.name} ({c.measured})" for c in skipped)
            promoting = (
                f"every evaluable check passed; needs {shortfall} more points, "
                f"available only once {waiting} can be evaluated"
            )

    return ScoreResult(
        score=total,
        state=state,
        checks=all_checks,
        reason=reason,
        promoting_condition=promoting,
        raw_score=raw,
        available_points=available,
    )
