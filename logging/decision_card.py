"""
Section 7 — templated decision card generation.

Cards are rendered from recorded numbers only. There is no free-form prose
anywhere in this module: every line is a format string filled from the
evaluation result, so the narrative cannot drift from the data it describes.
The same card text is reused by the dashboard, CLI and documentation.
"""
from __future__ import annotations

from typing import Any


def _evaluable(check: dict) -> bool:
    """Older recorded checks predate the tri-state and are all evaluable."""
    return check.get("evaluable", True)


def _fmt_checks_passed(checks: list[dict]) -> str:
    passed = [f"#{c['id']} {c['name']}" for c in checks
              if _evaluable(c) and c["passed"]]
    return ", ".join(passed) if passed else "none"


def _fmt_checks_failed(checks: list[dict]) -> str:
    failed = [f"#{c['id']} {c['name']} ({c['measured']})" for c in checks
              if _evaluable(c) and not c["passed"]]
    return "; ".join(failed) if failed else "none"


def _fmt_checks_not_evaluable(checks: list[dict]) -> str:
    """
    Non-evaluable checks are stated on the card, never omitted.

    A check excluded from the score has to be visible, or the reader cannot tell
    a 100 earned on nine checks from a 100 earned on eight.
    """
    skipped = [f"#{c['id']} {c['name']} — NOT EVALUABLE ({c['measured']})"
               for c in checks if not _evaluable(c)]
    return "; ".join(skipped)


def _score_line(score: int, checks: list[dict]) -> str:
    """Score line, showing the rescale whenever a check was skipped."""
    available = sum(c.get("available", c["points"]) for c in checks)
    earned = sum(c["awarded"] for c in checks)
    if available and available != 100:
        return (f"Opportunity Score: {score}/100 "
                f"({earned} of {available} available points, rescaled)")
    return f"Opportunity Score: {score}/100"


def trade_card(
    *,
    symbol: str,
    iv_condition: str,
    trend_condition: str,
    structure: str,
    score: int,
    checks: list[dict],
    credit: float,
    width: float,
    breakeven: float,
    max_loss: float,
    dte: int,
    contracts: int,
    max_loss_cap: float,
    position_count: int,
    position_limit: int,
) -> str:
    """Section 7.1 — Template: Trade Taken."""
    return (
        f"Underlying: {symbol}\n"
        f"Market Regime: {iv_condition}, {trend_condition}\n"
        f"Selected Strategy: {structure}\n"
        f"{_score_line(score, checks)} — checks passed: {_fmt_checks_passed(checks)}; "
        f"failed: {_fmt_checks_failed(checks)}\n"
        + (f"Not evaluated: {_fmt_checks_not_evaluable(checks)}\n"
           if _fmt_checks_not_evaluable(checks) else "")
        + f"Position: credit received ${credit:.2f} x {contracts} contract(s), "
        f"spread width ${width:.2f}, breakeven ${breakeven:.2f}, "
        f"maximum loss ${max_loss:.2f}, {dte} days to expiry\n"
        f"Risk Gate: approved — maximum loss ${max_loss:.2f} within the per-trade cap "
        f"${max_loss_cap:.2f}; position count {position_count}/{position_limit} within limit\n"
        f"Final Decision: EXECUTE TRADE"
    )


def declined_card(
    *,
    symbol: str,
    iv_condition: str,
    trend_condition: str,
    structure: str,
    score: int,
    reason: str,
    checks: list[dict] | None = None,
) -> str:
    """
    Section 7.2 — Template: Opportunity Declined.

    `checks` is optional and additive: the spec's template carries only the score
    and the rejection reason, but a score computed over fewer than 100 available
    points must say so on the card. Hiding a rescale would make an 83 earned on
    eight checks indistinguishable from an 83 earned on nine.
    """
    checks = checks or []
    skipped = _fmt_checks_not_evaluable(checks) if checks else ""
    return (
        f"Underlying: {symbol}\n"
        f"Market Regime: {iv_condition}, {trend_condition}\n"
        f"Selected Strategy: {structure}\n"
        + (f"{_score_line(score, checks)}\n" if checks
           else f"Opportunity Score: {score}/100\n")
        + (f"Not evaluated: {skipped}\n" if skipped else "")
        + f"Reason for rejection: {reason}\n"
        f"Final Decision: NO TRADE"
    )


def watch_card(
    *,
    symbol: str,
    iv_condition: str,
    trend_condition: str,
    structure: str,
    score: int,
    checks: list[dict],
    promoting_condition: str,
    cycles_seen: int,
    expires_after_cycle: int,
) -> str:
    """
    Section 5 — WATCH record.

    The spec fixes templates for TRADE and NO TRADE only; WATCH is recorded with
    the fields Section 5 requires (failing checks, current score, and the
    condition that would promote it to TRADE), in the same templated style.
    """
    return (
        f"Underlying: {symbol}\n"
        f"Market Regime: {iv_condition}, {trend_condition}\n"
        f"Selected Strategy: {structure}\n"
        f"{_score_line(score, checks)} — failed: {_fmt_checks_failed(checks)}\n"
        + (f"Not evaluated: {_fmt_checks_not_evaluable(checks)}\n"
           if _fmt_checks_not_evaluable(checks) else "")
        + f"Promoting condition: {promoting_condition}\n"
        f"Watch age: cycle {cycles_seen} of {expires_after_cycle}\n"
        f"Final Decision: WATCH"
    )


def expired_card(*, symbol: str, structure: str, score: int, cycles_seen: int, reason: str) -> str:
    """Section 5.1 — WATCH item closed out as EXPIRED."""
    return (
        f"Underlying: {symbol}\n"
        f"Selected Strategy: {structure}\n"
        f"Last Opportunity Score: {score}/100\n"
        f"Watched for: {cycles_seen} cycle(s)\n"
        f"Reason for expiry: {reason}\n"
        f"Final Decision: WATCH EXPIRED"
    )


def exit_card(
    *,
    symbol: str,
    structure: str,
    exit_reason: str,
    credit: float,
    exit_cost: float,
    realized_pnl: float,
    contracts: int,
) -> str:
    """Section 9.6 — why a position was exited when it was."""
    return (
        f"Underlying: {symbol}\n"
        f"Structure: {structure}\n"
        f"Exit trigger: {exit_reason}\n"
        f"Credit collected: ${credit:.2f} x {contracts} contract(s); "
        f"exit cost ${exit_cost:.2f}\n"
        f"Realized P&L: ${realized_pnl:.2f}\n"
        f"Final Decision: POSITION CLOSED"
    )


def render(state: str, payload: dict[str, Any]) -> str:
    """Dispatch to the correct template for a decision state."""
    if state == "TRADE":
        return trade_card(**payload)
    if state == "WATCH":
        return watch_card(**payload)
    if state == "EXPIRED":
        return expired_card(**payload)
    if state == "EXIT":
        return exit_card(**payload)
    return declined_card(**payload)
