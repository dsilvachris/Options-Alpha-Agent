"""
Broker-truth reconciliation.

The local SQLite store is a cache, not the record. Alpaca is the source of truth
for what is actually open. Any divergence between the two is a correctness bug
with money attached: a broker position the store does not know about is never
managed by the exit rules — no profit target, no stop, no time exit.

This module runs at agent startup and at the top of every scan cycle, before any
new opportunity is evaluated, and repairs both directions of divergence:

  broker has it, store does not  ->  ADOPT: rebuild the position row from the
                                     originating order so exit rules manage it.
  store has it, broker does not  ->  CLOSE: mark RECONCILED_MISSING.

Every repair is emitted as its own event so the dashboard shows it.

DRY_RUN positions are exempt from the second rule: they are simulated and the
broker will never hold them, so closing them as missing would destroy the
dry-run exit testing path.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import bootstrap  # noqa: F401  - must precede `logging.*` submodule imports
from decision.matrix import (
    BEAR_CALL_CREDIT_SPREAD,
    BULL_PUT_CREDIT_SPREAD,
    IRON_CONDOR,
)
from decision.structure import CONTRACT_MULTIPLIER
from logging.events import EventLog, Stage
from logging.store import Store
from perception.market import MarketData
from perception.mcp_client import AlpacaMCP, MCPError
from perception.normalize import as_obj, iter_records, occ_parse, pick, to_float, to_int

RECONCILED_MISSING = "RECONCILED_MISSING"
RECONCILED_ADOPTED = "RECONCILED_ADOPTED"
#: Exit reason for a simulated position retired by `cli.py clear-simulated`.
DRY_RUN_CLEARED = "DRY_RUN_CLEARED"


class SimulatedPositionsPresent(RuntimeError):
    """
    Raised when the agent is asked to run live while simulated positions are open.

    A DRY_RUN position occupies a real portfolio slot: check 9 (portfolio fit)
    and the risk gate both count it, so leaving simulated positions in the store
    would silently refuse every real trade with an exposure limit that is not
    actually there.
    """

    def __init__(self, positions: list[dict]) -> None:
        self.positions = positions
        super().__init__(
            f"{len(positions)} simulated (DRY_RUN) position(s) are still open "
            "while DRY_RUN=false"
        )


def open_simulated_positions(store: Store) -> list[dict]:
    """Open positions that were never actually transmitted to the broker."""
    return [p for p in store.open_positions() if p.get("dry_run")]


def assert_no_simulated_positions(store: Store, dry_run: bool) -> None:
    """Refuse to start live while simulated positions hold portfolio slots."""
    if dry_run:
        return
    blocking = open_simulated_positions(store)
    if blocking:
        raise SimulatedPositionsPresent(blocking)


def clear_simulated_positions(store: Store, events: EventLog) -> list[dict]:
    """
    Close every open DRY_RUN position with reason DRY_RUN_CLEARED.

    Only the position rows are retired. The decisions that produced them and the
    events recorded along the way are left untouched: they are the record of what
    the agent decided, and a dry run's reasoning is still worth reading.

    realized_pnl is left NULL rather than set to 0 — a simulated position never
    realized anything, and writing a number would imply it did.
    """
    cleared: list[dict] = []
    for position in open_simulated_positions(store):
        store.close_position(position["position_key"], DRY_RUN_CLEARED, None, None)
        cleared.append({
            "symbol": position["symbol"],
            "structure": position["structure"],
            "contracts": position["contracts"],
            "credit": position["credit"],
            "expiry": position["expiry"],
            "position_key": position["position_key"],
        })
        events.emit(
            Stage.POSITION_MANAGEMENT,
            f"DRY_RUN CLEARED — {position['symbol']} {position['structure']} "
            f"x{position['contracts']} (notional credit "
            f"${float(position['credit']):.2f}) retired as {DRY_RUN_CLEARED}; "
            "it was never transmitted to the broker",
            symbol=position["symbol"],
            payload={"reconcile": DRY_RUN_CLEARED,
                     "position_key": position["position_key"]},
        )
    # Retire the simulated ORDER rows too. They share a client_order_id with a
    # live submission for the same setup on the same day, so leaving them would
    # make the idempotency probe skip the real order.
    simulated_orders = [o for o in store.orders_today() if o.get("dry_run")]
    for order in simulated_orders:
        store.update_order(order["client_order_id"], status=DRY_RUN_CLEARED,
                           broker_order_id=None, response={"cleared": True})

    if simulated_orders:
        events.emit(
            Stage.EXECUTION,
            f"Cleared {len(simulated_orders)} simulated order row(s) so they "
            "cannot suppress a live submission with the same client_order_id",
            payload={"client_order_ids": [o["client_order_id"] for o in simulated_orders]},
        )

    if cleared or simulated_orders:
        events.emit(
            Stage.POSITION_MANAGEMENT,
            f"Cleared {len(cleared)} simulated position(s) and "
            f"{len(simulated_orders)} simulated order row(s); decisions and "
            "events left intact",
            payload={"cleared": cleared,
                     "cleared_orders": [o["client_order_id"] for o in simulated_orders]},
        )
    return cleared

#: Local order statuses that must never be trusted without asking the broker.
#: A submit that timed out may still have reached Alpaca.
UNTRUSTED_LOCAL_STATUSES = {"FAILED", "SUBMITTING", "UNKNOWN", ""}

#: Broker order statuses that mean the order exists and may hold a position.
LIVE_ORDER_STATUSES = {
    "new", "accepted", "partially_filled", "filled", "pending_new",
    "accepted_for_bidding", "calculated", "held", "replaced",
}


@dataclass
class ReconcileReport:
    adopted: list[dict] = field(default_factory=list)
    closed_missing: list[dict] = field(default_factory=list)
    matched: int = 0
    broker_option_legs: int = 0
    skipped_dry_run: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.adopted or self.closed_missing)

    def to_dict(self) -> dict:
        return {
            "adopted": self.adopted,
            "closed_missing": self.closed_missing,
            "matched": self.matched,
            "broker_option_legs": self.broker_option_legs,
            "skipped_dry_run": self.skipped_dry_run,
            "errors": self.errors,
        }

    def render(self) -> str:
        lines = [
            f"broker option legs: {self.broker_option_legs}",
            f"positions matched:  {self.matched}",
            f"adopted:            {len(self.adopted)}",
            f"closed as missing:  {len(self.closed_missing)}",
            f"dry-run skipped:    {self.skipped_dry_run}",
        ]
        for a in self.adopted:
            lines.append(f"  ADOPTED {a['symbol']} {a['structure']} x{a['contracts']} "
                         f"credit ${a['credit']:.2f} expiry {a['expiry']}")
        for c in self.closed_missing:
            lines.append(f"  CLOSED  {c['symbol']} {c['structure']} — {RECONCILED_MISSING}")
        for e in self.errors:
            lines.append(f"  ERROR   {e}")
        return "\n".join(lines)


def _loads(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def classify_structure(legs: list[dict]) -> str:
    """Name a structure from its leg geometry."""
    puts = [l for l in legs if l["right"] == "P"]
    calls = [l for l in legs if l["right"] == "C"]
    if len(puts) == 2 and len(calls) == 2:
        return IRON_CONDOR
    if len(puts) == 2 and not calls:
        short = next((l for l in puts if l["side"] == "sell"), None)
        long = next((l for l in puts if l["side"] == "buy"), None)
        if short and long and short["strike"] > long["strike"]:
            return BULL_PUT_CREDIT_SPREAD
    if len(calls) == 2 and not puts:
        short = next((l for l in calls if l["side"] == "sell"), None)
        long = next((l for l in calls if l["side"] == "buy"), None)
        if short and long and short["strike"] < long["strike"]:
            return BEAR_CALL_CREDIT_SPREAD
    return "reconstructed multi-leg position"


def structure_width(legs: list[dict]) -> float:
    """Widest wing, in points. Only one side of a condor can be breached."""
    widths = []
    for right in ("P", "C"):
        side = [l for l in legs if l["right"] == right]
        shorts = [l for l in side if l["side"] == "sell"]
        longs = [l for l in side if l["side"] == "buy"]
        if shorts and longs:
            widths.append(abs(shorts[0]["strike"] - longs[0]["strike"]))
    return max(widths) if widths else 0.0


def legs_from_order(order: dict) -> list[dict]:
    """
    Normalize an Alpaca order (or our stored request) into typed legs.

    Handles both the broker's nested `legs` on an mleg order and the argument
    payload we recorded when submitting.
    """
    raw_legs = pick(order, "legs") or []
    if isinstance(raw_legs, dict):
        raw_legs = [raw_legs]
    out: list[dict] = []
    for leg in raw_legs:
        if not isinstance(leg, dict):
            continue
        symbol = pick(leg, "symbol")
        if not isinstance(symbol, str):
            continue
        parsed = occ_parse(symbol)
        if not parsed:
            continue
        side = str(pick(leg, "side", default="") or "").lower()
        intent = str(pick(leg, "position_intent", default="") or "").lower()
        if side not in ("buy", "sell"):
            side = "sell" if "sell" in intent else "buy"
        out.append({
            "symbol": symbol.upper(),
            "side": side,
            "position_intent": intent or (
                "sell_to_open" if side == "sell" else "buy_to_open"),
            "ratio_qty": to_int(pick(leg, "ratio_qty", "qty"), 1) or 1,
            "strike": parsed["strike"],
            "right": parsed["right"],
            "expiry": parsed["expiry"],
            "underlying": parsed["underlying"],
            "filled_avg_price": to_float(
                pick(leg, "filled_avg_price", "filled_avg_price_per_contract")),
        })
    return out


class Reconciler:
    """Repairs divergence between the local store and the broker."""

    def __init__(self, mcp: AlpacaMCP, market: MarketData, store: Store,
                 events: EventLog) -> None:
        self.mcp = mcp
        self.market = market
        self.store = store
        self.events = events

    # -- broker state ------------------------------------------------------
    async def broker_option_legs(self) -> dict[str, dict]:
        """
        Open option positions at the broker, keyed by OCC symbol.

        Reads through `self.mcp` rather than `self.market` so positions and
        orders are always sourced from the same transport — otherwise the two
        halves of reconciliation could describe different brokers.
        """
        out: dict[str, dict] = {}
        for record in iter_records(await self.mcp.call("get_all_positions")):
            symbol = pick(record, "symbol", "asset_id")
            if not isinstance(symbol, str):
                continue
            parsed = occ_parse(symbol)
            if not parsed:
                continue  # equity/crypto position, not ours
            qty = to_float(pick(record, "qty", "quantity"), 0.0) or 0.0
            if qty == 0:
                continue
            side = str(pick(record, "side", default="") or "").lower()
            if not side:
                side = "short" if qty < 0 else "long"
            out[symbol.upper()] = {
                "symbol": symbol.upper(),
                "qty": qty,
                "side": "sell" if (qty < 0 or side == "short") else "buy",
                "avg_entry_price": to_float(
                    pick(record, "avg_entry_price", "average_entry_price")),
                **parsed,
            }
        return out

    async def recent_orders(self, limit: int = 200) -> list[dict]:
        """Recent orders, nested so multi-leg legs come back with them."""
        try:
            raw = await self.mcp.call(
                "get_orders",
                {"status": "all", "limit": limit, "nested": True,
                 "direction": "desc", "asset_class": ["us_option"]},
            )
        except MCPError:
            # asset_class filtering is not accepted by every server build.
            try:
                raw = await self.mcp.call(
                    "get_orders",
                    {"status": "all", "limit": limit, "nested": True,
                     "direction": "desc"},
                )
            except MCPError as exc:
                self.events.error(f"Reconcile: could not list orders — {exc}")
                return []
        return iter_records(raw)

    # -- reconstruction ----------------------------------------------------
    def reconstruct(self, legs: list[dict], contracts: int,
                    net_credit_per_contract: float | None) -> dict | None:
        """Build the fields a position row needs from its legs."""
        if not legs:
            return None
        underlying = legs[0]["underlying"]
        expiry = legs[0]["expiry"]
        structure = classify_structure(legs)
        width = structure_width(legs)

        credit_per_contract = net_credit_per_contract
        if credit_per_contract is None:
            filled = [l for l in legs if l.get("filled_avg_price") is not None]
            if len(filled) == len(legs):
                net = sum((l["filled_avg_price"] if l["side"] == "sell"
                           else -l["filled_avg_price"]) for l in legs)
                credit_per_contract = net * CONTRACT_MULTIPLIER
        if credit_per_contract is None:
            return None

        credit = round(credit_per_contract * contracts, 2)
        max_loss = round(width * CONTRACT_MULTIPLIER * contracts - credit, 2)
        return {
            "symbol": underlying,
            "structure": structure,
            "expiry": expiry.isoformat(),
            "contracts": contracts,
            "credit": credit,
            "width": width,
            "max_loss": max_loss,
            "legs": [
                {"symbol": l["symbol"], "side": l["side"],
                 "position_intent": l["position_intent"],
                 "ratio_qty": l["ratio_qty"], "strike": l["strike"],
                 "right": l["right"]}
                for l in legs
            ],
        }

    def reconstruct_from_order(self, order: dict,
                               stored_request: dict | None = None) -> dict | None:
        """
        Rebuild a position from the order that opened it.

        Prefers the broker's own leg fills; falls back to the net limit price we
        submitted, which is the credit we asked for.
        """
        legs = legs_from_order(order)
        if not legs and stored_request:
            legs = legs_from_order(stored_request)
        if not legs:
            return None

        contracts = to_int(pick(order, "qty", "quantity"), None)
        if contracts is None and stored_request:
            contracts = to_int(pick(stored_request, "qty"), None)
        contracts = contracts or 1

        net = None
        limit_price = pick(order, "limit_price")
        if limit_price is None and stored_request:
            limit_price = pick(stored_request, "limit_price")
        limit_value = to_float(limit_price)
        if limit_value is not None:
            # Positive limit = debit, negative = credit (MCP convention).
            net = -limit_value * CONTRACT_MULTIPLIER
        return self.reconstruct(legs, contracts, net)

    def _position_key_for(self, built: dict) -> str:
        """Structural identity plus its entry sequence, matching orders.py."""
        import hashlib

        leg_part = "+".join(sorted(f"{l['side']}:{l['symbol']}" for l in built["legs"]))
        digest = hashlib.sha1(leg_part.encode()).hexdigest()[:8]
        structural = f"{built['symbol']}|{built['structure']}|{built['expiry']}|{digest}"
        return f"{structural}#{self.store.closed_entry_count(structural)}"

    def adopt(self, built: dict, source: str, entry_order_id: str | None = None) -> dict:
        """Create the missing position row so exit rules manage it."""
        pkey = self._position_key_for(built)
        existing = self.store.query(
            "SELECT * FROM positions WHERE position_key=?", (pkey,))
        if existing:
            return {**built, "position_key": pkey, "already_present": True}

        from risk.rules import build_exit_plan

        class _S:  # minimal shim for build_exit_plan's credit arithmetic
            credit = built["credit"] / max(built["contracts"], 1)

        exit_plan = build_exit_plan(_S(), built["contracts"])
        self.store.add_position(
            position_key=pkey,
            symbol=built["symbol"],
            structure=built["structure"],
            contracts=built["contracts"],
            credit=built["credit"],
            width=built["width"],
            max_loss=built["max_loss"],
            expiry=built["expiry"],
            dry_run=False,  # adopted from the broker: real money
            legs=built["legs"],
            entry_order_id=entry_order_id,
            detail={
                "dry_run": False,
                "reconciled": True,
                "reconcile_source": source,
                "exit_plan": exit_plan.to_dict(),
                "credit_per_contract": built["credit"] / max(built["contracts"], 1),
                "max_loss_per_contract": built["max_loss"] / max(built["contracts"], 1),
            },
        )
        self.events.emit(
            Stage.POSITION_MANAGEMENT,
            f"RECONCILED ADOPTED — {built['symbol']} {built['structure']} "
            f"x{built['contracts']} credit ${built['credit']:.2f} expiry "
            f"{built['expiry']}; broker held this position, the store did not. "
            f"Exit rules now manage it ({exit_plan.describe()})",
            symbol=built["symbol"],
            payload={"reconcile": RECONCILED_ADOPTED, "source": source,
                     "position_key": pkey, **built},
        )
        return {**built, "position_key": pkey, "already_present": False}

    # -- the pass ----------------------------------------------------------
    async def run(self) -> ReconcileReport:
        """
        Full reconciliation. Broker is the source of truth in both directions.

        Runs before any opportunity evaluation so that newly adopted positions
        count toward portfolio limits and are eligible for exit management in
        the same cycle.
        """
        report = ReconcileReport()
        self.events.emit(
            Stage.POSITION_MANAGEMENT,
            "Reconciliation pass started — treating the broker as source of truth",
        )

        try:
            broker_legs = await self.broker_option_legs()
        except MCPError as exc:
            report.errors.append(f"could not read broker positions: {exc}")
            self.events.error(f"Reconcile aborted: {exc}")
            return report
        report.broker_option_legs = len(broker_legs)

        local_positions = self.store.open_positions()
        tracked: set[str] = set()
        for position in local_positions:
            for leg in (_loads(position.get("legs")) or []):
                symbol = leg.get("symbol")
                if isinstance(symbol, str):
                    tracked.add(symbol.upper())

        # ---- direction 1: broker has it, store does not -------------------
        untracked = {s: l for s, l in broker_legs.items() if s not in tracked}
        if untracked:
            orders = await self.recent_orders()
            claimed: set[str] = set()
            for order in orders:
                order_legs = legs_from_order(order)
                if not order_legs:
                    continue
                symbols = {l["symbol"] for l in order_legs}
                if not (symbols & set(untracked)) or symbols & claimed:
                    continue
                status = str(pick(order, "status", default="")).lower()
                if status not in LIVE_ORDER_STATUSES:
                    continue
                coid = pick(order, "client_order_id")
                stored = self.store.get_order(coid) if isinstance(coid, str) else None
                built = self.reconstruct_from_order(
                    order, _loads(stored.get("request")) if stored else None)
                if built is None:
                    report.errors.append(
                        f"could not reconstruct a position from order {coid}")
                    continue
                adopted = self.adopt(built, source=f"order:{coid}",
                                     entry_order_id=coid if isinstance(coid, str) else None)
                if not adopted.get("already_present"):
                    report.adopted.append(adopted)
                claimed |= symbols
                for symbol in symbols:
                    untracked.pop(symbol, None)

            # Anything still untracked has no recoverable order behind it.
            if untracked:
                grouped: dict[tuple[str, date], list[dict]] = {}
                for leg in untracked.values():
                    grouped.setdefault((leg["underlying"], leg["expiry"]), []).append(leg)
                for (underlying, expiry), group in grouped.items():
                    legs = [{
                        "symbol": l["symbol"], "side": l["side"],
                        "position_intent": ("sell_to_open" if l["side"] == "sell"
                                            else "buy_to_open"),
                        "ratio_qty": 1, "strike": l["strike"], "right": l["right"],
                        "expiry": l["expiry"], "underlying": l["underlying"],
                        "filled_avg_price": l.get("avg_entry_price"),
                    } for l in group]
                    contracts = int(max(abs(l["qty"]) for l in group)) or 1
                    built = self.reconstruct(legs, contracts, None)
                    if built is None:
                        report.errors.append(
                            f"broker holds {underlying} {expiry} legs "
                            f"{[l['symbol'] for l in group]} that could not be priced; "
                            "left untracked — MANUAL REVIEW REQUIRED")
                        self.events.error(
                            f"Reconcile: untracked broker legs for {underlying} "
                            f"{expiry} could not be reconstructed",
                            symbol=underlying,
                            payload={"legs": [l["symbol"] for l in group]})
                        continue
                    adopted = self.adopt(built, source="broker-position")
                    if not adopted.get("already_present"):
                        report.adopted.append(adopted)

        # ---- direction 2: store has it, broker does not -------------------
        for position in local_positions:
            detail = _loads(position.get("detail")) or {}
            if position.get("dry_run") or detail.get("dry_run"):
                # Simulated position; the broker will never hold it.
                report.skipped_dry_run += 1
                continue
            legs = _loads(position.get("legs")) or []
            symbols = {l.get("symbol", "").upper() for l in legs if l.get("symbol")}
            if not symbols:
                continue
            if symbols & set(broker_legs):
                report.matched += 1
                continue
            self.store.close_position(
                position["position_key"], RECONCILED_MISSING, None, None)
            report.closed_missing.append({
                "symbol": position["symbol"], "structure": position["structure"],
                "position_key": position["position_key"],
            })
            self.events.emit(
                Stage.POSITION_MANAGEMENT,
                f"RECONCILED MISSING — {position['symbol']} {position['structure']} "
                f"was open in the store but the broker holds none of its legs; "
                f"marked closed as {RECONCILED_MISSING}",
                symbol=position["symbol"],
                payload={"reconcile": RECONCILED_MISSING,
                         "position_key": position["position_key"],
                         "legs": sorted(symbols)},
            )

        self.events.emit(
            Stage.POSITION_MANAGEMENT,
            f"Reconciliation complete — {len(report.adopted)} adopted, "
            f"{len(report.closed_missing)} closed as missing, "
            f"{report.matched} matched, {report.skipped_dry_run} dry-run skipped",
            payload=report.to_dict(),
        )
        return report

    # -- targeted repair used by sync_order_status -------------------------
    async def adopt_order_if_live(self, client_order_id_value: str) -> dict | None:
        """
        Check one client order id at the broker and adopt its position if live.

        Used when a locally FAILED order turns out to have reached Alpaca.
        """
        try:
            raw = await self.mcp.call(
                "get_order_by_client_id",
                {"client_order_id": client_order_id_value},
            )
        except MCPError:
            return None
        order = as_obj(raw)
        if not isinstance(order, dict):
            return None
        status = str(pick(order, "status", default="")).lower()
        if status not in LIVE_ORDER_STATUSES:
            return None

        stored = self.store.get_order(client_order_id_value)
        built = self.reconstruct_from_order(
            order, _loads(stored.get("request")) if stored else None)
        if built is None:
            return None
        adopted = self.adopt(built, source=f"sync:{client_order_id_value}",
                             entry_order_id=client_order_id_value)
        return None if adopted.get("already_present") else adopted
