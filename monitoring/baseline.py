"""
Section 8.3 — Filter value baselines.

Two naive baselines are recorded over the same window, on the same watchlist,
carrying no capital and no risk. They exist to answer "did the opportunity score
add anything?" — not to be traded.

Unfiltered baseline: every setup the strategy matrix matched, ignoring the
                     opportunity score and all soft checks. Recorded as a
                     notional P&L on one contract per matched setup.
Passive baseline:    buy and hold the primary underlying, indexed to the first
                     observation in the window.

Both are computed as a single additional row appended inside the loop the agent
already runs — no separate data pull, no extra MCP traffic.
"""
from __future__ import annotations

import bootstrap  # noqa: F401  - must precede `logging.*` submodule imports

import json
from dataclasses import dataclass, field
from typing import Any

from config import UNIVERSE
from logging.events import EventLog, Stage
from logging.store import Store

UNFILTERED = "unfiltered"
PASSIVE = "passive"


@dataclass
class BaselineSnapshot:
    unfiltered_setups: int = 0
    unfiltered_notional_credit: float = 0.0
    passive_first_price: float | None = None
    passive_last_price: float | None = None
    passive_return_pct: float | None = None
    rows: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "unfiltered_setups": self.unfiltered_setups,
            "unfiltered_notional_credit": round(self.unfiltered_notional_credit, 2),
            "passive_symbol": UNIVERSE.primary_underlying,
            "passive_first_price": self.passive_first_price,
            "passive_last_price": self.passive_last_price,
            "passive_return_pct": self.passive_return_pct,
        }

    def render(self) -> str:
        lines = ["FILTER VALUE BASELINES (Section 8.3) — recorded, never traded", ""]
        lines.append(
            f"Unfiltered baseline: {self.unfiltered_setups} matched setup(s) taken "
            f"without regard to score, notional credit "
            f"${self.unfiltered_notional_credit:,.2f} (1 contract each)"
        )
        if self.passive_first_price and self.passive_last_price:
            lines.append(
                f"Passive baseline:    buy and hold {UNIVERSE.primary_underlying} "
                f"${self.passive_first_price:,.2f} -> ${self.passive_last_price:,.2f} "
                f"({self.passive_return_pct:+.2f}%)"
            )
        else:
            lines.append(
                f"Passive baseline:    {UNIVERSE.primary_underlying} — "
                "insufficient observations recorded yet"
            )
        return "\n".join(lines)


class BaselineRecorder:
    """Appends baseline rows inside the agent's own scan loop."""

    def __init__(self, store: Store, events: EventLog) -> None:
        self.store = store
        self.events = events

    def record_unfiltered(
        self,
        symbol: str,
        structure: str,
        credit: float,
        max_loss: float,
        score: int,
        taken_by_agent: bool,
    ) -> None:
        """
        One row per setup the matrix matched, regardless of score.

        This is the counterfactual: what the agent would have on if it ignored
        the opportunity evaluation framework entirely.
        """
        self.store.add_baseline(
            UNFILTERED,
            symbol,
            credit,
            detail={
                "structure": structure,
                "credit": credit,
                "max_loss": max_loss,
                "score": score,
                "taken_by_agent": taken_by_agent,
            },
            cycle_id=self.events.cycle_id,
        )

    def record_passive(self, symbol: str, price: float) -> None:
        """One price observation for the buy-and-hold baseline."""
        if symbol.upper() != UNIVERSE.primary_underlying.upper():
            return
        self.store.add_baseline(
            PASSIVE, symbol, price, detail={"price": price}, cycle_id=self.events.cycle_id
        )

    def emit_summary(self) -> BaselineSnapshot:
        snapshot = build(self.store)
        self.events.emit(
            Stage.BASELINE,
            f"Baselines — unfiltered {snapshot.unfiltered_setups} setup(s); "
            f"passive {UNIVERSE.primary_underlying} "
            f"{snapshot.passive_return_pct if snapshot.passive_return_pct is not None else 'n/a'}",
            payload=snapshot.to_dict(),
        )
        return snapshot


def build(store: Store) -> BaselineSnapshot:
    unfiltered = store.baseline_rows(UNFILTERED)
    passive = store.baseline_rows(PASSIVE)

    notional = 0.0
    for row in unfiltered:
        try:
            detail = json.loads(row["detail"]) if row.get("detail") else {}
        except (TypeError, ValueError):
            detail = {}
        notional += float(detail.get("credit") or 0.0)

    first = float(passive[0]["value"]) if passive else None
    last = float(passive[-1]["value"]) if passive else None
    ret = ((last - first) / first * 100.0) if (first and last) else None

    return BaselineSnapshot(
        unfiltered_setups=len(unfiltered),
        unfiltered_notional_credit=notional,
        passive_first_price=first,
        passive_last_price=last,
        passive_return_pct=round(ret, 4) if ret is not None else None,
        rows=passive,
    )
