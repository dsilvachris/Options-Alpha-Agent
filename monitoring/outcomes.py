"""
Section 8.2 — Outcome measures.

Simulated money is never mixed with real results. Every P&L figure here counts
LIVE positions only: DRY_RUN positions are excluded from realized P&L, the closed
and open position lists, and the exit-trigger counts. They are reported
separately, with their own counts, so nothing is hidden — a dry-run session shows
what it would have done without ever claiming it earned anything.

Decisions are treated differently on purpose. The reasoning log (Section 7) and
the activity ledger (Section 8.1) count every decision the agent made, simulated
or not, because those measure *decision behaviour*, not money. Average
opportunity scores below therefore span all decisions.

Every figure is reported with its explicit sample size. Where a segment holds
fewer than five observations the raw count is shown rather than a rate
(Section 8.4), and averages carry an "n=" label so a two-trade average is never
mistaken for a performance statistic.
"""
from __future__ import annotations

import bootstrap  # noqa: F401  - must precede `logging.*` submodule imports

import json
from dataclasses import dataclass, field
from typing import Any

from logging.store import Store

SMALL_SAMPLE = 5


@dataclass
class Outcomes:
    realized_pnl: float = 0.0
    open_positions: list[dict] = field(default_factory=list)
    closed_positions: list[dict] = field(default_factory=list)
    exits_by_reason: dict[str, int] = field(default_factory=dict)
    equity_curve: list[dict] = field(default_factory=list)
    avg_score_executed: float | None = None
    n_executed: int = 0
    avg_score_declined: float | None = None
    n_declined: int = 0
    # Simulated positions, reported separately and never added to the above.
    dry_run_open: int = 0
    dry_run_closed: int = 0
    dry_run_notional_pnl: float = 0.0

    def to_dict(self) -> dict:
        return {
            "realized_pnl": round(self.realized_pnl, 2),
            "open_positions": self.open_positions,
            "closed_positions": self.closed_positions,
            "exits_by_reason": self.exits_by_reason,
            "equity_curve": self.equity_curve,
            "avg_score_executed": self.avg_score_executed,
            "n_executed": self.n_executed,
            "avg_score_declined": self.avg_score_declined,
            "n_declined": self.n_declined,
            "dry_run_open": self.dry_run_open,
            "dry_run_closed": self.dry_run_closed,
            "dry_run_notional_pnl": round(self.dry_run_notional_pnl, 2),
        }

    def render(self) -> str:
        lines = ["OUTCOME MEASURES (Section 8.2) — sample sizes stated",
                 "LIVE positions only; simulated positions are listed separately", ""]
        lines.append(f"Realized P&L: ${self.realized_pnl:,.2f} "
                     f"(n={len(self.closed_positions)} closed live position(s))")
        lines.append(f"Open positions: {len(self.open_positions)} live")
        if self.dry_run_open or self.dry_run_closed:
            lines.append(
                f"Simulated (DRY_RUN, excluded from every figure above): "
                f"{self.dry_run_open} open, {self.dry_run_closed} closed, "
                f"notional P&L ${self.dry_run_notional_pnl:,.2f}")
        if self.equity_curve:
            first = self.equity_curve[0]["equity"]
            last = self.equity_curve[-1]["equity"]
            lines.append(
                f"Account equity: ${first:,.2f} -> ${last:,.2f} "
                f"(n={len(self.equity_curve)} sample(s))"
            )
        lines.append("")

        lines.append("Exits by trigger:")
        if not self.exits_by_reason:
            lines.append("  (none)")
        for reason, count in sorted(self.exits_by_reason.items()):
            lines.append(f"  {reason:<24} {count}")
        lines.append("")

        lines.append("Result per closed position:")
        if not self.closed_positions:
            lines.append("  (none)")
        for position in self.closed_positions:
            pnl = position.get("realized_pnl")
            pnl_text = f"${pnl:,.2f}" if pnl is not None else "unrealized/unknown"
            lines.append(
                f"  {position['symbol']:<6} {position['structure']:<26} "
                f"{pnl_text:>16}  {position.get('exit_reason') or ''}"
            )
        lines.append("")

        lines.append("Average opportunity score:")
        lines.append(
            f"  executed trades: "
            f"{self.avg_score_executed if self.avg_score_executed is not None else 'n/a'}"
            f"  (n={self.n_executed})"
            + ("  [small sample — read as a raw count]" if self.n_executed < SMALL_SAMPLE else "")
        )
        lines.append(
            f"  declined:        "
            f"{self.avg_score_declined if self.avg_score_declined is not None else 'n/a'}"
            f"  (n={self.n_declined})"
            + ("  [small sample — read as a raw count]" if self.n_declined < SMALL_SAMPLE else "")
        )
        return "\n".join(lines)


def _classify_exit(reason: str) -> str:
    text = (reason or "").lower()
    if "profit target" in text:
        return "profit target"
    if "stop loss" in text:
        return "stop loss"
    if "time exit" in text:
        return "time-based exit"
    if "flatten" in text:
        return "end-of-window flatten"
    return "other"


def build(store: Store) -> Outcomes:
    # LIVE positions only. Simulated positions would otherwise put a fictional
    # realized gain on the dashboard next to a flat real equity curve.
    positions = store.all_positions(include_dry_run=False)
    closed = [p for p in positions if p["status"] == "CLOSED"]
    open_ = [p for p in positions if p["status"] == "OPEN"]

    simulated = store.dry_run_positions()
    dry_open = [p for p in simulated if p["status"] == "OPEN"]
    dry_closed = [p for p in simulated if p["status"] == "CLOSED"]
    dry_notional = sum(float(p["realized_pnl"] or 0.0) for p in dry_closed)

    realized = sum(float(p["realized_pnl"] or 0.0) for p in closed)

    exits: dict[str, int] = {}
    for position in closed:
        bucket = _classify_exit(position.get("exit_reason") or "")
        exits[bucket] = exits.get(bucket, 0) + 1

    decisions = store.all_decisions()
    executed = [d["score"] for d in decisions if d["state"] == "TRADE" and d["score"] is not None]
    declined = [
        d["score"] for d in decisions
        if d["state"] in {"REJECT", "WATCH", "EXPIRED"} and d["score"] is not None
    ]

    return Outcomes(
        realized_pnl=realized,
        open_positions=open_,
        closed_positions=closed,
        exits_by_reason=exits,
        equity_curve=store.equity_curve(),
        avg_score_executed=round(sum(executed) / len(executed), 1) if executed else None,
        n_executed=len(executed),
        avg_score_declined=round(sum(declined) / len(declined), 1) if declined else None,
        n_declined=len(declined),
        dry_run_open=len(dry_open),
        dry_run_closed=len(dry_closed),
        dry_run_notional_pnl=dry_notional,
    )
