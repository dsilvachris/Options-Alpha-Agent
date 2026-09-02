"""
Stage 6 — Order placement and management, entirely through Alpaca's MCP server.

No HTTP client is imported here. Orders are submitted with `place_option_order`,
status is read with `get_order_by_client_id` / `get_order_by_id`, and positions
are closed with `place_option_order` (closing legs) or `close_position`.

Safety properties
-----------------
DRY_RUN         Defaults to true. The full loop runs and logs decisions
                normally, but no order is transmitted. Live placement requires
                DRY_RUN=false to be set explicitly.
Idempotency     Every order carries a deterministic client_order_id derived from
                the position identity and intent. Before submitting, the store
                and then the broker are checked for that id, so a retry after a
                timeout can never double-submit.
Risk gate       `open_structure` requires an approved RiskDecision. It refuses to
                place anything the gate did not approve, whatever the score.
Defined risk    Multi-leg orders are submitted as a single `mleg` order so the
                long leg cannot fill without the short leg.
"""
from __future__ import annotations

import bootstrap  # noqa: F401  - must precede `logging.*` submodule imports

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from config import (ENV, EXECUTION, EXPIRY_DAY_CLOSE_TIME, HARD_CLOSE_AT, RISK,
                    now_et, past_expiry_day_close, past_hard_close, today_et)
from decision.structure import CONTRACT_MULTIPLIER, Leg, ProposedStructure
from logging.events import EventLog, Stage
from logging.store import Store
from perception.market import MarketData
from perception.mcp_client import AlpacaMCP, MCPError
from execution.reconcile import (ENTRY_CANCELLED, UNTRUSTED_LOCAL_STATUSES,
                                 WORKING_ORDER_STATUSES)
from perception.normalize import as_obj, iter_records, pick, to_float
from risk.rules import ExitPlan, RiskDecision, trip_circuit_breaker

OPEN_INTENT = "open"
CLOSE_INTENT = "close"

#: Markers in the broker's rejection of a replacement that changes nothing.
#: Alpaca answers 422 "order parameters are not changed" when the replacement
#: carries the limit the order already has.
_NOOP_REQUOTE_MARKERS = ("order parameters are not changed",
                         "parameters are not changed",
                         "order is not changed")


def is_noop_requote(error: BaseException | str) -> bool:
    """
    True when a re-quote was refused because it would change nothing.

    This is not a failure of the order — the order is alive and unchanged at the
    price it already carries — so the ladder must step past it rather than give
    up the remaining, genuinely different prices.
    """
    text = str(error).lower()
    return any(marker in text for marker in _NOOP_REQUOTE_MARKERS)


def structural_key(symbol: str, structure: str, expiry: date, legs: list[Leg]) -> str:
    """
    Identity of a *setup*: the same symbol, structure, expiry and strikes.

    Deliberately excludes the entry sequence, so every round trip through the
    same setup shares this key.
    """
    leg_part = "+".join(sorted(f"{l.side}:{l.symbol}" for l in legs))
    digest = hashlib.sha1(leg_part.encode()).hexdigest()[:8]
    return f"{symbol}|{structure}|{expiry.isoformat()}|{digest}"


def entry_sequence(store: Store, structural: str) -> int:
    """
    Which entry attempt this is for a setup: 0 for the first, 1 after one
    completed round trip, and so on.

    The sequence advances ONLY when a previous entry has closed. That is what
    separates the two cases that must behave differently:

      re-entry  A morning position closed at its profit target and the same
                setup qualifies again in the afternoon. One closed row exists,
                so the sequence advances and the afternoon order gets a fresh
                client_order_id and its own position row.

      retry     A submission timed out, or the same still-open setup is proposed
                again next cycle. Nothing has closed, so the sequence is
                unchanged, the client_order_id is identical, and the order
                collides — locally and at Alpaca — instead of double-submitting.
    """
    return store.closed_entry_count(structural)


def position_key(symbol: str, structure: str, expiry: date, legs: list[Leg],
                 sequence: int = 0) -> str:
    """
    Identity of one *entry* into a setup: structural identity plus its sequence.

    Unique per round trip, so a re-entry gets its own row in `positions` (whose
    position_key column is UNIQUE) and its own client_order_id.
    """
    return f"{structural_key(symbol, structure, expiry, legs)}#{sequence}"


def client_order_id(position_key_value: str, intent: str, attempt_date: date) -> str:
    """
    Deterministic idempotency key.

    Alpaca rejects a duplicate client_order_id, so a retry after a timeout
    resolves to the original order rather than a second position. Scoped by date
    so the same setup may be re-entered on a later session.
    """
    digest = hashlib.sha1(
        f"{position_key_value}|{intent}|{attempt_date.isoformat()}".encode()
    ).hexdigest()[:20]
    return f"oaa-{intent}-{digest}"


