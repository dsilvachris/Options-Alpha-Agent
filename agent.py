"""
The perception-decision-action loop (Section 2).

Runs the seven stages in order for every underlying on the watchlist, emitting a
real backend event at each stage. Nothing here is simulated and nothing depends
on manual intervention.

    Market Scan -> Market Analysis -> Strategy Selection -> Opportunity
    Evaluation -> Risk Review -> Final Decision -> Execution -> decision card

Position management (exit discipline) runs at the top of every cycle, before new
opportunities are considered, so capital and position slots are freed first.
"""
from __future__ import annotations

import bootstrap  # noqa: F401  - must precede `logging.*` submodule imports

import asyncio
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from config import (ENV, HARD_CLOSE_AT, LOOP, RISK, SCORING, SIGNALS,
                    UNIVERSE, past_hard_close, today_et)
from decision import matrix as strategy_matrix
from decision.structure import ProposedStructure, StructureError, build as build_structure
from evaluation import scorer
from evaluation.states import WatchRegistry, watch_key
from execution.orders import Executor
from execution.reconcile import (ReconcileReport, Reconciler,
                                 assert_no_simulated_positions)
from logging import decision_card
from logging.events import EventLog, Stage
from logging.store import Store, get_store
from monitoring.baseline import BaselineRecorder
from perception.market import MarketData, UnderlyingSnapshot
from perception.mcp_client import AlpacaMCP, MCPError
from risk import rules as risk_rules
from signals import trend as trend_signal
from signals import volatility as vol_signal


@dataclass
class SymbolOutcome:
    """What the loop concluded for one underlying in one cycle."""

    symbol: str
    state: str
    score: int = 0
    structure: str = ""
    reason: str = ""
    card: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class CycleResult:
    cycle_id: str
    reconciliation: ReconcileReport | None = None
    outcomes: list[SymbolOutcome] = field(default_factory=list)
    closed_positions: list[dict] = field(default_factory=list)
    expired_watches: list[dict] = field(default_factory=list)
    #: Entry orders still unfilled at the end of this cycle, cancelled by it.
    cancelled_entries: list[dict] = field(default_factory=list)
    equity: float | None = None
    market_open: bool = True
    halted: bool = False
    error: str | None = None


