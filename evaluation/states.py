"""
Section 5 — Decision state machine.

Every evaluated opportunity resolves to exactly one of TRADE, WATCH or REJECT.
WATCH is a tracked state with defined transitions, not a holding pen:

* Each watched item carries a created timestamp and an expiry expressed in scan
  cycles (config.LOOP.watch_expiry_cycles).
* WATCH items are persisted in SQLite and re-evaluated on each subsequent cycle
  against fresh market data. They are never recomputed from scratch — the stored
  item carries its age, its original promoting condition and its score history.
* If the promoting condition is not met inside the window, the item is closed out
  as EXPIRED with a reason.
* Every transition (WATCH -> TRADE, WATCH -> EXPIRED) is written as an event so
  the dashboard can display it over the course of a session.
"""
from __future__ import annotations

import bootstrap  # noqa: F401  - must precede `logging.*` submodule imports

import json
from dataclasses import dataclass
from typing import Any

from config import LOOP
from evaluation.scorer import REJECT, TRADE, WATCH, ScoreResult
from logging.decision_card import expired_card
from logging.events import EventLog, Stage
from logging.store import Store

EXPIRED = "EXPIRED"
PROMOTED = "PROMOTED"


def watch_key(symbol: str, structure: str, expiry: str) -> str:
    """Stable identity for a watched setup across cycles."""
    return f"{symbol}|{structure}|{expiry}"


@dataclass
class WatchOutcome:
    key: str
    status: str
    cycles_seen: int
    promoting_condition: str
    detail: dict[str, Any]


class WatchRegistry:
    """Persistent WATCH lifecycle (Section 5.1)."""

    def __init__(self, store: Store, events: EventLog) -> None:
        self.store = store
        self.events = events

    # -- recording ---------------------------------------------------------
    def record(
        self,
        *,
        symbol: str,
        structure: str,
        expiry: str,
        result: ScoreResult,
        detail: dict[str, Any] | None = None,
    ) -> WatchOutcome:
        """
        Register or refresh a WATCH item.

        An item seen again on a later cycle has its age incremented and its score
        refreshed; it is not recreated.
        """
        key = watch_key(symbol, structure, expiry)
        existing = self.store.get_watch_item(key)
        item = self.store.upsert_watch_item(
            key,
            symbol=symbol,
            structure=structure,
            score=result.score,
            promoting_condition=result.promoting_condition,
            expires_after_cycle=LOOP.watch_expiry_cycles,
            detail=detail or {},
        )
        transition = "WATCH_CREATED" if existing is None else "WATCH_REFRESHED"
        self.events.emit(
            Stage.WATCH_TRANSITION,
            f"{transition}: {symbol} {structure} score {result.score} "
            f"(cycle {item['cycles_seen']}/{LOOP.watch_expiry_cycles})",
            symbol=symbol,
            payload={
                "key": key,
                "transition": transition,
                "score": result.score,
                "cycles_seen": item["cycles_seen"],
                "promoting_condition": result.promoting_condition,
            },
        )
        return WatchOutcome(
            key=key,
            status=WATCH,
            cycles_seen=int(item["cycles_seen"]),
            promoting_condition=result.promoting_condition or "",
            detail=detail or {},
        )

    def promote(self, symbol: str, structure: str, expiry: str, score: int) -> bool:
        """WATCH -> TRADE. Returns True when an item was actually promoted."""
        key = watch_key(symbol, structure, expiry)
        item = self.store.get_watch_item(key)
        if item is None or item["status"] != WATCH:
            return False
        condition = item.get("promoting_condition") or "score reached the execution band"
        self.store.resolve_watch_item(
            key, PROMOTED, f"promoted to TRADE at score {score}: {condition}"
        )
        self.events.emit(
            Stage.WATCH_TRANSITION,
            f"WATCH -> TRADE: {symbol} {structure} promoted at score {score}",
            symbol=symbol,
            payload={
                "key": key,
                "transition": "WATCH_TO_TRADE",
                "score": score,
                "promoting_condition": condition,
                "cycles_watched": item["cycles_seen"],
            },
        )
        return True

    def reject(self, symbol: str, structure: str, expiry: str, reason: str) -> None:
        """A previously watched setup that now fails outright is closed out."""
        key = watch_key(symbol, structure, expiry)
        item = self.store.get_watch_item(key)
        if item is None or item["status"] != WATCH:
            return
        self.store.resolve_watch_item(key, REJECT, reason)
        self.events.emit(
            Stage.WATCH_TRANSITION,
            f"WATCH -> REJECT: {symbol} {structure} — {reason}",
            symbol=symbol,
            payload={"key": key, "transition": "WATCH_TO_REJECT", "reason": reason},
        )

    # -- expiry sweep ------------------------------------------------------
    def expire_stale(self, market_open: bool = True) -> list[dict]:
        """
        Close out WATCH items whose promoting condition was not met in the window.

        Run once per cycle, after all symbols have been evaluated, so an item
        refreshed this cycle is judged on its updated age.
        """
        expired: list[dict] = []
        for item in self.store.open_watch_items():
            if int(item["cycles_seen"]) < int(item["expires_after_cycle"]):
                continue
            condition = item.get("promoting_condition") or "promoting condition"
            reason = (
                f"promoting condition not met within "
                f"{item['expires_after_cycle']} cycle(s): {condition}"
            )
            self.store.resolve_watch_item(item["key"], EXPIRED, reason)

            detail = {}
            if item.get("detail"):
                try:
                    detail = json.loads(item["detail"])
                except (TypeError, ValueError):
                    detail = {}

            card = expired_card(
                symbol=item["symbol"],
                structure=item["structure"] or "unknown",
                score=int(item["score"] or 0),
                cycles_seen=int(item["cycles_seen"]),
                reason=reason,
            )
            self.store.add_decision(
                cycle_id=self.events.cycle_id,
                symbol=item["symbol"],
                state=EXPIRED,
                market_open=market_open,
                score=int(item["score"] or 0),
                structure=item["structure"],
                iv_condition=detail.get("iv_condition"),
                trend_condition=detail.get("trend_condition"),
                reason=reason,
                card=card,
                detail=detail,
            )
            self.events.emit(
                Stage.WATCH_TRANSITION,
                f"WATCH -> EXPIRED: {item['symbol']} {item['structure']} — {reason}",
                symbol=item["symbol"],
                payload={
                    "key": item["key"],
                    "transition": "WATCH_TO_EXPIRED",
                    "reason": reason,
                    "cycles_seen": item["cycles_seen"],
                },
            )
            expired.append(item)
        return expired

    def open_items(self) -> list[dict]:
        return self.store.open_watch_items()

    def is_watched(self, symbol: str, structure: str, expiry: str) -> bool:
        item = self.store.get_watch_item(watch_key(symbol, structure, expiry))
        return bool(item and item["status"] == WATCH)


__all__ = [
    "EXPIRED",
    "PROMOTED",
    "REJECT",
    "TRADE",
    "WATCH",
    "WatchRegistry",
    "watch_key",
]
