"""
Section 6 — Visible decision pipeline.

Reads the recorded `events` table and groups stage events into cycles so the
front end can render the agent's reasoning as an ordered sequence of stages.

This module reads state; it never generates it. If a stage has no recorded event
it is reported as "pending" rather than being filled in — the dashboard must
never simulate progress that the backend did not actually make.
"""
from __future__ import annotations

import bootstrap  # noqa: F401  - must precede `logging.*` submodule imports

import json
from dataclasses import dataclass, field
from typing import Any

from logging.events import Stage
from logging.store import Store

STAGE_LABELS = {
    Stage.MARKET_SCAN: "Market Scan",
    Stage.MARKET_ANALYSIS: "Market Analysis",
    Stage.STRATEGY_SELECTION: "Strategy Selection",
    Stage.OPPORTUNITY_EVALUATION: "Opportunity Evaluation",
    Stage.RISK_REVIEW: "Risk Review",
    Stage.FINAL_DECISION: "Final Decision",
}


@dataclass
class StageView:
    stage: str
    label: str
    status: str            # "complete" | "pending"
    events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "label": self.label,
            "status": self.status,
            "count": len(self.events),
            "events": self.events,
        }


def _parse(event: dict) -> dict:
    payload: Any = None
    if event.get("payload"):
        try:
            payload = json.loads(event["payload"])
        except (TypeError, ValueError):
            payload = event["payload"]
    return {
        "id": event["id"],
        "ts": event["ts"],
        "stage": event["stage"],
        "symbol": event.get("symbol"),
        "message": event.get("message"),
        "payload": payload,
    }


def latest_cycle_id(store: Store) -> str | None:
    rows = store.query(
        "SELECT cycle_id FROM events WHERE cycle_id IS NOT NULL ORDER BY id DESC LIMIT 1"
    )
    return rows[0]["cycle_id"] if rows else None


def cycle_pipeline(store: Store, cycle_id: str | None = None) -> dict:
    """The six Section 6 stages for one cycle, in order, from recorded events."""
    cycle = cycle_id or latest_cycle_id(store)
    if cycle is None:
        return {
            "cycle_id": None,
            "stages": [
                StageView(s, STAGE_LABELS[s], "pending").to_dict() for s in Stage.PIPELINE
            ],
            "started": None,
            "ended": None,
            "complete": False,
        }

    rows = store.query(
        "SELECT * FROM events WHERE cycle_id=? ORDER BY id ASC", (cycle,)
    )
    parsed = [_parse(r) for r in rows]
    by_stage: dict[str, list[dict]] = {s: [] for s in Stage.PIPELINE}
    for event in parsed:
        if event["stage"] in by_stage:
            by_stage[event["stage"]].append(event)

    stages = [
        StageView(
            stage,
            STAGE_LABELS[stage],
            "complete" if by_stage[stage] else "pending",
            by_stage[stage],
        ).to_dict()
        for stage in Stage.PIPELINE
    ]

    started = next((e["ts"] for e in parsed if e["stage"] == Stage.CYCLE_START), None)
    ended = next((e["ts"] for e in parsed if e["stage"] == Stage.CYCLE_END), None)

    return {
        "cycle_id": cycle,
        "stages": stages,
        "started": started,
        "ended": ended,
        "complete": ended is not None,
        "other_events": [
            e for e in parsed
            if e["stage"] not in by_stage and e["stage"] not in
            {Stage.CYCLE_START, Stage.CYCLE_END}
        ],
    }


def recent_cycles(store: Store, limit: int = 20) -> list[dict]:
    """Cycle index for the dashboard's cycle selector."""
    rows = store.query(
        "SELECT cycle_id, MIN(ts) AS started, MAX(ts) AS ended, COUNT(*) AS events"
        " FROM events WHERE cycle_id IS NOT NULL GROUP BY cycle_id"
        " ORDER BY MAX(id) DESC LIMIT ?",
        (limit,),
    )
    return rows
