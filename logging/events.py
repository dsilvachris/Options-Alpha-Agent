"""
Section 6 — timestamped pipeline stage events.

Every stage the agent moves through emits a real backend event with a payload.
The dashboard reads these rows; it never simulates progress. Stage names are
fixed so the pipeline view can render them in order.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from config import fmt_et
from logging.store import Store, get_store, utcnow


class Stage:
    """The six pipeline stages of Section 6, plus lifecycle stages."""

    MARKET_SCAN = "MARKET_SCAN"
    MARKET_ANALYSIS = "MARKET_ANALYSIS"
    STRATEGY_SELECTION = "STRATEGY_SELECTION"
    OPPORTUNITY_EVALUATION = "OPPORTUNITY_EVALUATION"
    RISK_REVIEW = "RISK_REVIEW"
    FINAL_DECISION = "FINAL_DECISION"

    #: Ordered stages rendered by dashboard/pipeline.py.
    PIPELINE = (
        MARKET_SCAN,
        MARKET_ANALYSIS,
        STRATEGY_SELECTION,
        OPPORTUNITY_EVALUATION,
        RISK_REVIEW,
        FINAL_DECISION,
    )

    # Lifecycle / out-of-band stages.
    CYCLE_START = "CYCLE_START"
    CYCLE_END = "CYCLE_END"
    MCP = "MCP"
    EXECUTION = "EXECUTION"
    POSITION_MANAGEMENT = "POSITION_MANAGEMENT"
    WATCH_TRANSITION = "WATCH_TRANSITION"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"
    BASELINE = "BASELINE"
    ERROR = "ERROR"


def new_cycle_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class EventLog:
    """Cycle-scoped event emitter. Writes straight through to the store."""

    cycle_id: str
    store: Store
    echo: bool = False

    @classmethod
    def start(cls, store: Store | None = None, echo: bool = False) -> "EventLog":
        return cls(new_cycle_id(), store or get_store(), echo)

    def emit(
        self,
        stage: str,
        message: str = "",
        *,
        symbol: str | None = None,
        payload: Any = None,
    ) -> None:
        self.store.add_event(
            stage,
            message,
            cycle_id=self.cycle_id,
            symbol=symbol,
            payload=payload,
        )
        if self.echo:
            # Timestamps are stored in UTC; humans read them in market time.
            clock = fmt_et(utcnow(), "%H:%M:%S")
            tag = f"[{stage}]"
            sym = f" {symbol}" if symbol else ""
            print(f"{clock}  {tag:<26}{sym:<6} {message}")

    def error(self, message: str, *, symbol: str | None = None, payload: Any = None) -> None:
        self.emit(Stage.ERROR, message, symbol=symbol, payload=payload)