@dataclass
class OrderResult:
    submitted: bool
    dry_run: bool
    client_order_id: str
    broker_order_id: str | None = None
    status: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class Executor:
    """Places and manages orders. Every Alpaca interaction is an MCP tool call."""

    def __init__(
        self,
        mcp: AlpacaMCP,
        market: MarketData,
        store: Store,
        events: EventLog,
        market_open: bool | None = None,
    ) -> None:
        self.mcp = mcp
        self.market = market
        self.store = store
        self.events = events
        #: Market session state for this cycle. False blocks ALL submission —
        #: entries and exits alike. The risk gate already refuses entries out of
        #: session, but exits bypass the risk gate entirely, so the block lives
        #: here as well: a stop firing overnight off a stale mark must not queue
        #: an order into a closed book.
        #: None means "not supplied" and does not block; the agent sets it every
        #: cycle from get_clock.
        self.market_open = market_open

    def set_market_open(self, market_open: bool | None) -> None:
        self.market_open = market_open

    def _blocked_by_session(self, what: str, symbol: str | None = None) -> bool:
        """True when the market is known to be closed, logging the block."""
        if self.market_open is not False:
            return False
        self.events.emit(
            Stage.EXECUTION,
            f"MARKET CLOSED — {what} not submitted; it would queue into a closed "
            "book and fill at the next open at an unevaluated price",
            symbol=symbol,
            payload={"blocked": "MARKET_CLOSED", "what": what},
        )
        return True

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _limit_price(net_dollars: float, contracts: int) -> str:
        """
        Net limit price for a multi-leg order.

        The MCP tool takes a per-strategy net price where positive is a debit and
        negative is a credit, so a collected credit is submitted as a negative
        number.
        """
        per_contract = net_dollars / CONTRACT_MULTIPLIER
        return f"{-per_contract:.2f}"

    def requote_ladder(self, structure: ProposedStructure) -> list[float]:
        """
        Net prices to walk through after the initial mid-priced order.

        Entries are submitted at the MID of the net spread price — the bid-ask
        is never crossed on entry. If that does not fill, the limit is walked
        linearly from the mid toward the crossing price in
        `EXECUTION.requote_attempts` equal steps. The crossing price is the
        floor and is never exceeded, so the agent concedes at most the spread.
        """
        attempts = max(0, EXECUTION.requote_attempts)
        if attempts == 0 or not EXECUTION.requote_walk_to_cross:
            return []
        mid, cross = structure.credit, structure.credit_at_cross
        if mid <= cross:
            return []
        step = (mid - cross) / attempts
        return self._distinct_steps(
            mid, [round(mid - step * k, 2) for k in range(1, attempts + 1)])

    @classmethod
    def _distinct_steps(cls, mid: float, candidates: list[float]) -> list[float]:
        """
        Drop ladder steps that would transmit a limit price already resting.

        Net prices are carried in dollars per spread but transmitted as a
        per-share limit rounded to the cent, so every step narrower than $1.00
        of net collapses onto the limit before it. Replacing an order with the
        limit it already carries is not an order at all: Alpaca rejects it 422
        "order parameters are not changed", which cost a whole ladder its
        re-quotes.

        The LAST member of each run of equal limits is kept, not the first, so
        the walk still ends exactly on the crossing price instead of a cent
        above it. Steps that collapse onto the resting mid are dropped outright.
        """
        resting = cls._limit_price(mid, 1)
        limits = [cls._limit_price(price, 1) for price in candidates]
        out: list[float] = []
        for index, (price, limit) in enumerate(zip(candidates, limits)):
            if limit == resting:
                continue  # a no-op against the order already working
            if index + 1 < len(limits) and limits[index + 1] == limit:
                continue  # a later, better-priced step transmits this same limit
            out.append(price)
        return out

    async def _order_status(self, order_id: str) -> str:
        try:
            raw = await self.mcp.call("get_order_by_id", {"order_id": order_id})
        except MCPError:
            return "unknown"
        obj = as_obj(raw)
        return str(pick(obj, "status", default="unknown")) if isinstance(obj, dict) else "unknown"

    async def manage_fill(
        self,
        structure: ProposedStructure,
        contracts: int,
        broker_order_id: str | None,
        coid: str,
    ) -> dict:
        """
        Wait for a fill, re-quoting toward the crossing price if needed.

        Every re-quote is logged as an EXECUTION event with the old and new net
        price, so the fill path is auditable alongside the decision itself.
        """
        ladder = self.requote_ladder(structure)
        result = {"filled": False, "requotes": 0, "final_price": structure.credit,
                  "status": "unknown", "ladder": ladder}
        if broker_order_id is None:
            return result

        order_id = broker_order_id
        terminal = {"filled", "canceled", "expired", "rejected", "done_for_day"}

        transmitted = 0
        for attempt, price in enumerate([None] + ladder):
            if price is not None:
                new_limit = self._limit_price(price, 1)
                try:
                    raw = await self.mcp.call(
                        "replace_order_by_id",
                        {"order_id": order_id, "limit_price": new_limit},
                    )
                except MCPError as exc:
                    if is_noop_requote(exc):
                        # The resting order already carries this limit, so there
                        # is nothing to replace. Step to the next distinct price
                        # instead of abandoning the rest of the ladder — the
                        # walk toward the cross is what gets this order filled.
                        self.events.emit(
                            Stage.EXECUTION,
                            f"Re-quote {attempt}/{len(ladder)} skipped for "
                            f"{structure.symbol}: limit {new_limit} is already "
                            f"working (broker refused the replacement as "
                            f"unchanged); stepping to the next distinct price",
                            symbol=structure.symbol,
                            payload={"attempt": attempt, "of": len(ladder),
                                     "limit_price": new_limit,
                                     "skipped": "NO_OP_REQUOTE",
                                     "client_order_id": coid,
                                     "order_id": order_id},
                        )
                        continue
                    self.events.error(
                        f"Re-quote {attempt}/{len(ladder)} failed for "
                        f"{structure.symbol}: {exc}",
                        symbol=structure.symbol,
                    )
                    break
                obj = as_obj(raw)
                replaced_id = pick(obj, "id", "order_id") if isinstance(obj, dict) else None
                self.events.emit(
                    Stage.EXECUTION,
                    f"RE-QUOTE {attempt}/{len(ladder)} — {structure.symbol} "
                    f"{structure.structure}: net credit "
                    f"${result['final_price']:.2f} -> ${price:.2f} "
                    f"(mid ${structure.credit:.2f}, cross floor "
                    f"${structure.credit_at_cross:.2f})",
                    symbol=structure.symbol,
                    payload={
                        "attempt": attempt, "of": len(ladder),
                        "previous_net": result["final_price"], "new_net": price,
                        "limit_price": new_limit,
                        "mid": structure.credit,
                        "cross_floor": structure.credit_at_cross,
                        "client_order_id": coid,
                        "order_id": replaced_id or order_id,
                    },
                )
                transmitted += 1
                result["requotes"] = transmitted
                result["final_price"] = price
                if replaced_id:
                    order_id = str(replaced_id)

            await asyncio.sleep(EXECUTION.requote_wait_seconds)
            status = await self._order_status(order_id)
            result["status"] = status
            if status.lower() == "filled":
                result["filled"] = True
                self.events.emit(
                    Stage.EXECUTION,
                    f"FILLED — {structure.symbol} {structure.structure} at net "
                    f"${result['final_price']:.2f} after {result['requotes']} re-quote(s)",
                    symbol=structure.symbol, payload=result,
                )
                break
            if status.lower() in terminal:
                break

        if not result["filled"]:
            self.events.emit(
                Stage.EXECUTION,
                f"NO FILL — {structure.symbol} {structure.structure} after "
                f"{result['requotes']} re-quote(s); last status {result['status']}",
                symbol=structure.symbol, payload=result,
            )
        self.store.update_order(coid, status=result["status"],
                                broker_order_id=order_id, response=result)
        return result

    async def _existing_order(self, coid: str) -> dict | None:
        """
        Idempotency probe: local store first, then the broker.

        A lookup failure is treated as "no such order", because "order not found"
        is the normal response for a fresh client_order_id and the MCP server
        surfaces it as an error rather than an empty result. This is safe: the
        local store is checked first, and Alpaca enforces client_order_id
        uniqueness server-side, so a duplicate submission is rejected by the
        broker even if this probe is wrong.
        """
        local = self.store.get_order(coid)

        # A DRY_RUN order was never transmitted, so it cannot be a duplicate of a
        # live submission. client_order_id is derived from
        # position_key|intent|date, so a dry-run scan and a live run on the same
        # day produce the SAME id — without this, every setup already dry-run
        # scanned today would be silently skipped for the rest of the session.
        if local is not None and local.get("dry_run") and not ENV.dry_run:
            self.events.emit(
                Stage.EXECUTION,
                f"Ignoring DRY_RUN order row {coid} while running live — it was "
                "never transmitted, so it is not a duplicate",
                symbol=local.get("symbol"),
                payload={"client_order_id": coid, "local_status": local.get("status")},
            )
            local = None

        local_status = str((local or {}).get("status") or "").upper()

        # A local FAILED/SUBMITTING row means the *response* was lost, not that
        # the order was refused: the submit may well have reached Alpaca. Trusting
        # it would abandon a live position, so the broker is asked first.
        if local is not None and local_status not in UNTRUSTED_LOCAL_STATUSES:
            return local

        try:
            raw = await self.mcp.call("get_order_by_client_id", {"client_order_id": coid})
        except Exception:  # noqa: BLE001 - not-found is the normal path
            # Broker could not confirm. If we have no local row at all, treat as
            # new. If the local row is FAILED, allow the retry — Alpaca enforces
            # client_order_id uniqueness server-side, so a duplicate is refused
            # there rather than becoming a second position.
            return None
        obj = as_obj(raw)
        if isinstance(obj, dict) and pick(obj, "id", "order_id", "client_order_id"):
            status = str(pick(obj, "status", default="unknown"))
            if local is not None and local_status in UNTRUSTED_LOCAL_STATUSES:
                # The order did land. Repair the local record and adopt the
                # position so the exit rules pick it up.
                self.store.update_order(
                    coid, status=status,
                    broker_order_id=pick(obj, "id", "order_id"), response=obj)
                self.events.emit(
                    Stage.EXECUTION,
                    f"RECONCILED ORDER — {coid} was locally {local_status} but the "
                    f"broker holds it with status {status}; local record repaired",
                    symbol=(local or {}).get("symbol"),
                    payload={"client_order_id": coid, "local_status": local_status,
                             "broker_status": status},
                )
                await self._adopt_position_for(coid)
            return {
                "client_order_id": coid,
                "broker_order_id": pick(obj, "id", "order_id"),
                "status": status,
                "response": obj,
            }
        return None

    async def _adopt_position_for(self, coid: str) -> None:
        """Create the position row for an order that turned out to be live."""
        from execution.reconcile import Reconciler

        try:
            adopted = await Reconciler(
                self.mcp, self.market, self.store, self.events
            ).adopt_order_if_live(coid)
        except Exception as exc:  # noqa: BLE001 - reconciliation must not raise here
            self.events.error(f"Could not adopt position for {coid}: {exc}")
            return
        if adopted is None:
            return

    # -- opening -----------------------------------------------------------
    async def open_structure(
        self,
        structure: ProposedStructure,
        risk: RiskDecision,
        *,
        today: date | None = None,
    ) -> OrderResult:
        """
        Submit the entry order for an approved structure.

        Refuses outright if the Risk Gate did not approve, or if the daily
        circuit breaker has tripped.
        """
        today = today or today_et()
        structural = structural_key(
            structure.symbol, structure.structure, structure.expiry, structure.legs)
        sequence = entry_sequence(self.store, structural)
        pkey = f"{structural}#{sequence}"
        coid = client_order_id(pkey, OPEN_INTENT, today)

        if self._blocked_by_session(
                f"entry for {structure.symbol} {structure.structure}", structure.symbol):
            return OrderResult(False, ENV.dry_run, coid, status="MARKET_CLOSED",
                               error="market closed")

        if not risk.approved or risk.contracts < 1:
            self.events.emit(
                Stage.EXECUTION,
                f"Order refused for {structure.symbol}: risk gate did not approve",
                symbol=structure.symbol,
                payload={"reasons": risk.reasons},
            )
            return OrderResult(False, ENV.dry_run, coid, error="risk gate did not approve")

        # Idempotency: never double-submit.
        existing = await self._existing_order(coid)
        if existing is not None:
            self.events.emit(
                Stage.EXECUTION,
                f"Idempotent skip for {structure.symbol}: order {coid} already exists "
                f"(status {existing.get('status')})",
                symbol=structure.symbol,
                payload={"client_order_id": coid, "existing": existing.get("status")},
            )
            return OrderResult(
                False, ENV.dry_run, coid,
                broker_order_id=existing.get("broker_order_id"),
                status=str(existing.get("status") or "existing"),
                detail={"idempotent_skip": True},
            )

        legs = [leg.to_mcp_leg(closing=False) for leg in structure.legs]
        arguments: dict[str, Any] = {
            "qty": str(risk.contracts),
            "type": "limit",
            "time_in_force": "day",
            "order_class": "mleg",
            # Entry is priced at the MID of the net spread, never crossing.
            "limit_price": self._limit_price(structure.credit, 1),
            "client_order_id": coid,
            "legs": legs,
        }

        attempts = self.store.increment_order_attempts()
        if attempts > RISK.max_order_attempts_per_day:
            reason = (
                f"daily order attempt limit reached "
                f"({attempts}/{RISK.max_order_attempts_per_day})"
            )
            trip_circuit_breaker(self.store, self.events, reason)
            return OrderResult(False, ENV.dry_run, coid, error=reason)

        if ENV.dry_run:
            ladder = self.requote_ladder(structure)
            self.store.record_order(
                coid, symbol=structure.symbol, intent=OPEN_INTENT, dry_run=True,
                status="DRY_RUN", request=arguments,
            )
            self._record_position(structure, risk, pkey, coid, dry_run=True)
            self.events.emit(
                Stage.EXECUTION,
                f"DRY_RUN — would place {structure.describe()} x{risk.contracts} "
                f"at mid for ${structure.credit * risk.contracts:.2f} credit; "
                f"re-quote ladder (net/contract): "
                f"{[f'${p:.2f}' for p in ladder] or 'none'} "
                f"down to cross floor ${structure.credit_at_cross:.2f}",
                symbol=structure.symbol,
                payload={"client_order_id": coid, "arguments": arguments,
                         "dry_run": True, "mid": structure.credit,
                         "requote_ladder": ladder,
                         "cross_floor": structure.credit_at_cross},
            )
            return OrderResult(False, True, coid, status="DRY_RUN", detail={"arguments": arguments})

        self.store.record_order(
            coid, symbol=structure.symbol, intent=OPEN_INTENT, dry_run=False,
            status="SUBMITTING", request=arguments,
        )
        try:
            raw = await self.mcp.call("place_option_order", arguments)
        except MCPError as exc:
            self.store.update_order(coid, status="FAILED", response={"error": str(exc)})
            self.events.error(
                f"Order placement failed for {structure.symbol}: {exc}",
                symbol=structure.symbol,
                payload={"client_order_id": coid},
            )
            return OrderResult(False, False, coid, error=str(exc))

        obj = as_obj(raw)
        broker_id = pick(obj, "id", "order_id") if isinstance(obj, dict) else None
        status = str(pick(obj, "status", default="submitted")) if isinstance(obj, dict) else "submitted"
        self.store.update_order(coid, status=status, broker_order_id=broker_id, response=obj)
        self._record_position(structure, risk, pkey, coid, dry_run=False)

        fill = await self.manage_fill(structure, risk.contracts, broker_id, coid)
        self.events.emit(
            Stage.EXECUTION,
            f"ORDER PLACED — {structure.describe()} x{risk.contracts} at mid, "
            f"credit ${structure.credit * risk.contracts:.2f}, status {status}, "
            f"{fill['requotes']} re-quote(s), filled={fill['filled']}",
            symbol=structure.symbol,
            payload={
                "client_order_id": coid,
                "broker_order_id": broker_id,
                "status": status,
                "arguments": arguments,
                "fill": fill,
            },
        )
        return OrderResult(True, False, coid, broker_order_id=broker_id, status=status,
                           detail={"arguments": arguments, "fill": fill})

    def _record_position(
        self,
        structure: ProposedStructure,
        risk: RiskDecision,
        pkey: str,
        coid: str,
        dry_run: bool,
    ) -> None:
        self.store.add_position(
            position_key=pkey,
            symbol=structure.symbol,
            structure=structure.structure,
            contracts=risk.contracts,
            credit=round(structure.credit * risk.contracts, 2),
            width=structure.width,
            max_loss=round(risk.max_loss_total, 2),
            expiry=structure.expiry.isoformat(),
            dry_run=dry_run,
            legs=[
                {
                    "symbol": l.symbol,
                    "side": l.side,
                    "position_intent": l.position_intent,
                    "ratio_qty": l.ratio_qty,
                    "strike": l.contract.strike,
                    "right": l.contract.right,
                }
                for l in structure.legs
            ],
            entry_order_id=coid,
            detail={
                "dry_run": dry_run,
                "breakevens": structure.breakevens,
                "exit_plan": risk.exit_plan.to_dict() if risk.exit_plan else None,
                "credit_per_contract": structure.credit,
                "max_loss_per_contract": structure.max_loss,
            },
        )

    # -- exits -------------------------------------------------------------
    async def cost_to_close(self, position: dict) -> float | None:
        """
        Current cost to close a recorded position, in dollars.

        Buying back the shorts and selling the longs at the current mid.
        """
        legs = position.get("legs")
        if isinstance(legs, str):
            import json

            try:
                legs = json.loads(legs)
            except (TypeError, ValueError):
                return None
        if not legs:
            return None

        symbols = [l["symbol"] for l in legs]
        snapshot = await self.market.option_snapshot(symbols)
        total = 0.0
        for leg in legs:
            contract = snapshot.get(leg["symbol"].upper())
            if contract is None or contract.mid is None:
                return None
            # Closing a short costs the mid; closing a long returns the mid.
            total += contract.mid if leg["side"] == "sell" else -contract.mid
        return total * CONTRACT_MULTIPLIER * int(position["contracts"])

    def exit_trigger(
        self,
        position: dict,
        cost: float | None,
        today: date | None = None,
        now: datetime | None = None,
    ) -> tuple[str | None, dict]:
        """
        Section 3.4 exit discipline. Returns (reason, measurements).

        Profit target: cost to close has fallen to the configured fraction of
        the credit collected. Stop loss: cost to close has risen to the
        configured multiple. Time exit: expiry is at or inside the DTE floor.
        """
        # `now` is injectable so the exit rules can be exercised at a chosen
        # instant by the self-test harness without patching module globals.
        now = now or now_et()
        today = today or now.date()
        credit = float(position["credit"])
        measures: dict[str, Any] = {"credit": credit, "cost_to_close": cost}

        # End-of-window deadline outranks every other exit rule, including P&L.
        if past_hard_close(now):
            measures["hard_close_at"] = HARD_CLOSE_AT.isoformat()
            return (
                f"hard deadline: end-of-window force-close at "
                f"{HARD_CLOSE_AT:%Y-%m-%d %H:%M %Z} reached — closing regardless of P&L",
                measures,
            )

        expiry = position.get("expiry")

        # Expiry-day flatten. These are physically-settled ETF options, so a
        # short leg held through expiry can be assigned. Checked ahead of the
        # profit target and stop: assignment risk is not a P&L question.
        if expiry and past_expiry_day_close(str(expiry), now):
            measures["expiry_day_close_time"] = EXPIRY_DAY_CLOSE_TIME.isoformat()
            return (
                f"expiry-day close: {EXPIRY_DAY_CLOSE_TIME:%H:%M} ET on the "
                f"{expiry} expiry reached — flattening to avoid assignment",
                measures,
            )

        if expiry:
            try:
                dte = (date.fromisoformat(str(expiry)) - today).days
                measures["dte"] = dte
                if dte <= RISK.time_exit_dte:
                    return (
                        f"time exit: {dte} DTE at or inside the "
                        f"{RISK.time_exit_dte} DTE floor",
                        measures,
                    )
            except ValueError:
                pass

        if cost is None or credit <= 0:
            return None, measures

        target = credit * (1 - RISK.profit_target_pct_of_credit)
        stop = credit * RISK.stop_loss_multiple_of_credit
        measures["profit_target"] = target
        measures["stop_loss"] = stop

        if cost <= target:
            return (
                f"profit target: cost to close ${cost:.2f} at or below "
                f"{RISK.profit_target_pct_of_credit:.0%} of ${credit:.2f} credit "
                f"(${target:.2f})",
                measures,
            )
        if cost >= stop:
            return (
                f"stop loss: cost to close ${cost:.2f} at or above "
                f"{RISK.stop_loss_multiple_of_credit:g}x the ${credit:.2f} credit "
                f"(${stop:.2f})",
                measures,
            )
        return None, measures

    async def close_structure(
        self,
        position: dict,
        reason: str,
        cost: float | None,
        *,
        today: date | None = None,
        force: bool = False,
    ) -> OrderResult:
        """
        Close a recorded position by submitting the reversing multi-leg order.

        Blocked while the market is closed unless `force` is set. Automated exits
        never force; the manual `flatten` command does, because that is a
        deliberate operator action.
        """
        import json

        today = today or today_et()
        pkey = position["position_key"]
        coid = client_order_id(pkey, CLOSE_INTENT, today)

        if not force and self._blocked_by_session(
                f"exit for {position['symbol']} {position['structure']} ({reason})",
                position["symbol"]):
            return OrderResult(False, ENV.dry_run, coid, status="MARKET_CLOSED",
                               error="market closed")

        legs = position.get("legs")
        if isinstance(legs, str):
            try:
                legs = json.loads(legs)
            except (TypeError, ValueError):
                legs = []
        if not legs:
            return OrderResult(False, ENV.dry_run, coid, error="position has no recorded legs")

        mcp_legs = [
            {
                "symbol": leg["symbol"],
                "ratio_qty": str(leg.get("ratio_qty", 1)),
                "side": "buy" if leg["side"] == "sell" else "sell",
                "position_intent": "buy_to_close" if leg["side"] == "sell" else "sell_to_close",
            }
            for leg in legs
        ]
        arguments: dict[str, Any] = {
            "qty": str(int(position["contracts"])),
            "type": "market",
            "time_in_force": "day",
            "order_class": "mleg",
            "client_order_id": coid,
            "legs": mcp_legs,
        }

        credit = float(position["credit"])
        realized = (credit - cost) if cost is not None else None

        existing = await self._existing_order(coid)
        if existing is not None:
            return OrderResult(
                False, ENV.dry_run, coid,
                broker_order_id=existing.get("broker_order_id"),
                status=str(existing.get("status") or "existing"),
                detail={"idempotent_skip": True},
            )

        if ENV.dry_run:
            self.store.record_order(
                coid, symbol=position["symbol"], intent=CLOSE_INTENT, dry_run=True,
                status="DRY_RUN", request=arguments,
            )
            self.store.close_position(pkey, reason, realized, coid)
            self.events.emit(
                Stage.POSITION_MANAGEMENT,
                f"DRY_RUN — would close {position['symbol']} {position['structure']}: {reason}",
                symbol=position["symbol"],
                payload={"client_order_id": coid, "reason": reason,
                         "realized_pnl": realized, "arguments": arguments},
            )
            return OrderResult(False, True, coid, status="DRY_RUN")

        self.store.record_order(
            coid, symbol=position["symbol"], intent=CLOSE_INTENT, dry_run=False,
            status="SUBMITTING", request=arguments,
        )
        try:
            raw = await self.mcp.call("place_option_order", arguments)
        except MCPError as exc:
            self.store.update_order(coid, status="FAILED", response={"error": str(exc)})
            self.events.error(
                f"Close failed for {position['symbol']}: {exc}", symbol=position["symbol"]
            )
            return OrderResult(False, False, coid, error=str(exc))

        obj = as_obj(raw)
        broker_id = pick(obj, "id", "order_id") if isinstance(obj, dict) else None
        status = str(pick(obj, "status", default="submitted")) if isinstance(obj, dict) else "submitted"
        self.store.update_order(coid, status=status, broker_order_id=broker_id, response=obj)
        self.store.close_position(pkey, reason, realized, coid)

        self.events.emit(
            Stage.POSITION_MANAGEMENT,
            f"POSITION CLOSED — {position['symbol']} {position['structure']}: {reason}",
            symbol=position["symbol"],
            payload={
                "client_order_id": coid, "broker_order_id": broker_id,
                "reason": reason, "realized_pnl": realized, "status": status,
            },
        )
        return OrderResult(True, False, coid, broker_order_id=broker_id, status=status)

    # -- flatten -----------------------------------------------------------
    async def flatten_all(self) -> dict:
        """
        End-of-window flatten: close every open position.

        Closes each position the agent recorded (so bookkeeping and realized P&L
        stay correct), then calls close_all_positions to catch anything opened
        outside the agent's own records.
        """
        results: list[dict] = []
        for position in self.store.open_positions():
            cost = await self.cost_to_close(position)
            result = await self.close_structure(
                position, "end-of-window flatten", cost, force=True)
            results.append(
                {
                    "symbol": position["symbol"],
                    "structure": position["structure"],
                    "client_order_id": result.client_order_id,
                    "submitted": result.submitted,
                    "dry_run": result.dry_run,
                    "error": result.error,
                }
            )

        broker: Any = None
        if ENV.dry_run:
            self.events.emit(
                Stage.POSITION_MANAGEMENT,
                f"DRY_RUN — flatten complete for {len(results)} recorded position(s); "
                "close_all_positions not called",
                payload={"results": results, "dry_run": True},
            )
        else:
            try:
                broker = await self.mcp.call("close_all_positions", {"cancel_orders": True})
            except MCPError as exc:
                broker = {"error": str(exc)}
            self.events.emit(
                Stage.POSITION_MANAGEMENT,
                f"FLATTEN — closed {len(results)} recorded position(s) and called "
                "close_all_positions",
                payload={"results": results, "broker": str(broker)[:500]},
            )
        return {"positions": results, "broker": broker, "dry_run": ENV.dry_run}


    # -- end-of-cycle order hygiene ---------------------------------------
    async def cancel_unfilled_entries(self) -> list[dict]:
        """
        Cancel every entry order still working at the end of the scan cycle.

        An unfilled entry that is left resting is a standing instruction priced
        against quotes the agent can no longer see: it can fill hours later, at
        an edge that has since decayed, off a decision nothing re-evaluated.

        It is also a bookkeeping hazard, and that is the sharper one. The
        position row is written at submission, so a never-filled entry leaves a
        row the broker has nothing behind. Reconciliation used to close that row
        as RECONCILED_MISSING while the order was still live, which advances the
        entry sequence — and the next cycle would then pass the idempotency
        probe with a fresh client_order_id and open a SECOND position in the
        same underlying if the original order finally filled.

        So the cycle that places an order owns it. Anything unfilled is
        cancelled here and re-priced against fresh quotes next cycle. Exits are
        deliberately untouched: a working exit is trying to close risk and must
        be left alone.

        Every candidate's status is re-read from the broker immediately before
        the cancel, because a fill landing between the last status read and now
        must not be cancelled out from under its position.
        """
        cancelled: list[dict] = []
        for order in self.store.orders_today():
            if order.get("dry_run") or order.get("intent") != OPEN_INTENT:
                continue
            if str(order.get("status") or "").lower() not in WORKING_ORDER_STATUSES:
                continue

            coid = order["client_order_id"]
            symbol = order.get("symbol")
            status = str(order.get("status") or "").lower()
            broker_id = order.get("broker_order_id")

            confirmed = await self._broker_order(coid)
            if confirmed is not None:
                status = str(pick(confirmed, "status", default=status) or "").lower()
                broker_id = pick(confirmed, "id", "order_id") or broker_id
                self.store.update_order(coid, status=status,
                                        broker_order_id=broker_id, response=confirmed)
            if status not in WORKING_ORDER_STATUSES:
                continue  # filled, already cancelled, or otherwise settled

            if not broker_id:
                self.events.error(
                    f"Entry {coid} is still working but no broker order id was "
                    "recorded, so it cannot be cancelled — MANUAL REVIEW REQUIRED",
                    symbol=symbol, payload={"client_order_id": coid, "status": status},
                )
                continue

            try:
                await self.mcp.call("cancel_order_by_id", {"order_id": str(broker_id)})
            except MCPError as exc:
                self.events.error(
                    f"Could not cancel unfilled entry {coid} for {symbol}: {exc}",
                    symbol=symbol,
                    payload={"client_order_id": coid, "order_id": str(broker_id)},
                )
                continue

            # Alpaca answers a cancel with an empty body, so the outcome is read
            # back rather than assumed: the cancel can still lose a race with a
            # fill, and recording "canceled" for a filled order would abandon a
            # live position.
            after = await self._broker_order(coid)
            final = (str(pick(after, "status", default="pending_cancel") or "").lower()
                     if after is not None else "pending_cancel")
            filled_qty = to_float(pick(after, "filled_qty"), 0.0) if after else 0.0
            self.store.update_order(coid, status=final, broker_order_id=broker_id,
                                    response=after if after is not None else {"cancel_requested": True})

            record = {"client_order_id": coid, "symbol": symbol,
                      "order_id": str(broker_id), "status": final,
                      "filled_qty": filled_qty, "position_closed": False}

            if final == "filled" or (filled_qty or 0) > 0:
                # It filled first. Leave the position row alone; reconciliation
                # owns whatever the broker actually holds.
                self.events.emit(
                    Stage.EXECUTION,
                    f"CANCEL RACED A FILL — {symbol} entry {coid} filled "
                    f"({filled_qty:g} contract(s)) before the cancel landed; the "
                    "position row is kept and reconciliation will confirm it",
                    symbol=symbol, payload=record,
                )
                cancelled.append(record)
                continue

            record["position_closed"] = self._retire_unfilled_position(coid, symbol, final)
            self.events.emit(
                Stage.EXECUTION,
                f"ENTRY CANCELLED — {symbol} entry {coid} was still working at the "
                f"end of the cycle that placed it and never filled; cancelled "
                f"(broker status {final}) so the next cycle re-prices against "
                "fresh quotes",
                symbol=symbol, payload={"cancelled": "UNFILLED_ENTRY", **record},
            )
            cancelled.append(record)

        if cancelled:
            self.events.emit(
                Stage.EXECUTION,
                f"End-of-cycle sweep cancelled {len(cancelled)} unfilled entry "
                "order(s); none were left resting at the broker",
                payload={"cancelled": cancelled},
            )
        return cancelled

    async def _broker_order(self, coid: str) -> dict | None:
        """Current broker record for a client order id, or None if unreadable."""
        try:
            raw = await self.mcp.call("get_order_by_client_id",
                                      {"client_order_id": coid})
        except MCPError:
            return None
        obj = as_obj(raw)
        return obj if isinstance(obj, dict) else None

    def _retire_unfilled_position(self, coid: str, symbol: str | None,
                                  status: str) -> bool:
        """
        Close the position row behind an entry that was cancelled unfilled.

        The row was written at submission for an order that never filled, so
        nothing was ever held and there is no P&L to realize. Closing it here
        rather than leaving it for reconciliation keeps the reason honest
        (ENTRY_CANCELLED, not RECONCILED_MISSING) and lets the entry sequence
        advance so the setup can be re-entered next cycle at a fresh price.
        """
        rows = self.store.query(
            "SELECT * FROM positions WHERE entry_order_id=? AND status='OPEN'",
            (coid,),
        )
        for row in rows:
            self.store.close_position(row["position_key"], ENTRY_CANCELLED, None, None)
            self.events.emit(
                Stage.POSITION_MANAGEMENT,
                f"POSITION RETIRED — {row['symbol']} {row['structure']} was "
                f"recorded when its entry was submitted, but that entry was "
                f"cancelled unfilled ({status}); the row is closed as "
                f"{ENTRY_CANCELLED} with no realized P&L",
                symbol=row["symbol"],
                payload={"position_key": row["position_key"],
                         "client_order_id": coid, "exit_reason": ENTRY_CANCELLED},
            )
        return bool(rows)

    # -- reconciliation ----------------------------------------------------
    async def sync_order_status(self) -> None:
        """Refresh stored status for today's live orders."""
        for order in self.store.orders_today():
            if order.get("dry_run"):
                continue
            if str(order.get("status") or "") in {"canceled", "expired"}:
                continue
            try:
                raw = await self.mcp.call(
                    "get_order_by_client_id", {"client_order_id": order["client_order_id"]}
                )
            except MCPError:
                continue
            obj = as_obj(raw)
            if isinstance(obj, dict):
                previous = str(order.get("status") or "").upper()
                status = str(pick(obj, "status", default=order.get("status")))
                self.store.update_order(
                    order["client_order_id"],
                    status=status,
                    broker_order_id=pick(obj, "id", "order_id") or order.get("broker_order_id"),
                    response=obj,
                )
                # Repairing the status alone would leave an unmanaged position:
                # the order is live at the broker but no position row exists, so
                # no exit rule would ever evaluate it.
                if (previous in UNTRUSTED_LOCAL_STATUSES
                        and order.get("intent") == OPEN_INTENT):
                    self.events.emit(
                        Stage.EXECUTION,
                        f"RECONCILED ORDER — {order['client_order_id']} was locally "
                        f"{previous}, broker reports {status}; adopting position",
                        symbol=order.get("symbol"),
                        payload={"client_order_id": order["client_order_id"],
                                 "local_status": previous, "broker_status": status},
                    )
                    await self._adopt_position_for(order["client_order_id"])