class Agent:
    """Owns the MCP session and runs scan cycles against it."""

    def __init__(self, store: Store | None = None, echo: bool = False) -> None:
        self.store = store or get_store()
        self.echo = echo
        self.mcp = AlpacaMCP(self.store)
        self.market = MarketData(self.mcp)

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        # Fail fast, before opening a session: a simulated position left in the
        # store occupies a real portfolio slot and would silently block every
        # live trade via check 9 and the risk gate.
        assert_no_simulated_positions(self.store, ENV.dry_run)
        await self.mcp.connect()
        await self.market.verify_tools()
        await self.reconcile()

    async def reconcile(self) -> ReconcileReport:
        """
        Repair store/broker divergence. Broker is the source of truth.

        Run at startup and at the top of every scan cycle, before any new
        opportunity is evaluated, so adopted positions count toward portfolio
        limits and are managed by the exit rules in the same cycle.
        """
        events = EventLog.start(self.store, echo=self.echo)
        self.mcp.bind_events(events)
        return await Reconciler(self.mcp, self.market, self.store, events).run()

    async def stop(self) -> None:
        await self.mcp.disconnect()

    async def __aenter__(self) -> "Agent":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    # -- one cycle ---------------------------------------------------------
    async def run_cycle(self, today: date | None = None) -> CycleResult:
        today = today or today_et()
        events = EventLog.start(self.store, echo=self.echo)
        self.mcp.bind_events(events)

        watches = WatchRegistry(self.store, events)
        executor = Executor(self.mcp, self.market, self.store, events)
        baselines = BaselineRecorder(self.store, events)
        result = CycleResult(cycle_id=events.cycle_id)

        events.emit(
            Stage.CYCLE_START,
            f"Scan cycle started — watchlist {', '.join(UNIVERSE.watchlist)}"
            f"{' [DRY_RUN]' if ENV.dry_run else ' [LIVE]'}",
            payload={"dry_run": ENV.dry_run, "watchlist": list(UNIVERSE.watchlist)},
        )

        try:
            # ---- account state -------------------------------------------
            account = await self.market.account()
            result.equity = account.equity
            self.store.set_day_starting_equity(account.equity)
            self.store.add_equity_sample(account.equity, account.last_equity, events.cycle_id)

            clock = await self.market.clock()
            market_open = bool(clock["is_open"])
            result.market_open = market_open
            # Both gates are armed from the same reading: the risk gate refuses
            # entries, and the executor refuses every submission including exits.
            executor.set_market_open(market_open)
            events.emit(
                Stage.MARKET_SCAN,
                f"Account equity ${account.equity:,.2f}; market "
                f"{'OPEN' if market_open else 'CLOSED'}"
                + ("" if market_open else
                   " — pipeline runs, no order will be submitted"),
                payload={
                    "equity": account.equity,
                    "cash": account.cash,
                    "buying_power": account.buying_power,
                    "options_approved_level": account.options_approved_level,
                    "market_open": clock["is_open"],
                },
            )

            # ---- reconciliation (broker is source of truth) ---------------
            # Runs before exits and before evaluation: an adopted position must
            # be managed and must count toward portfolio limits this cycle.
            result.reconciliation = await Reconciler(
                self.mcp, self.market, self.store, events).run()

            # ---- position management (exit discipline) --------------------
            result.closed_positions = await self._manage_positions(
                executor, events, today, market_open)

            # ---- circuit breaker -----------------------------------------
            halted, halt_reason = risk_rules.circuit_breaker_state(self.store, account.equity)
            if halted:
                risk_rules.trip_circuit_breaker(self.store, events, halt_reason)
                result.halted = True

            # ---- per-underlying pipeline ---------------------------------
            for symbol in UNIVERSE.watchlist:
                try:
                    outcome = await self._evaluate_symbol(
                        symbol, account, executor, watches, baselines, events,
                        today, market_open,
                    )
                except MCPError as exc:
                    events.error(f"{symbol}: MCP failure — {exc}", symbol=symbol)
                    outcome = SymbolOutcome(symbol, "ERROR", reason=str(exc))
                except Exception as exc:  # noqa: BLE001 - one symbol must not kill the cycle
                    events.error(f"{symbol}: {type(exc).__name__} — {exc}", symbol=symbol)
                    outcome = SymbolOutcome(symbol, "ERROR", reason=f"{type(exc).__name__}: {exc}")
                result.outcomes.append(outcome)

            # ---- WATCH expiry sweep --------------------------------------
            result.expired_watches = watches.expire_stale(market_open)

            # ---- baselines -----------------------------------------------
            baselines.emit_summary()

            if not ENV.dry_run:
                await executor.sync_order_status()
                # Last thing in the cycle, after status has been refreshed: no
                # entry order this cycle placed may outlive it. An unfilled
                # entry left resting fills later against quotes nothing
                # re-evaluated, and its position row would be closed as missing
                # while the order is still live — which advances the entry
                # sequence and lets the next cycle open a second position in the
                # same underlying.
                result.cancelled_entries = await executor.cancel_unfilled_entries()

        except Exception as exc:  # noqa: BLE001 - recorded, loop continues next cycle
            result.error = f"{type(exc).__name__}: {exc}"
            events.error(f"Cycle aborted: {result.error}")

        events.emit(
            Stage.CYCLE_END,
            f"Scan cycle complete — "
            + ", ".join(f"{o.symbol}:{o.state}" for o in result.outcomes),
            payload={
                "outcomes": [{"symbol": o.symbol, "state": o.state, "score": o.score}
                             for o in result.outcomes],
                "closed": len(result.closed_positions),
                "expired_watches": len(result.expired_watches),
                "cancelled_entries": len(result.cancelled_entries),
                "halted": result.halted,
            },
        )
        return result

    # -- position management ----------------------------------------------
    async def _manage_positions(
        self, executor: Executor, events: EventLog, today: date,
        market_open: bool = True,
    ) -> list[dict]:
        """
        Apply exit discipline to every open position before scanning.

        Out of session the triggers are still EVALUATED and the intent recorded,
        but no order is submitted — the position is acted on at the next open.
        Evaluating anyway keeps the reasoning trail complete and means a stop
        that fired overnight is visible rather than silently skipped.
        """
        closed: list[dict] = []
        open_positions = self.store.open_positions()
        if not open_positions:
            return closed

        deadline_reached = past_hard_close()
        events.emit(
            Stage.POSITION_MANAGEMENT,
            f"Reviewing {len(open_positions)} open position(s) against exit rules"
            + (f" — END-OF-WINDOW DEADLINE {HARD_CLOSE_AT:%Y-%m-%d %H:%M %Z} REACHED, "
               "force-closing all" if deadline_reached else ""),
            payload={"count": len(open_positions),
                     "hard_close_at": HARD_CLOSE_AT.isoformat(),
                     "deadline_reached": deadline_reached},
        )

        for position in open_positions:
            cost = await executor.cost_to_close(position)
            reason, measures = executor.exit_trigger(position, cost, today)
            if reason is None:
                events.emit(
                    Stage.POSITION_MANAGEMENT,
                    f"{position['symbol']} {position['structure']} held — "
                    f"cost to close "
                    f"{f'${cost:.2f}' if cost is not None else 'unavailable'}, "
                    f"credit ${float(position['credit']):.2f}",
                    symbol=position["symbol"],
                    payload=measures,
                )
                continue

            if not market_open:
                events.emit(
                    Stage.POSITION_MANAGEMENT,
                    f"EXIT DEFERRED — {position['symbol']} {position['structure']} "
                    f"would exit now ({reason}) but the market is closed; "
                    f"it will be acted on at the next open",
                    symbol=position["symbol"],
                    payload={"deferred": "MARKET_CLOSED", "reason": reason,
                             **measures},
                )
                self.store.add_decision(
                    cycle_id=events.cycle_id, symbol=position["symbol"],
                    state="EXIT_DEFERRED", score=None, market_open=False,
                    structure=position["structure"], reason=reason,
                    card=decision_card.exit_card(
                        symbol=position["symbol"], structure=position["structure"],
                        exit_reason=f"{reason} [DEFERRED: market closed]",
                        credit=float(position["credit"]),
                        exit_cost=cost if cost is not None else 0.0,
                        realized_pnl=0.0, contracts=int(position["contracts"])),
                    detail={"measures": measures, "deferred": True},
                )
                continue

            await executor.close_structure(position, reason, cost, today=today)
            credit = float(position["credit"])
            realized = (credit - cost) if cost is not None else 0.0
            card = decision_card.exit_card(
                symbol=position["symbol"],
                structure=position["structure"],
                exit_reason=reason,
                credit=credit,
                exit_cost=cost if cost is not None else 0.0,
                realized_pnl=realized,
                contracts=int(position["contracts"]),
            )
            self.store.add_decision(
                cycle_id=events.cycle_id,
                symbol=position["symbol"],
                state="EXIT",
                market_open=market_open,
                score=None,
                structure=position["structure"],
                reason=reason,
                card=card,
                detail={"measures": measures, "realized_pnl": realized},
            )
            closed.append({**position, "exit_reason": reason, "realized_pnl": realized})
        return closed

    # -- the seven-stage pipeline for one underlying -----------------------
    async def _evaluate_symbol(
        self,
        symbol: str,
        account,
        executor: Executor,
        watches: WatchRegistry,
        baselines: BaselineRecorder,
        events: EventLog,
        today: date,
        market_open: bool = True,
    ) -> SymbolOutcome:
        # ---- Stage 1: Market Scan ----------------------------------------
        events.emit(Stage.MARKET_SCAN, f"Scanning {symbol}", symbol=symbol)
        snapshot: UnderlyingSnapshot = await self.market.snapshot_underlying(symbol, today)
        baselines.record_passive(symbol, snapshot.spot)
        events.emit(
            Stage.MARKET_SCAN,
            f"{symbol} spot ${snapshot.spot:,.2f}; {len(snapshot.contracts)} eligible "
            f"contract(s) across {len(snapshot.expiries)} expiry(ies)",
            symbol=symbol,
            payload={
                "spot": snapshot.spot,
                "contracts": len(snapshot.contracts),
                "expiries": [e.isoformat() for e in snapshot.expiries],
                "bars": len(snapshot.closes),
            },
        )

        # ---- Stage 2: Market Analysis ------------------------------------
        priors = self.store.recent_atm_iv(
            symbol, today.isoformat(), SIGNALS.iv_average_window
        )
        vol = vol_signal.evaluate(snapshot.closes, snapshot.contracts, snapshot.spot, priors)
        trend = trend_signal.evaluate(snapshot.closes, snapshot.spot)
        if vol.atm_iv is not None:
            self.store.record_atm_iv(symbol, today.isoformat(), vol.atm_iv)

        events.emit(
            Stage.MARKET_ANALYSIS,
            f"{symbol}: {vol.condition} (ATM IV "
            f"{vol.atm_iv if vol.atm_iv is None else round(vol.atm_iv, 4)} vs RV "
            f"{vol.realized_vol if vol.realized_vol is None else round(vol.realized_vol, 4)}), "
            f"{trend.condition} (MA separation "
            f"{trend.separation if trend.separation is None else round(trend.separation, 4)})",
            symbol=symbol,
            payload={
                "iv_condition": vol.condition,
                "atm_iv": vol.atm_iv,
                "realized_vol": vol.realized_vol,
                "iv_rv_ratio": vol.iv_rv_ratio,
                "iv_average": vol.iv_average,
                "iv_change_ratio": vol.iv_change_ratio,
                "trend_condition": trend.condition,
                "ma_short": trend.ma_short,
                "ma_long": trend.ma_long,
                "separation": trend.separation,
            },
        )

        # ---- Stage 3: Strategy Selection ---------------------------------
        selection = strategy_matrix.select(vol, trend)
        events.emit(
            Stage.STRATEGY_SELECTION,
            f"{symbol}: {selection.structure} — {selection.rationale}",
            symbol=symbol,
            payload={
                "structure": selection.structure,
                "eligible": selection.eligible,
                "bias": selection.bias,
                "rationale": selection.rationale,
            },
        )

        if not selection.eligible:
            return self._record_no_structure(symbol, vol, trend, selection, events)

        # ---- Build the concrete structure --------------------------------
        try:
            structure = build_structure(selection, snapshot.contracts, snapshot.spot, today)
        except StructureError as exc:
            reason = f"no compliant structure available: {exc}"
            card = decision_card.declined_card(
                symbol=symbol, iv_condition=vol.condition, trend_condition=trend.condition,
                structure=selection.structure, score=0, reason=reason,
            )
            events.emit(Stage.FINAL_DECISION, f"{symbol}: REJECT — {reason}",
                        symbol=symbol, payload={"reason": reason})
            self.store.add_decision(
                cycle_id=events.cycle_id, symbol=symbol, market_open=market_open, state="REJECT", score=0,
                structure=selection.structure, iv_condition=vol.condition,
                trend_condition=trend.condition, reason=reason, card=card,
                detail={"stage": "structure_selection"},
            )
            return SymbolOutcome(symbol, "REJECT", 0, selection.structure, reason, card)

        events.emit(
            Stage.STRATEGY_SELECTION,
            f"{symbol}: {structure.describe()} — credit ${structure.credit:.2f}, "
            f"width ${structure.width * 100:.2f}, max loss ${structure.max_loss:.2f}",
            symbol=symbol,
            payload={
                "structure": structure.structure,
                "expiry": structure.expiry.isoformat(),
                "dte": structure.dte,
                "credit": structure.credit,
                "width": structure.width,
                "max_loss": structure.max_loss,
                "breakevens": structure.breakevens,
                "legs": [
                    {"symbol": l.symbol, "side": l.side, "strike": l.contract.strike,
                     "right": l.contract.right, "delta": l.contract.delta,
                     "open_interest": l.contract.open_interest}
                    for l in structure.legs
                ],
            },
        )

        # Section 8.3 unfiltered baseline: every matched setup, score ignored.
        # ---- Stage 4: Opportunity Evaluation -----------------------------
        portfolio = self._portfolio_state(selection.bias)
        score_result = scorer.score(
            vol=vol, trend=trend, matrix=selection, structure=structure,
            corporate_actions=snapshot.corporate_actions, portfolio=portfolio,
        )
        baselines.record_unfiltered(
            symbol, structure.structure, structure.credit, structure.max_loss,
            score_result.score, taken_by_agent=score_result.state == scorer.TRADE,
        )

        events.emit(
            Stage.OPPORTUNITY_EVALUATION,
            f"{symbol}: score {score_result.score}/100 -> {score_result.state} "
            f"({score_result.reason})",
            symbol=symbol,
            payload={
                "score": score_result.score,
                "state": score_result.state,
                "checks": score_result.checks_dict,
                "failed_hard_gate": (
                    score_result.failed_hard_gate.to_dict()
                    if score_result.failed_hard_gate else None
                ),
                "reason": score_result.reason,
            },
        )

        spread_pct = structure.spread_pct_of_credit
        detail = {
            "iv_condition": vol.condition,
            "trend_condition": trend.condition,
            "structure": structure.structure,
            "expiry": structure.expiry.isoformat(),
            "dte": structure.dte,
            "credit": structure.credit,
            "credit_at_cross": structure.credit_at_cross,
            "width": structure.width,
            "max_loss_per_contract": structure.max_loss,
            "breakevens": structure.breakevens,
            # Measured values behind checks 5, 6 and 7, kept as numbers so the
            # calibration report can show magnitudes, not just pass/fail.
            "short_delta": structure.short_delta,
            "credit_to_width": round(structure.credit_to_width, 4),
            # inf when credit <= 0; stored as None so the row stays valid JSON.
            "spread_pct_of_credit": (
                None if spread_pct == float("inf") else round(spread_pct, 4)
            ),
            "total_spread_points": round(structure.total_spread, 4),
            "min_open_interest": structure.min_open_interest,
            "checks": score_result.checks_dict,
            "score": score_result.score,
            "legs": [
                {"symbol": l.symbol, "side": l.side, "strike": l.contract.strike,
                 "right": l.contract.right}
                for l in structure.legs
            ],
        }

        key_expiry = structure.expiry.isoformat()

        # ---- REJECT ------------------------------------------------------
        if score_result.state == scorer.REJECT:
            watches.reject(symbol, structure.structure, key_expiry, score_result.reason)
            card = decision_card.declined_card(
                symbol=symbol, iv_condition=vol.condition, trend_condition=trend.condition,
                structure=structure.structure, score=score_result.score,
                reason=score_result.reason, checks=score_result.checks_dict,
            )
            events.emit(Stage.FINAL_DECISION, f"{symbol}: REJECT — {score_result.reason}",
                        symbol=symbol, payload={"reason": score_result.reason})
            self.store.add_decision(
                cycle_id=events.cycle_id, symbol=symbol, market_open=market_open, state="REJECT",
                score=score_result.score, structure=structure.structure,
                iv_condition=vol.condition, trend_condition=trend.condition,
                reason=score_result.reason, card=card, detail=detail,
            )
            return SymbolOutcome(symbol, "REJECT", score_result.score,
                                 structure.structure, score_result.reason, card, detail)

        # ---- WATCH -------------------------------------------------------
        if score_result.state == scorer.WATCH:
            outcome = watches.record(
                symbol=symbol, structure=structure.structure, expiry=key_expiry,
                result=score_result, detail=detail,
            )
            card = decision_card.watch_card(
                symbol=symbol, iv_condition=vol.condition, trend_condition=trend.condition,
                structure=structure.structure, score=score_result.score,
                checks=score_result.checks_dict,
                promoting_condition=score_result.promoting_condition,
                cycles_seen=outcome.cycles_seen, expires_after_cycle=LOOP.watch_expiry_cycles,
            )
            events.emit(
                Stage.FINAL_DECISION,
                f"{symbol}: WATCH — {score_result.promoting_condition}",
                symbol=symbol,
                payload={"promoting_condition": score_result.promoting_condition,
                         "cycles_seen": outcome.cycles_seen},
            )
            self.store.add_decision(
                cycle_id=events.cycle_id, symbol=symbol, market_open=market_open, state="WATCH",
                score=score_result.score, structure=structure.structure,
                iv_condition=vol.condition, trend_condition=trend.condition,
                reason=score_result.promoting_condition, card=card, detail=detail,
            )
            return SymbolOutcome(symbol, "WATCH", score_result.score, structure.structure,
                                 score_result.promoting_condition, card, detail)

        # ---- Stage 5: Risk Review ----------------------------------------
        risk_decision = risk_rules.evaluate(
            structure=structure,
            equity=account.equity,
            open_positions=portfolio.open_positions,
            same_direction_positions=portfolio.same_direction_positions,
            proposed_bias=selection.bias,
            corporate_actions=snapshot.corporate_actions,
            store=self.store,
            events=events,
            today=today,
            market_open=market_open,
        )
        events.emit(
            Stage.RISK_REVIEW,
            f"{symbol}: risk gate {risk_decision.status}"
            + (f" — {risk_decision.reason}" if risk_decision.reasons else
               f" — {risk_decision.contracts} contract(s), max loss "
               f"${risk_decision.max_loss_total:.2f} of ${risk_decision.max_loss_cap:.2f} cap"),
            symbol=symbol,
            payload={
                "approved": risk_decision.approved,
                "contracts": risk_decision.contracts,
                "max_loss_total": risk_decision.max_loss_total,
                "max_loss_cap": risk_decision.max_loss_cap,
                "reasons": risk_decision.reasons,
                "exit_plan": risk_decision.exit_plan.to_dict() if risk_decision.exit_plan else None,
            },
        )
        detail["risk"] = risk_decision.detail
        detail["risk_reasons"] = risk_decision.reasons

        if not risk_decision.approved:
            reason = f"risk gate refused: {risk_decision.reason}"
            watches.reject(symbol, structure.structure, key_expiry, reason)
            card = decision_card.declined_card(
                symbol=symbol, iv_condition=vol.condition, trend_condition=trend.condition,
                structure=structure.structure, score=score_result.score, reason=reason,
                checks=score_result.checks_dict,
            )
            events.emit(Stage.FINAL_DECISION, f"{symbol}: REJECT — {reason}",
                        symbol=symbol, payload={"reason": reason})
            self.store.add_decision(
                cycle_id=events.cycle_id, symbol=symbol, market_open=market_open, state="REJECT",
                score=score_result.score, structure=structure.structure,
                iv_condition=vol.condition, trend_condition=trend.condition,
                reason=reason, card=card, detail=detail,
            )
            return SymbolOutcome(symbol, "REJECT", score_result.score, structure.structure,
                                 reason, card, detail)

        # ---- Stage 6: Final Decision + Execution -------------------------
        watches.promote(symbol, structure.structure, key_expiry, score_result.score)

        order = await executor.open_structure(structure, risk_decision, today=today)
        detail["order"] = {
            "client_order_id": order.client_order_id,
            "submitted": order.submitted,
            "dry_run": order.dry_run,
            "status": order.status,
            "error": order.error,
        }

        card = decision_card.trade_card(
            symbol=symbol, iv_condition=vol.condition, trend_condition=trend.condition,
            structure=structure.structure, score=score_result.score,
            checks=score_result.checks_dict,
            credit=structure.credit * risk_decision.contracts,
            width=structure.width * 100,
            breakeven=structure.breakeven,
            max_loss=risk_decision.max_loss_total,
            dte=structure.dte,
            contracts=risk_decision.contracts,
            max_loss_cap=risk_decision.max_loss_cap,
            position_count=portfolio.open_positions + 1,
            position_limit=RISK.max_concurrent_positions,
        )
        events.emit(
            Stage.FINAL_DECISION,
            f"{symbol}: TRADE — {structure.describe()} x{risk_decision.contracts}"
            + (" [DRY_RUN, not transmitted]" if order.dry_run else ""),
            symbol=symbol,
            payload={"score": score_result.score, "order": detail["order"]},
        )
        self.store.add_decision(
            cycle_id=events.cycle_id, symbol=symbol, market_open=market_open, state="TRADE",
            score=score_result.score, structure=structure.structure,
            iv_condition=vol.condition, trend_condition=trend.condition,
            reason=score_result.reason, card=card, detail=detail,
        )
        return SymbolOutcome(symbol, "TRADE", score_result.score, structure.structure,
                             score_result.reason, card, detail)

    # -- helpers -----------------------------------------------------------
    def _record_no_structure(
        self, symbol: str, vol, trend, selection, events: EventLog
    ) -> SymbolOutcome:
        reason = selection.rationale
        card = decision_card.declined_card(
            symbol=symbol, iv_condition=vol.condition, trend_condition=trend.condition,
            structure=selection.structure, score=0, reason=reason,
        )
        events.emit(Stage.FINAL_DECISION, f"{symbol}: REJECT — {reason}",
                    symbol=symbol, payload={"reason": reason})
        self.store.add_decision(
            cycle_id=events.cycle_id, symbol=symbol, market_open=market_open, state="REJECT", score=0,
            structure=selection.structure, iv_condition=vol.condition,
            trend_condition=trend.condition, reason=reason, card=card,
            detail={"stage": "strategy_selection", "iv_condition": vol.condition,
                    "trend_condition": trend.condition},
        )
        return SymbolOutcome(symbol, "REJECT", 0, selection.structure, reason, card)

    def _portfolio_state(self, proposed_bias: int) -> scorer.PortfolioState:
        open_positions = self.store.open_positions()
        same_direction = 0
        for position in open_positions:
            bias = strategy_matrix.STRUCTURE_BIAS.get(position["structure"], 0)
            if bias != 0 and bias == proposed_bias:
                same_direction += 1
        return scorer.PortfolioState(
            open_positions=len(open_positions),
            same_direction_positions=same_direction,
            proposed_bias=proposed_bias,
        )

    # -- publishing --------------------------------------------------------
    _last_publish_digest: str | None = None

    async def _publish_snapshot(self, push: bool) -> None:
        """
        Write the static snapshot; commit only on a material change.

        Never raises into the scan loop: a publishing failure must not stop the
        agent from trading.
        """
        from dashboard import export as export_mod
        from dashboard import git_publish

        events = EventLog.start(self.store, echo=self.echo)
        try:
            result = export_mod.publish(self.store, previous_digest=self._last_publish_digest)
        except export_mod.RedactionError as exc:
            events.error(f"PUBLISH BLOCKED — {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - publishing must not kill the loop
            events.error(f"Publish failed: {type(exc).__name__}: {exc}")
            return

        if not result.changed:
            events.emit(
                Stage.EXECUTION,
                "Publish skipped — snapshot unchanged since the last commit",
                payload={"digest": result.digest[:12]},
            )
            return

        self._last_publish_digest = result.digest
        events.emit(
            Stage.EXECUTION,
            f"Published static snapshot ({len(result.files)} files, digest "
            f"{result.digest[:12]})",
            payload={"digest": result.digest, "generated_at": result.generated_at},
        )
        git_publish.commit_and_push(
            [p for p, _ in result.files],
            f"dashboard: snapshot {result.generated_at}", events, push=push)

    # -- continuous loop ---------------------------------------------------
    async def run_forever(self, publish: bool = False, push: bool = False) -> None:
        """
        Unattended scan loop, on a monotonic schedule.

        The first cycle runs immediately — starting the agent at 09:30 scans at
        09:30, it does not wait an interval.

        Cadence is driven by a deadline advanced by the interval, not by sleeping
        for the interval after each cycle. Sleeping afterwards makes the period
        `interval + cycle duration`, so scans drift later by ~8s every iteration
        and never land on a boundary. That matters because the expiry-day 15:30
        close and the 09-04 hard close fire on a *scan*, not on a timer: a
        drifted loop reaches them late.

        With `publish=True` a static dashboard snapshot is written after every
        cycle, but committed only when its material digest changed — a scan every
        15 minutes would otherwise produce ~26 near-identical commits a session.

        The interval itself depends on the session — LOOP.scan_interval_minutes
        while open, LOOP.closed_market_sleep_minutes while closed.

        A cycle failure never terminates the loop: the MCP client reconnects on
        its own, and any other error is recorded and retried next interval.
        """
        loop = asyncio.get_running_loop()
        next_run = loop.time()

        while True:
            market_open = True
            try:
                result = await self.run_cycle()
                market_open = result.market_open
                if result.error:
                    print(f"[cycle {result.cycle_id}] error: {result.error}")
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                self.store.add_event(Stage.ERROR, f"Loop iteration failed: {exc}")
                print(f"[loop] error: {exc}")

            if publish:
                await self._publish_snapshot(push)

            interval = LOOP.interval_minutes(market_open) * 60
            next_run += interval
            now = loop.time()
            if next_run <= now:
                # The cycle overran its slot. Skip whole intervals rather than
                # firing a burst of catch-up scans back to back.
                missed = int((now - next_run) // interval) + 1
                next_run += missed * interval
                self.store.add_event(
                    Stage.ERROR,
                    f"Scan overran its {interval / 60:.0f}m slot; skipped "
                    f"{missed} interval(s) to stay on schedule",
                )
            await asyncio.sleep(max(0.0, next_run - loop.time()))
