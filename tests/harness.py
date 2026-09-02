"""
Dry-run self-test harness.

Exercises the paths that a normal DRY_RUN scan never reaches: the order lifecycle
including the re-quote ladder, every exit trigger in isolation, the circuit
breaker, order idempotency under a submit timeout, and MCP reconnect after the
server subprocess is killed.

Design rules
------------
* Real chain data is used wherever the scenario allows (structure construction
  comes from a live get_option_chain call).
* Broker responses are simulated only where the scenario *requires* a state the
  paper account will not produce on demand — an unfilled order, a submit
  timeout, a tripped breaker. Those simulations wrap the real AlpacaMCP so the
  production code path is unchanged.
* No production threshold or rule is modified to make a scenario pass. Failures
  are reported as failures.

Run with:  .venv/bin/python cli.py selftest
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import bootstrap  # noqa: F401
from config import (
    ENV,
    EXECUTION,
    EXPIRY_DAY_CLOSE_TIME,
    HARD_CLOSE_AT,
    MARKET_TZ,
    RISK,
    SCORING,
    UNIVERSE,
    now_et,
    today_et,
)
from decision import matrix as strategy_matrix
from decision.structure import StructureError, build as build_structure
from execution.orders import CLOSE_INTENT, OPEN_INTENT, Executor, client_order_id, position_key
from logging.events import EventLog, Stage
from logging.store import Store
from perception.market import MarketData
from perception.mcp_client import AlpacaMCP, MCPError
from risk import rules as risk_rules
from signals import trend as trend_signal
from signals import volatility as vol_signal

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, RED, AMBER, CYAN = "\033[32m", "\033[31m", "\033[33m", "\033[36m"


@dataclass
class Outcome:
    name: str
    passed: bool | None  # None = could not be tested
    detail: str = ""
    notes: list[str] = field(default_factory=list)


def hdr(title: str) -> None:
    print(f"\n{BOLD}{'=' * 78}{RESET}")
    print(f"{BOLD}{title}{RESET}")
    print(f"{BOLD}{'=' * 78}{RESET}")


def sub(title: str) -> None:
    print(f"\n{CYAN}--- {title} ---{RESET}")


def verdict(ok: bool | None, text: str) -> str:
    if ok is None:
        return f"{AMBER}NOT TESTED{RESET} {text}"
    return (f"{GREEN}PASS{RESET} " if ok else f"{RED}FAIL{RESET} ") + text


def show_events(store: Store, cycle_id: str, stages: tuple[str, ...] | None = None) -> None:
    """Print the recorded event log for a cycle — the actual backend rows."""
    rows = store.query(
        "SELECT * FROM events WHERE cycle_id=? ORDER BY id ASC", (cycle_id,)
    )
    if not rows:
        print(f"  {DIM}(no events recorded){RESET}")
        return
    for row in rows:
        if stages and row["stage"] not in stages:
            continue
        sym = f" {row['symbol']}" if row["symbol"] else ""
        print(f"  {DIM}{row['ts'][11:19]}Z{RESET} [{row['stage']}]{sym} {row['message']}")


def test_store(tag: str) -> Store:
    path = Path(f"/tmp/oaa_selftest_{tag}.db")
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()
    return Store(path)


# ---------------------------------------------------------------------------
# Simulated broker wrappers (used only where a real state cannot be requested)
# ---------------------------------------------------------------------------


class NeverFillsMCP:
    """Wraps a real AlpacaMCP; order placement/replacement returns 'new'."""

    def __init__(self, inner: AlpacaMCP) -> None:
        self.inner = inner
        self.transmitted: list[dict] = []
        self._order_seq = 0

    def bind_events(self, events): self.inner.bind_events(events)
    def require(self, *t): self.inner.require(*t)
    def tool_names(self): return self.inner.tool_names()

    async def call(self, tool: str, arguments: dict | None = None) -> Any:
        args = arguments or {}
        if tool == "place_option_order":
            self._order_seq += 1
            self.transmitted.append({"tool": tool, "arguments": args})
            return {"id": f"sim-order-{self._order_seq}", "status": "new"}
        if tool == "replace_order_by_id":
            self._order_seq += 1
            self.transmitted.append({"tool": tool, "arguments": args})
            return {"id": f"sim-order-{self._order_seq}", "status": "new"}
        if tool == "get_order_by_id":
            return {"id": args.get("order_id"), "status": "new"}
        if tool == "get_order_by_client_id":
            raise MCPError("order not found")
        return await self.inner.call(tool, args)


class TimeoutOnceMCP:
    """First place_option_order times out after the broker accepted it."""

    def __init__(self, inner: AlpacaMCP) -> None:
        self.inner = inner
        self.submit_calls = 0
        self.accepted: dict[str, dict] = {}

    def bind_events(self, events): self.inner.bind_events(events)
    def require(self, *t): self.inner.require(*t)
    def tool_names(self): return self.inner.tool_names()

    async def call(self, tool: str, arguments: dict | None = None) -> Any:
        args = arguments or {}
        if tool == "place_option_order":
            self.submit_calls += 1
            coid = args.get("client_order_id")
            # The broker accepts the order, then the response is lost.
            self.accepted[coid] = {"id": "sim-broker-1", "status": "accepted",
                                   "client_order_id": coid}
            if self.submit_calls == 1:
                raise MCPError("timed out waiting for place_option_order response")
            return self.accepted[coid]
        if tool == "get_order_by_client_id":
            coid = args.get("client_order_id")
            if coid in self.accepted:
                return self.accepted[coid]
            raise MCPError("order not found")
        if tool == "get_order_by_id":
            return {"id": args.get("order_id"), "status": "accepted"}
        return await self.inner.call(tool, args)


# ---------------------------------------------------------------------------
# Scenario 1 — full order lifecycle in DRY_RUN, including the re-quote ladder
# ---------------------------------------------------------------------------


async def scenario_1_order_lifecycle(mcp: AlpacaMCP, store: Store) -> Outcome:
    hdr("SCENARIO 1 — ORDER LIFECYCLE IN DRY_RUN (real chain data)")
    events = EventLog.start(store)
    mcp.bind_events(events)
    market = MarketData(mcp)
    today = today_et()
    notes: list[str] = []

    symbol = None
    structure = None
    for candidate in UNIVERSE.watchlist:
        snap = await market.snapshot_underlying(candidate, today)
        vol = vol_signal.evaluate(snap.closes, snap.contracts, snap.spot, [])
        trend = trend_signal.evaluate(snap.closes, snap.spot)
        selection = strategy_matrix.select(vol, trend)
        if not selection.eligible:
            continue
        try:
            structure = build_structure(selection, snap.contracts, snap.spot, today)
            symbol = candidate
            break
        except StructureError as exc:
            notes.append(f"{candidate}: {exc}")
    if structure is None:
        return Outcome("1 order lifecycle", None,
                       "no compliant structure available from live chains", notes)

    print(f"  built from live chain: {structure.describe()}")
    print(f"  net mid credit ${structure.credit:.2f}/contract, "
          f"cross floor ${structure.credit_at_cross:.2f}, width ${structure.width * 100:.0f}")

    risk = risk_rules.evaluate(
        structure=structure, equity=100_000.0, open_positions=0,
        same_direction_positions=0, proposed_bias=strategy_matrix.STRUCTURE_BIAS.get(
            structure.structure, 0),
        corporate_actions=[], store=store, events=events, today=today,
    )
    if not risk.approved:
        return Outcome("1 order lifecycle", None,
                       f"risk gate refused the live structure: {risk.reason}", notes)

    sub("1a. DRY_RUN submission — what open_structure records")
    executor = Executor(mcp, market, store, events)
    result = await executor.open_structure(structure, risk, today=today)
    order_row = store.get_order(result.client_order_id)
    request = json.loads(order_row["request"])
    print(f"  client_order_id : {result.client_order_id}")
    print(f"  transmitted     : {RED}NOTHING — DRY_RUN{RESET} (status "
          f"{order_row['status']}, dry_run={bool(order_row['dry_run'])})")
    print(f"  would transmit  : place_option_order")
    print(json.dumps(request, indent=4))

    sub("1b. Re-quote ladder — exact payload at each step")
    ladder = executor.requote_ladder(structure)
    print(f"  ladder (net $/contract): mid ${structure.credit:.2f} -> "
          f"{' -> '.join(f'${p:.2f}' for p in ladder)}  (floor "
          f"${structure.credit_at_cross:.2f})")
    print(f"  {DIM}NOTE: open_structure returns before manage_fill in DRY_RUN, so the{RESET}")
    print(f"  {DIM}ladder is driven directly below against a simulated unfilled order.{RESET}")

    sim = NeverFillsMCP(mcp)
    sim_exec = Executor(sim, market, store, events)
    original_wait = EXECUTION.requote_wait_seconds
    object.__setattr__(EXECUTION, "requote_wait_seconds", 0.05)
    try:
        fill = await sim_exec.manage_fill(structure, risk.contracts, "sim-order-0",
                                          result.client_order_id)
    finally:
        object.__setattr__(EXECUTION, "requote_wait_seconds", original_wait)

    for i, call in enumerate(sim.transmitted, 1):
        print(f"\n  re-quote {i} -> {call['tool']}")
        print(json.dumps(call["arguments"], indent=4))

    print(f"\n  final: filled={fill['filled']} requotes={fill['requotes']} "
          f"final net ${fill['final_price']:.2f} status={fill['status']}")

    sub("1c. Recorded event log")
    show_events(store, events.cycle_id, (Stage.EXECUTION, Stage.ERROR))

    expected = len(ladder)
    ok = (
        len(sim.transmitted) == expected
        and fill["requotes"] == expected
        and abs(fill["final_price"] - structure.credit_at_cross) < 0.011
        and not fill["filled"]
    )
    if result.submitted:
        ok = False
        notes.append("DRY_RUN reported an order as submitted")
    notes.append(
        "manage_fill is not invoked by open_structure under DRY_RUN, so the "
        "re-quote ladder never runs in a normal dry-run scan; it was driven "
        "directly here"
    )
    return Outcome("1 order lifecycle", ok,
                   f"{len(sim.transmitted)}/{expected} re-quotes transmitted, "
                   f"walked to the cross floor, nothing sent in DRY_RUN", notes)


# ---------------------------------------------------------------------------
# Scenario 2 — every exit trigger in isolation, plus precedence
# ---------------------------------------------------------------------------


def synthetic_position(expiry: str, credit: float = 1000.0) -> dict:
    return {
        "position_key": f"SPY|bull put credit spread|{expiry}|test",
        "symbol": "SPY", "structure": "bull put credit spread",
        "contracts": 10, "credit": credit, "width": 5.0, "max_loss": 4000.0,
        "expiry": expiry, "legs": "[]", "status": "OPEN",
    }


def scenario_2_exits(store: Store) -> Outcome:
    hdr("SCENARIO 2 — EXIT TRIGGERS IN ISOLATION (synthetic positions)")
    events = EventLog.start(store)
    executor = Executor(None, None, store, events)  # no MCP needed for trigger logic
    at = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=MARKET_TZ)

    credit = 1000.0
    target = credit * (1 - RISK.profit_target_pct_of_credit)
    stop = credit * RISK.stop_loss_multiple_of_credit
    print(f"  position: credit ${credit:.2f}, profit target <= ${target:.2f}, "
          f"stop >= ${stop:.2f}, time_exit_dte={RISK.time_exit_dte}")
    print(f"  expiry-day close {EXPIRY_DAY_CLOSE_TIME:%H:%M} ET, "
          f"hard close {HARD_CLOSE_AT:%Y-%m-%d %H:%M %Z}")

    # NOTE: every evaluation instant must sit BEFORE HARD_CLOSE_AT
    # (2026-09-04 15:30 ET), otherwise the hard deadline correctly short-circuits
    # every other rule and the other triggers cannot be observed in isolation.
    cases = [
        ("profit target hit",      "2026-09-04", 500.0,  "2026-09-02 12:00", "profit target"),
        ("profit target just miss","2026-09-04", 501.0,  "2026-09-02 12:00", None),
        ("stop hit",               "2026-09-04", 2000.0, "2026-09-02 12:00", "stop loss"),
        ("stop just miss",         "2026-09-04", 1999.0, "2026-09-02 12:00", None),
        ("expiry day, at the open", "2026-09-03", 800.0, "2026-09-03 09:35", None),
        ("expiry day, 15:29",      "2026-09-03", 800.0,  "2026-09-03 15:29", None),
        ("expiry-day 15:30 close", "2026-09-03", 800.0,  "2026-09-03 15:30", "expiry-day close"),
        ("past its expiry date",   "2026-09-03", 800.0,  "2026-09-04 09:35", "expiry-day close"),
        ("09-04 hard close",       "2026-09-30", 800.0,  "2026-09-04 15:30", "hard deadline"),
        ("09-04 one minute early", "2026-09-30", 800.0,  "2026-09-04 15:29", None),
        ("no trigger",             "2026-09-04", 800.0,  "2026-09-02 12:00", None),
    ]
    results = []
    sub("2a. Each trigger in isolation")
    for label, expiry, cost, when, expect in cases:
        reason, _ = executor.exit_trigger(
            synthetic_position(expiry), cost, now=at(when))
        got = reason.split(":")[0] if reason else None
        ok = (expect is None and reason is None) or (expect and reason and reason.startswith(expect))
        results.append(ok)
        shown = (reason or "HOLD")[:64]
        print(f"  {'OK ' if ok else 'BAD'} {label:<24} cost ${cost:>7.0f} @ {when} -> {shown}")

    sub("2b. Precedence when two rules fire at once")
    prec = [
        ("expiry-day close vs profit target", "2026-09-03", 400.0,
         "2026-09-03 15:30", "expiry-day close"),
        ("expiry-day close vs stop loss",     "2026-09-03", 2500.0,
         "2026-09-03 15:30", "expiry-day close"),
        ("hard close vs profit target",       "2026-09-30", 400.0,
         "2026-09-04 15:30", "hard deadline"),
        ("hard close vs expiry-day close",    "2026-09-04", 400.0,
         "2026-09-04 15:30", "hard deadline"),
        # time_exit_dte = -1: the DTE rule no longer pre-empts P&L on expiry
        # day. A position expiring today is managed on its merits until 15:30.
        ("expiry morning: profit target runs","2026-09-03", 400.0,
         "2026-09-03 10:00", "profit target"),
        ("expiry morning: stop runs",         "2026-09-03", 2500.0,
         "2026-09-03 10:00", "stop loss"),
    ]
    for label, expiry, cost, when, expect in prec:
        reason, _ = executor.exit_trigger(
            synthetic_position(expiry), cost, now=at(when))
        ok = bool(reason and reason.startswith(expect))
        results.append(ok)
        print(f"  {'OK ' if ok else 'BAD'} {label:<36} -> {(reason or 'HOLD')[:52]}")

    sub("2c. At time_exit_dte = -1 the DTE rule is unreachable by construction")
    print(f"  {DIM}dte <= -1 means the expiry date has already passed, and the "
          f"expiry-day{RESET}")
    print(f"  {DIM}rule fires for any date before today — it always gets there "
          f"first.{RESET}")
    dte_cases = [
        ("expires today",      "2026-09-03", "2026-09-03 10:00"),
        ("expired yesterday",  "2026-09-02", "2026-09-03 10:00"),
        ("expired last week",  "2026-08-28", "2026-09-03 10:00"),
    ]
    for label, expiry, when in dte_cases:
        reason, measures = executor.exit_trigger(
            synthetic_position(expiry), 800.0, now=at(when))
        fired = bool(reason and reason.startswith("time exit"))
        results.append(not fired)
        where = ("held" if reason is None
                 else reason.split(":")[0])
        print(f"  {'OK ' if not fired else 'BAD'} {label:<18} expiry {expiry} @ "
              f"{when} -> {where} (dte read: "
              f"{measures.get('dte', 'rule never reached')})")

    sub("2d. What the judges read: the plan names the rule that actually fires")
    from risk.rules import build_exit_plan

    plan = build_exit_plan(_ladder_structure(80.0, 75.0), 3)
    text = plan.describe()
    print(f"  {text}")
    rendered = plan.to_dict()
    checks = {
        "no dead '-1 DTE' clause on the card": "DTE" not in text,
        "it names the expiry-day close":
            f"{EXPIRY_DAY_CLOSE_TIME:%H:%M} ET" in text,
        "the dashboard column has a value to render":
            rendered.get("expiry_day_close_et") == f"{EXPIRY_DAY_CLOSE_TIME:%H:%M} ET",
        "the old key survives for plans stored before the rename":
            "time_exit_dte" in rendered,
    }
    for label, ok in checks.items():
        results.append(ok)
        print(f"  {'OK ' if ok else 'BAD'} {label}")

    sub("2e. The entry gate still refuses an expiry inside the DTE floor")
    print(f"  {DIM}pinned to UNIVERSE.min_dte={UNIVERSE.min_dte}, not to "
          f"time_exit_dte={RISK.time_exit_dte}, which can refuse nothing{RESET}")
    gate_store = test_store("expiry_gate")
    gate_events = EventLog.start(gate_store)
    for dte, should_approve in ((0, False), (UNIVERSE.min_dte, True)):
        st = _ladder_structure(80.0, 75.0)
        object.__setattr__(st, "dte", dte)
        object.__setattr__(st, "expiry", today_et() + timedelta(days=dte))
        decision = risk_rules.evaluate(
            structure=st, equity=100_000.0, open_positions=0,
            same_direction_positions=0, proposed_bias=1, corporate_actions=[],
            store=gate_store, events=gate_events, today=today_et())
        expiry_refusal = [r for r in decision.reasons if r.startswith("expiry:")]
        ok = (not expiry_refusal) if should_approve else bool(expiry_refusal)
        results.append(ok)
        print(f"  {'OK ' if ok else 'BAD'} {dte} DTE -> "
              f"{expiry_refusal[0] if expiry_refusal else 'no expiry refusal'}")

    return Outcome("2 exit triggers", all(results),
                   f"{sum(results)}/{len(results)} cases behaved as specified; "
                   f"time_exit_dte={RISK.time_exit_dte} defers expiry day to the "
                   f"{EXPIRY_DAY_CLOSE_TIME:%H:%M} close")


# ---------------------------------------------------------------------------
# Scenario 3 — daily circuit breaker
# ---------------------------------------------------------------------------


def scenario_3_circuit_breaker() -> Outcome:
    hdr("SCENARIO 3 — DAILY CIRCUIT BREAKER")
    results = []

    sub("3a. Order-attempt limit")
    store = test_store("breaker_attempts")
    events = EventLog.start(store)
    store.set_day_starting_equity(100_000.0)
    print(f"  limit is {RISK.max_order_attempts_per_day} attempts/day")
    for i in range(1, RISK.max_order_attempts_per_day + 1):
        store.increment_order_attempts()
        halted, reason = risk_rules.circuit_breaker_state(store, 100_000.0)
        if i >= RISK.max_order_attempts_per_day - 1:
            print(f"    attempt {i:>2}: halted={halted} {reason[:52]}")
    halted, reason = risk_rules.circuit_breaker_state(store, 100_000.0)
    results.append(halted)
    risk_rules.trip_circuit_breaker(store, events, reason)

    print(f"  halt persisted in day_state: {bool(store.day_state()['halted'])}")
    results.append(bool(store.day_state()["halted"]))
    logged = [e for e in store.recent_events(50) if e["stage"] == Stage.CIRCUIT_BREAKER]
    results.append(bool(logged))
    print(f"  halt logged as event: {bool(logged)}")
    for e in logged:
        print(f"    {DIM}{e['ts'][11:19]}Z{RESET} [{e['stage']}] {e['message'][:76]}")

    sub("3b. New orders refused while halted")
    from decision.structure import Leg, ProposedStructure
    from perception.market import OptionContract
    short = OptionContract("SPY260908P00755000", "SPY", date(2026, 9, 8), "P", 755.0,
                           bid=1.00, ask=1.05, delta=-0.28, open_interest=800)
    long = OptionContract("SPY260908P00750000", "SPY", date(2026, 9, 8), "P", 750.0,
                          bid=0.20, ask=0.25, delta=-0.12, open_interest=700)
    st = ProposedStructure(
        "SPY", "bull put credit spread", date(2026, 9, 8), 6,
        [Leg(short, "sell", "sell_to_open"), Leg(long, "buy", "buy_to_open")],
        credit=80.0, credit_at_cross=75.0, width=5.0, max_loss=420.0, max_profit=80.0,
        breakevens=[754.2], total_spread=0.10, short_delta=0.28,
        min_open_interest=700, is_credit=True)
    decision = risk_rules.evaluate(
        structure=st, equity=100_000.0, open_positions=0, same_direction_positions=0,
        proposed_bias=1, corporate_actions=[], store=store, events=events,
        today=date(2026, 9, 1))
    refused = not decision.approved and any("circuit breaker" in r for r in decision.reasons)
    results.append(refused)
    print(f"  risk gate: {decision.status} — {decision.reason[:70]}")

    sub("3c. 3% intraday drawdown")
    store2 = test_store("breaker_drawdown")
    events2 = EventLog.start(store2)
    store2.set_day_starting_equity(100_000.0)
    for equity, label in [(97_500.0, "-2.5%"), (97_100.0, "-2.9%"), (97_000.0, "-3.0%")]:
        halted, reason = risk_rules.circuit_breaker_state(store2, equity)
        print(f"    equity ${equity:,.0f} ({label:>5}): halted={halted} {reason[:46]}")
        if label == "-2.9%":
            results.append(not halted)
        if label == "-3.0%":
            results.append(halted)
            risk_rules.trip_circuit_breaker(store2, events2, reason)
    logged2 = [e for e in store2.recent_events(50) if e["stage"] == Stage.CIRCUIT_BREAKER]
    results.append(bool(logged2))
    for e in logged2:
        print(f"    {DIM}{e['ts'][11:19]}Z{RESET} [{e['stage']}] {e['message'][:76]}")

    return Outcome("3 circuit breaker", all(results),
                   f"{sum(results)}/{len(results)} breaker assertions held")


# ---------------------------------------------------------------------------
# Scenario 4 — order idempotency under a submit timeout
# ---------------------------------------------------------------------------


async def scenario_4_idempotency(mcp: AlpacaMCP) -> Outcome:
    hdr("SCENARIO 4 — IDEMPOTENCY: SUBMIT TIMEOUT THEN RETRY")
    store = test_store("idempotency")
    events = EventLog.start(store)
    results = []

    from decision.structure import Leg, ProposedStructure
    from perception.market import OptionContract
    short = OptionContract("SPY260908P00755000", "SPY", date(2026, 9, 8), "P", 755.0,
                           bid=1.00, ask=1.05, delta=-0.28, open_interest=800)
    long = OptionContract("SPY260908P00750000", "SPY", date(2026, 9, 8), "P", 750.0,
                          bid=0.20, ask=0.25, delta=-0.12, open_interest=700)
    st = ProposedStructure(
        "SPY", "bull put credit spread", date(2026, 9, 8), 6,
        [Leg(short, "sell", "sell_to_open"), Leg(long, "buy", "buy_to_open")],
        credit=80.0, credit_at_cross=75.0, width=5.0, max_loss=420.0, max_profit=80.0,
        breakevens=[754.2], total_spread=0.10, short_delta=0.28,
        min_open_interest=700, is_credit=True)
    risk = risk_rules.evaluate(
        structure=st, equity=100_000.0, open_positions=0, same_direction_positions=0,
        proposed_bias=1, corporate_actions=[], store=store, events=events,
        today=date(2026, 9, 1))
    if not risk.approved:
        return Outcome("4 idempotency", None, f"risk gate refused: {risk.reason}")

    pkey = position_key(st.symbol, st.structure, st.expiry, st.legs)
    expected_coid = client_order_id(pkey, OPEN_INTENT, today_et())
    print(f"  deterministic client_order_id: {expected_coid}")

    sim = TimeoutOnceMCP(mcp)
    executor = Executor(sim, MarketData(mcp), store, events)

    # Force the live path so the timeout is exercised (DRY_RUN would short-circuit).
    was_dry = ENV.dry_run
    object.__setattr__(ENV, "dry_run", False)
    try:
        sub("4a. First submit — broker accepts, response times out")
        first = await executor.open_structure(st, risk, today=today_et())
        print(f"  result: submitted={first.submitted} error={str(first.error)[:60]}")
        print(f"  broker actually accepted: {list(sim.accepted)}")
        print(f"  store row status: {store.get_order(expected_coid)['status']}")
        results.append(first.error is not None and not first.submitted)

        sub("4b. Retry with identical inputs")
        second = await executor.open_structure(st, risk, today=today_et())
        print(f"  result: submitted={second.submitted} status={second.status} "
              f"idempotent_skip={second.detail.get('idempotent_skip')}")
        print(f"  client_order_id reused: {second.client_order_id == expected_coid}")
        results.append(second.client_order_id == expected_coid)
        results.append(not second.submitted)
        results.append(sim.submit_calls == 1)
        print(f"  place_option_order calls to broker: {sim.submit_calls} "
              f"(must be 1 — a second call would be a double-submit)")

        rows = store.query("SELECT * FROM orders")
        positions = store.query("SELECT * FROM positions")
        results.append(len(rows) == 1)
        print(f"  order rows: {len(rows)}   position rows: {len(positions)}")
    finally:
        object.__setattr__(ENV, "dry_run", was_dry)

    sub("4c. Recorded event log")
    show_events(store, events.cycle_id, (Stage.EXECUTION, Stage.ERROR))

    return Outcome("4 idempotency", all(results),
                   f"{sum(results)}/{len(results)} assertions held; broker received "
                   f"{sim.submit_calls} submit(s)")


# ---------------------------------------------------------------------------
# Scenario 5 — MCP reconnect after the server subprocess is killed
# ---------------------------------------------------------------------------


def _server_pids() -> list[int]:
    """PIDs of running alpaca-mcp-server processes."""
    try:
        out = subprocess.run(["pgrep", "-f", "alpaca-mcp-server"],
                             capture_output=True, text=True, timeout=10).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    return [int(p) for p in out.split() if p.strip().isdigit()]


async def scenario_5_reconnect(store: Store) -> Outcome:
    hdr("SCENARIO 5 — MCP RECONNECT AFTER SUBPROCESS KILL")
    events = EventLog.start(store)
    mcp = AlpacaMCP(store, events)
    results: list[bool] = []
    notes: list[str] = []

    sub("5a. Establish session and make a baseline call")
    await mcp.connect()
    market = MarketData(mcp)
    before = _server_pids()
    account = await market.account()
    print(f"  connected, {len(mcp.tool_names())} tools; equity ${account.equity:,.2f}")
    print(f"  server pid(s): {before}   connect_count={mcp.connect_count}")

    if not before:
        await mcp.disconnect()
        return Outcome("5 mcp reconnect", None,
                       "could not locate the alpaca-mcp-server process to kill")

    sub("5b. Kill the server subprocess mid-cycle")
    for pid in before:
        try:
            os.kill(pid, signal.SIGKILL)
            print(f"  SIGKILL -> pid {pid}")
        except ProcessLookupError:
            print(f"  pid {pid} already gone")
    time.sleep(1.5)
    print(f"  server pid(s) after kill: {_server_pids()}")

    sub("5c. Next call must reconnect and succeed")
    started = time.perf_counter()
    try:
        account2 = await market.account()
        elapsed = time.perf_counter() - started
        print(f"  call succeeded after {elapsed:.1f}s; equity ${account2.equity:,.2f}")
        print(f"  connect_count={mcp.connect_count} (was 1 before the kill)")
        results.append(mcp.connect_count > 1)
        results.append(account2.equity == account.equity)
    except Exception as exc:  # noqa: BLE001 - this is the failure being tested
        print(f"  {RED}call failed after kill: {type(exc).__name__}: {exc}{RESET}")
        results.append(False)
        notes.append(f"reconnect did not recover: {exc}")

    sub("5d. A full scan cycle must still complete after the kill")
    from agent import Agent
    agent = Agent(store=store, echo=False)
    agent.mcp = mcp
    agent.market = market
    for pid in _server_pids():
        try:
            os.kill(pid, signal.SIGKILL)
            print(f"  SIGKILL mid-cycle -> pid {pid}")
        except ProcessLookupError:
            pass
    cycle = await agent.run_cycle(today_et())
    states = [f"{o.symbol}:{o.state}" for o in cycle.outcomes]
    print(f"  cycle {cycle.cycle_id} completed: {', '.join(states)}")
    print(f"  cycle error: {cycle.error or 'none'}")
    completed = cycle.error is None and len(cycle.outcomes) == len(UNIVERSE.watchlist)
    no_errors = all(o.state != "ERROR" for o in cycle.outcomes)
    results.append(completed)
    results.append(no_errors)
    if not no_errors:
        notes.append("cycle completed but some symbols errored after the kill")

    sub("5e. Reconnect events recorded")
    rows = store.query(
        "SELECT * FROM events WHERE stage=? ORDER BY id ASC", (Stage.MCP,))
    for row in rows[-8:]:
        print(f"  {DIM}{row['ts'][11:19]}Z{RESET} [{row['stage']}] {row['message'][:74]}")
    results.append(any("Reconnect" in (r["message"] or "") for r in rows))

    await mcp.disconnect()
    return Outcome("5 mcp reconnect", all(results),
                   f"{sum(results)}/{len(results)} assertions held; "
                   f"{mcp.connect_count} session(s) established", notes)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run_all(only: int | None = None) -> int:
    print(f"{BOLD}OPTIONS ALPHA AGENT — DRY-RUN SELF TEST{RESET}")
    print(f"{DIM}market time {now_et():%Y-%m-%d %H:%M:%S %Z} · DRY_RUN={ENV.dry_run} · "
          f"endpoint {ENV.alpaca_base_url}{RESET}")
    if not ENV.configured:
        print(f"{RED}credentials not configured — scenarios needing live data cannot run{RESET}")

    outcomes: list[Outcome] = []
    store = test_store("main")
    mcp = AlpacaMCP(store)

    needs_live = only in (None, 1, 4, 6)
    connected = False
    if needs_live and ENV.configured:
        try:
            await mcp.connect()
            connected = True
        except Exception as exc:  # noqa: BLE001
            print(f"{RED}could not connect to the MCP server: {exc}{RESET}")

    try:
        if only in (None, 1):
            if connected:
                outcomes.append(await scenario_1_order_lifecycle(mcp, store))
            else:
                outcomes.append(Outcome("1 order lifecycle", None, "no MCP session"))
        if only in (None, 2):
            outcomes.append(scenario_2_exits(test_store("exits")))
        if only in (None, 3):
            outcomes.append(scenario_3_circuit_breaker())
        if only in (None, 4):
            if connected:
                outcomes.append(await scenario_4_idempotency(mcp))
            else:
                outcomes.append(Outcome("4 idempotency", None, "no MCP session"))
        if only in (None, 9):
            outcomes.append(await scenario_9_publish_paths())
        if only in (None, 8):
            outcomes.append(await scenario_8_market_closed())
        if only in (None, 7):
            outcomes.append(await scenario_7_reentry_vs_retry())
        if only in (None, 10):
            outcomes.append(await scenario_10_requote_ladder())
        if only in (None, 11):
            outcomes.append(await scenario_11_unfilled_entry())
        if only in (None, 6):
            if connected:
                outcomes.append(await scenario_6_reconcile_recovery(mcp))
            else:
                outcomes.append(Outcome("6 reconcile recovery", None, "no MCP session"))
    finally:
        if connected:
            await mcp.disconnect()

    if only in (None, 5):
        if ENV.configured:
            outcomes.append(await scenario_5_reconnect(test_store("reconnect")))
        else:
            outcomes.append(Outcome("5 mcp reconnect", None, "credentials not configured"))

    hdr("SELF TEST SUMMARY")
    for outcome in outcomes:
        print(f"  {verdict(outcome.passed, outcome.name)}")
        print(f"      {outcome.detail}")
        for note in outcome.notes:
            print(f"      {AMBER}note:{RESET} {note}")

    failed = [o for o in outcomes if o.passed is False]
    untested = [o for o in outcomes if o.passed is None]
    print(f"\n  {len(outcomes) - len(failed) - len(untested)} passed, "
          f"{len(failed)} failed, {len(untested)} not tested")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# Scenario 6 — recovery of a position lost to a submit timeout
# ---------------------------------------------------------------------------


class LiveAfterTimeoutMCP:
    """
    Broker accepts the order, the submit response is lost, and the resulting
    position is subsequently visible in get_all_positions and get_orders.

    This is the exact failure the harness found: the agent believes the submit
    failed while the broker holds a live spread.
    """

    def __init__(self, inner: AlpacaMCP, legs: list[dict], qty: int,
                 limit_price: str, expiry: str) -> None:
        self.inner = inner
        self.legs = legs
        self.qty = qty
        self.limit_price = limit_price
        self.expiry = expiry
        self.submit_calls = 0
        self.accepted: dict[str, dict] = {}
        self.position_open = False

    def bind_events(self, events): self.inner.bind_events(events)
    def require(self, *t): self.inner.require(*t)
    def tool_names(self): return self.inner.tool_names()

    def _order_obj(self, coid: str) -> dict:
        return {
            "id": "sim-broker-1", "client_order_id": coid, "status": "filled",
            "qty": str(self.qty), "limit_price": self.limit_price,
            "order_class": "mleg",
            "legs": [dict(l) for l in self.legs],
        }

    async def call(self, tool: str, arguments: dict | None = None) -> Any:
        args = arguments or {}
        if tool == "place_option_order":
            self.submit_calls += 1
            coid = args.get("client_order_id")
            self.accepted[coid] = self._order_obj(coid)
            self.position_open = True
            raise MCPError("timed out waiting for place_option_order response")
        if tool == "get_order_by_client_id":
            coid = args.get("client_order_id")
            if coid in self.accepted:
                return self.accepted[coid]
            raise MCPError("order not found")
        if tool == "get_orders":
            return {"orders": list(self.accepted.values())}
        if tool == "get_all_positions":
            if not self.position_open:
                return []
            out = []
            for leg in self.legs:
                qty = self.qty * (-1 if leg["side"] == "sell" else 1)
                out.append({
                    "symbol": leg["symbol"], "qty": str(qty),
                    "side": "short" if qty < 0 else "long",
                    "avg_entry_price": "1.00", "asset_class": "us_option",
                })
            return out
        if tool == "get_option_snapshot":
            return await self.inner.call(tool, args)
        return await self.inner.call(tool, args)


async def scenario_6_reconcile_recovery(mcp: AlpacaMCP) -> Outcome:
    hdr("SCENARIO 6 — RECONCILIATION RECOVERS A POSITION LOST TO A SUBMIT TIMEOUT")
    store = test_store("reconcile_recovery")
    events = EventLog.start(store)
    results: list[bool] = []
    notes: list[str] = []

    from decision.structure import Leg, ProposedStructure
    from execution.reconcile import RECONCILED_MISSING, Reconciler
    from perception.market import OptionContract

    expiry = date(2026, 9, 4)
    short = OptionContract("SPY260904P00755000", "SPY", expiry, "P", 755.0,
                           bid=1.00, ask=1.05, delta=-0.28, open_interest=800)
    long = OptionContract("SPY260904P00750000", "SPY", expiry, "P", 750.0,
                          bid=0.20, ask=0.25, delta=-0.12, open_interest=700)
    st = ProposedStructure(
        "SPY", "bull put credit spread", expiry, 3,
        [Leg(short, "sell", "sell_to_open"), Leg(long, "buy", "buy_to_open")],
        credit=80.0, credit_at_cross=75.0, width=5.0, max_loss=420.0, max_profit=80.0,
        breakevens=[754.2], total_spread=0.10, short_delta=0.28,
        min_open_interest=700, is_credit=True)
    risk = risk_rules.evaluate(
        structure=st, equity=100_000.0, open_positions=0, same_direction_positions=0,
        proposed_bias=1, corporate_actions=[], store=store, events=events,
        today=date(2026, 9, 1))
    if not risk.approved:
        return Outcome("6 reconcile recovery", None, f"risk gate refused: {risk.reason}")

    sim_legs = [l.to_mcp_leg() for l in st.legs]
    sim = LiveAfterTimeoutMCP(mcp, sim_legs, risk.contracts, "-0.80", expiry.isoformat())
    executor = Executor(sim, MarketData(mcp), store, events)
    pkey = position_key(st.symbol, st.structure, st.expiry, st.legs)
    coid = client_order_id(pkey, OPEN_INTENT, today_et())

    was_dry = ENV.dry_run
    object.__setattr__(ENV, "dry_run", False)
    try:
        sub("6a. Submit times out; broker actually holds the spread")
        first = await executor.open_structure(st, risk, today=today_et())
        print(f"  submit result   : submitted={first.submitted} "
              f"error={str(first.error)[:50]}")
        print(f"  local order row : {store.get_order(coid)['status']}")
        print(f"  local positions : {len(store.open_positions())}")
        print(f"  broker holds    : {len(await sim.call('get_all_positions'))} option leg(s)")
        results.append(len(store.open_positions()) == 0)

        sub("6b. Reconciliation pass")
        report = await Reconciler(sim, MarketData(mcp), store, events).run()
        print(report.render())
        open_now = store.open_positions()
        results.append(len(report.adopted) == 1)
        results.append(len(open_now) == 1)

        if not open_now:
            return Outcome("6 reconcile recovery", False,
                           "reconciliation did not create the position row", notes)

        pos = open_now[0]
        print(f"\n  adopted position: {pos['symbol']} {pos['structure']} "
              f"x{pos['contracts']} credit ${pos['credit']:.2f} "
              f"width {pos['width']} expiry {pos['expiry']}")
        results.append(pos["structure"] == "bull put credit spread")
        results.append(int(pos["contracts"]) == risk.contracts)
        results.append(float(pos["credit"]) > 0)
        results.append(pos["expiry"] == expiry.isoformat())
        results.append(len(json.loads(pos["legs"])) == 2)

        sub("6c. Exit rules must now fire on the adopted position")
        at = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=MARKET_TZ)
        credit = float(pos["credit"])
        target = credit * (1 - RISK.profit_target_pct_of_credit)
        stop = credit * RISK.stop_loss_multiple_of_credit
        print(f"  credit ${credit:.2f} -> profit target <= ${target:.2f}, "
              f"stop >= ${stop:.2f}, expiry {pos['expiry']}")

        exit_cases = [
            ("profit target", target - 1.0,  "2026-09-02 12:00", "profit target"),
            ("stop loss",     stop + 1.0,    "2026-09-02 12:00", "stop loss"),
            # Expiry 2026-09-04: held through its expiry morning now that
            # time_exit_dte is -1, then caught by the 09-04 hard close.
            ("expiry morning", credit * 0.8, "2026-09-04 10:00", None),
            ("hard close",    credit * 0.8,  "2026-09-04 15:30", "hard deadline"),
            ("no trigger",    credit * 0.8,  "2026-09-02 12:00", None),
        ]
        for label, cost, when, expect in exit_cases:
            reason, _ = executor.exit_trigger(pos, cost, now=at(when))
            ok = ((expect is None and reason is None)
                  or (expect is not None and reason is not None
                      and reason.startswith(expect)))
            results.append(ok)
            print(f"  {'OK ' if ok else 'BAD'} {label:<14} cost ${cost:>8.2f} @ {when} "
                  f"-> {(reason or 'HOLD')[:50]}")

        sub("6d. Position disappearing from the broker is closed as missing")
        sim.position_open = False
        report2 = await Reconciler(sim, MarketData(mcp), store, events).run()
        closed = store.query(
            "SELECT * FROM positions WHERE position_key=?", (open_now[0]["position_key"],))
        gone = closed and closed[0]["status"] == "CLOSED"
        results.append(bool(gone))
        results.append(len(report2.closed_missing) == 1)
        print(f"  position status now: {closed[0]['status'] if closed else '?'} "
              f"reason={closed[0]['exit_reason'] if closed else '?'}")
        results.append(bool(closed) and closed[0]["exit_reason"] == RECONCILED_MISSING)
    finally:
        object.__setattr__(ENV, "dry_run", was_dry)

    sub("6e. Recorded event log")
    show_events(store, events.cycle_id,
                (Stage.POSITION_MANAGEMENT, Stage.EXECUTION, Stage.ERROR))

    return Outcome("6 reconcile recovery", all(results),
                   f"{sum(results)}/{len(results)} assertions held; "
                   f"broker received {sim.submit_calls} submit(s)", notes)


# ---------------------------------------------------------------------------
# Scenario 7 — re-entry vs retry: the entry sequence
# ---------------------------------------------------------------------------


class SubmitRecorder:
    """Records every place_option_order; optionally loses the first response."""

    def __init__(self, lose_first: bool = False) -> None:
        self.submits: list[str] = []
        self.lose_first = lose_first
        self.calls = 0

    def bind_events(self, events): pass
    def require(self, *t): pass
    def tool_names(self): return []

    async def call(self, tool: str, arguments: dict | None = None) -> Any:
        args = arguments or {}
        if tool == "place_option_order":
            self.calls += 1
            self.submits.append(args["client_order_id"])
            if self.lose_first and self.calls == 1:
                raise MCPError("timed out waiting for place_option_order response")
            return {"id": f"o{self.calls}", "status": "filled"}
        if tool == "get_order_by_client_id":
            raise MCPError("order not found")
        if tool == "get_order_by_id":
            return {"id": args.get("order_id"), "status": "filled"}
        return {}


def _reentry_structure():
    from decision.structure import Leg, ProposedStructure
    from perception.market import OptionContract

    expiry = date(2026, 9, 4)
    short = OptionContract("SPY260904P00755000", "SPY", expiry, "P", 755.0,
                           bid=1.00, ask=1.05, delta=-0.28, open_interest=800)
    long = OptionContract("SPY260904P00750000", "SPY", expiry, "P", 750.0,
                          bid=0.20, ask=0.25, delta=-0.12, open_interest=700)
    return ProposedStructure(
        "SPY", "bull put credit spread", expiry, 3,
        [Leg(short, "sell", "sell_to_open"), Leg(long, "buy", "buy_to_open")],
        credit=80.0, credit_at_cross=75.0, width=5.0, max_loss=420.0, max_profit=80.0,
        breakevens=[754.2], total_spread=0.10, short_delta=0.28,
        min_open_interest=700, is_credit=True)


async def scenario_7_reentry_vs_retry() -> Outcome:
    hdr("SCENARIO 7 — RE-ENTRY IS ALLOWED, RETRY STILL COLLIDES")
    from execution.orders import entry_sequence, structural_key

    results: list[bool] = []
    st = _reentry_structure()
    sk = structural_key(st.symbol, st.structure, st.expiry, st.legs)

    was_dry = ENV.dry_run
    original_wait = EXECUTION.requote_wait_seconds
    object.__setattr__(ENV, "dry_run", False)
    object.__setattr__(EXECUTION, "requote_wait_seconds", 0.01)

    def prepare(tag: str, lose_first: bool = False):
        store = test_store(tag)
        events = EventLog.start(store)
        mcp = SubmitRecorder(lose_first)
        risk = risk_rules.evaluate(
            structure=st, equity=100_000.0, open_positions=0,
            same_direction_positions=0, proposed_bias=1, corporate_actions=[],
            store=store, events=events, today=date(2026, 9, 1))
        return store, events, mcp, Executor(mcp, None, store, events), risk

    try:
        sub("7a. Position closes at profit, same setup qualifies again")
        store, events, mcp, ex, risk = prepare("reentry_a")
        first = await ex.open_structure(st, risk, today=today_et())
        print(f"  morning   seq=0 submitted={first.submitted} coid={first.client_order_id}")
        store.close_position(store.open_positions()[0]["position_key"],
                             "profit target", 40.0, "x")
        print(f"  midday    closed at profit; sequence now {entry_sequence(store, sk)}")
        second = await ex.open_structure(st, risk, today=today_et())
        print(f"  afternoon seq=1 submitted={second.submitted} coid={second.client_order_id}")
        print(f"  distinct client_order_id : {second.client_order_id != first.client_order_id}")
        print(f"  broker submits           : {len(mcp.submits)} (expected 2)")
        print(f"  position rows            : {len(store.all_positions())} (expected 2)")
        results += [first.submitted, second.submitted,
                    second.client_order_id != first.client_order_id,
                    len(mcp.submits) == 2, len(store.all_positions()) == 2]

        sub("7b. Same setup proposed again while STILL OPEN")
        store, events, mcp, ex, risk = prepare("reentry_b")
        a = await ex.open_structure(st, risk, today=today_et())
        b = await ex.open_structure(st, risk, today=today_et())
        print(f"  second call idempotent_skip={b.detail.get('idempotent_skip')} "
              f"same coid={a.client_order_id == b.client_order_id}")
        print(f"  broker submits: {len(mcp.submits)} (expected 1)")
        results += [not b.submitted, len(mcp.submits) == 1,
                    a.client_order_id == b.client_order_id]

        sub("7c. Retry after a lost submit response")
        store, events, mcp, ex, risk = prepare("reentry_c", lose_first=True)
        a = await ex.open_structure(st, risk, today=today_et())
        seq = entry_sequence(store, sk)
        b = await ex.open_structure(st, risk, today=today_et())
        print(f"  first submitted={a.submitted} error={str(a.error)[:40]}")
        print(f"  sequence unchanged at {seq} (nothing closed)")
        print(f"  retry reuses client_order_id: {a.client_order_id == b.client_order_id}")
        results += [seq == 0, a.client_order_id == b.client_order_id]

        sub("7d. Three round trips in one session")
        store, events, mcp, ex, risk = prepare("reentry_d")
        coids = []
        for i in range(3):
            r = await ex.open_structure(st, risk, today=today_et())
            coids.append(r.client_order_id)
            print(f"  entry {i}: seq={entry_sequence(store, sk)} "
                  f"submitted={r.submitted} coid=...{r.client_order_id[-12:]}")
            for position in store.open_positions():
                store.close_position(position["position_key"], "profit target", 40.0, "x")
        print(f"  unique coids {len(set(coids))}/3, broker submits {len(mcp.submits)}, "
              f"position rows {len(store.all_positions())}")
        results += [len(set(coids)) == 3, len(mcp.submits) == 3,
                    len(store.all_positions()) == 3]
    finally:
        object.__setattr__(ENV, "dry_run", was_dry)
        object.__setattr__(EXECUTION, "requote_wait_seconds", original_wait)

    return Outcome("7 re-entry vs retry", all(results),
                   f"{sum(results)}/{len(results)} assertions held")


# ---------------------------------------------------------------------------
# Scenario 8 — market closed: pipeline runs, nothing is submitted
# ---------------------------------------------------------------------------


async def scenario_8_market_closed() -> Outcome:
    hdr("SCENARIO 8 — MARKET CLOSED: PIPELINE RUNS, NO ORDER IS SUBMITTED")
    store = test_store("market_closed")
    events = EventLog.start(store)
    results: list[bool] = []

    from risk.rules import MARKET_CLOSED

    st = _reentry_structure()
    mcp = SubmitRecorder()
    executor = Executor(mcp, None, store, events, market_open=False)

    was_dry = ENV.dry_run
    object.__setattr__(ENV, "dry_run", False)
    try:
        sub("8a. Risk gate refuses entries with MARKET_CLOSED")
        closed = risk_rules.evaluate(
            structure=st, equity=100_000.0, open_positions=0,
            same_direction_positions=0, proposed_bias=1, corporate_actions=[],
            store=store, events=events, today=date(2026, 9, 1), market_open=False)
        open_ok = risk_rules.evaluate(
            structure=st, equity=100_000.0, open_positions=0,
            same_direction_positions=0, proposed_bias=1, corporate_actions=[],
            store=store, events=events, today=date(2026, 9, 1), market_open=True)
        print(f"  market closed -> {closed.status}: {closed.reason[:64]}")
        print(f"  market open   -> {open_ok.status}: {open_ok.contracts} contract(s)")
        results += [not closed.approved, MARKET_CLOSED in closed.reason, open_ok.approved]

        sub("8b. Entry submission blocked at the order layer")
        entry = await executor.open_structure(st, open_ok, today=today_et())
        print(f"  submitted={entry.submitted} status={entry.status}")
        print(f"  broker place_option_order calls: {len(mcp.submits)} (must be 0)")
        results += [not entry.submitted, len(mcp.submits) == 0]

        sub("8c. Exit trigger EVALUATES but the order is blocked")
        # Real legs: close_structure needs them to build the reversing order.
        position = {
            "position_key": "SPY|bull put credit spread|2026-09-04|test#0",
            "symbol": "SPY", "structure": "bull put credit spread", "contracts": 4,
            "credit": 320.0, "width": 5.0, "max_loss": 1680.0,
            "expiry": "2026-09-04", "status": "OPEN", "dry_run": 0,
            "legs": json.dumps([
                {"symbol": "SPY260904P00755000", "side": "sell",
                 "position_intent": "sell_to_open", "ratio_qty": 1,
                 "strike": 755.0, "right": "P"},
                {"symbol": "SPY260904P00750000", "side": "buy",
                 "position_intent": "buy_to_open", "ratio_qty": 1,
                 "strike": 750.0, "right": "P"},
            ]),
        }
        at = datetime(2026, 9, 2, 20, 0, tzinfo=MARKET_TZ)  # after the close
        reason, measures = executor.exit_trigger(position, 100.0, now=at)
        print(f"  trigger evaluated -> {(reason or 'HOLD')[:60]}")
        results.append(reason is not None and reason.startswith("profit target"))

        closed_result = await executor.close_structure(position, reason, 100.0, today=date(2026, 9, 2))
        print(f"  exit submitted={closed_result.submitted} status={closed_result.status}")
        print(f"  broker calls still: {len(mcp.submits)} (must be 0)")
        results += [not closed_result.submitted, len(mcp.submits) == 0]

        sub("8d. Manual flatten may still force through")
        forced = await executor.close_structure(
            position, "end-of-window flatten", 100.0, today=date(2026, 9, 2), force=True)
        print(f"  forced exit submitted={forced.submitted} "
              f"broker calls={len(mcp.submits)} (must be 1)")
        results += [forced.submitted, len(mcp.submits) == 1]

        sub("8e. Decision recorded with market_open=false")
        store.add_decision(cycle_id=events.cycle_id, symbol="SPY", state="REJECT",
                           market_open=False, score=0,
                           structure="bull put credit spread",
                           reason=f"risk gate refused: {closed.reason}", card="x")
        store.add_decision(cycle_id=events.cycle_id, symbol="SPY", state="TRADE",
                           market_open=True, score=90,
                           structure="bull put credit spread", reason="in session",
                           card="y")
        counts = store.decision_session_counts()
        print(f"  session counts: {counts}")
        in_session = store.all_decisions(session_only=True)
        every = store.all_decisions(session_only=False)
        print(f"  all_decisions(session_only=True)={len(in_session)}  "
              f"all={len(every)}")
        results += [counts["out_of_session"] >= 1, counts["in_session"] >= 1,
                    len(in_session) < len(every)]

        sub("8f. Ledger defaults to in-session and counts the rest separately")
        from monitoring import ledger as ledger_mod

        led = ledger_mod.build(store, session_only=True)
        print(f"  total (in-session)={led.total_decisions} "
              f"out_of_session={led.out_of_session} "
              f"unrecorded={led.unrecorded_session}")
        results += [led.session_only, led.out_of_session >= 1]

        sub("8g. Recorded event log")
        show_events(store, events.cycle_id, (Stage.EXECUTION, Stage.POSITION_MANAGEMENT))
    finally:
        object.__setattr__(ENV, "dry_run", was_dry)

    return Outcome("8 market closed", all(results),
                   f"{sum(results)}/{len(results)} assertions held; broker received "
                   f"{len(mcp.submits)} submit(s) (only the forced flatten)")


# ---------------------------------------------------------------------------
# Scenario 9 — publish paths actually commit and push
# ---------------------------------------------------------------------------


def _make_repo(tag: str) -> tuple[Path, Path]:
    """A throwaway work tree with a real bare remote, for exercising git."""
    import shutil

    base = Path(f"/tmp/oaa_git_{tag}")
    if base.exists():
        shutil.rmtree(base)
    remote, work = base / "remote.git", base / "work"
    remote.mkdir(parents=True)
    work.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)],
                   capture_output=True, check=True)
    for cmd in (["git", "init", "-b", "main"],
                ["git", "config", "user.email", "selftest@example.com"],
                ["git", "config", "user.name", "OAA Selftest"],
                ["git", "remote", "add", "origin", str(remote)]):
        subprocess.run(cmd, cwd=work, capture_output=True, check=True)
    (work / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=work, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=work, capture_output=True, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=work,
                   capture_output=True, check=True)
    return work, remote


def _remote_commits(remote: Path) -> int:
    out = subprocess.run(["git", "rev-list", "--count", "main"], cwd=remote,
                         capture_output=True, text=True)
    return int(out.stdout.strip() or 0)


async def scenario_9_publish_paths() -> Outcome:
    hdr("SCENARIO 9 — PUBLISH ACTUALLY COMMITS AND PUSHES")
    import cli as cli_mod
    from dashboard import git_publish

    results: list[bool] = []
    parser = cli_mod.build_parser()

    sub("9a. Flag resolution — `publish --push` must imply --commit")
    expectations = [
        (["publish"], (False, False)),
        (["publish", "--commit"], (True, False)),
        (["publish", "--push"], (True, True)),
        (["publish", "--commit", "--push"], (True, True)),
    ]
    for argv, expected in expectations:
        got = cli_mod.resolve_publish_flags(parser.parse_args(argv))
        ok = got == expected
        results.append(ok)
        print(f"  {'OK ' if ok else 'BAD'} {' '.join(argv):<28} -> "
              f"commit={got[0]!s:<5} push={got[1]!s:<5} (expected {expected})")

    sub("9b. Flag resolution — `loop --publish` pushes by default")
    loop_expectations = [
        (["loop"], (False, False)),
        (["loop", "--publish"], (True, True)),
        (["loop", "--publish", "--no-push"], (True, False)),
    ]
    for argv, expected in loop_expectations:
        got = cli_mod.resolve_loop_publish_flags(parser.parse_args(argv))
        ok = got == expected
        results.append(ok)
        print(f"  {'OK ' if ok else 'BAD'} {' '.join(argv):<28} -> "
              f"publish={got[0]!s:<5} push={got[1]!s:<5} (expected {expected})")

    sub("9c. commit_and_push against a real repo with a real remote")
    work, remote = _make_repo("push")
    before = _remote_commits(remote)
    target = work / "data" / "state.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"snapshot": 1}')

    first = git_publish.commit_and_push([target], "snapshot 1", None,
                                        push=True, repo_root=work)
    after = _remote_commits(remote)
    print(f"  committed={first.committed} pushed={first.pushed} — {first.reason}")
    print(f"  remote commits {before} -> {after}")
    results += [first.committed, first.pushed, after == before + 1]

    sub("9d. Unchanged snapshot must not produce a commit")
    second = git_publish.commit_and_push([target], "snapshot 1 again", None,
                                         push=True, repo_root=work)
    after2 = _remote_commits(remote)
    print(f"  committed={second.committed} — {second.reason}")
    print(f"  remote commits still {after2}")
    results += [not second.committed, after2 == after]

    sub("9e. A changed snapshot commits and pushes again")
    target.write_text('{"snapshot": 2}')
    third = git_publish.commit_and_push([target], "snapshot 2", None,
                                        push=True, repo_root=work)
    after3 = _remote_commits(remote)
    print(f"  committed={third.committed} pushed={third.pushed} — {third.reason}")
    print(f"  remote commits {after2} -> {after3}")
    results += [third.committed, third.pushed, after3 == after2 + 1]

    sub("9f. push=False commits locally but leaves the remote untouched")
    target.write_text('{"snapshot": 3}')
    fourth = git_publish.commit_and_push([target], "snapshot 3", None,
                                         push=False, repo_root=work)
    after4 = _remote_commits(remote)
    print(f"  committed={fourth.committed} pushed={fourth.pushed} — {fourth.reason}")
    print(f"  remote commits still {after4} (local commit not pushed)")
    results += [fourth.committed, not fourth.pushed, after4 == after3]

    sub("9g. A non-repo degrades without raising")
    plain = Path("/tmp/oaa_git_plain")
    plain.mkdir(parents=True, exist_ok=True)
    (plain / "x.json").write_text("{}")
    none = git_publish.commit_and_push([plain / "x.json"], "no repo", None,
                                       push=True, repo_root=plain)
    print(f"  committed={none.committed} — {none.reason}")
    results += [not none.committed, "not a git repository" in none.reason]

    return Outcome("9 publish paths", all(results),
                   f"{sum(results)}/{len(results)} assertions held; "
                   f"remote received {after3} commit(s)")


# ---------------------------------------------------------------------------
# Scenario 10 — the re-quote ladder survives a 422 no-op
# ---------------------------------------------------------------------------


class NoOpReplaceMCP:
    """
    A broker that answers 422 when a replacement changes nothing.

    Alpaca's real behaviour: `replace_order_by_id` with the limit the order
    already carries is rejected with "order parameters are not changed". The
    order stays alive and unchanged — it is not an error the ladder should quit
    on. `reject_limits` forces that answer for chosen limits so the skip path is
    exercised even after the ladder was made distinct.
    """

    def __init__(self, reject_limits: tuple[str, ...] = ()) -> None:
        self.reject_limits = set(reject_limits)
        self.resting = ""
        self.transmitted: list[dict] = []
        self.rejected: list[str] = []
        self._seq = 0

    def bind_events(self, events): pass
    def require(self, *t): pass
    def tool_names(self): return []

    async def call(self, tool: str, arguments: dict | None = None) -> Any:
        args = arguments or {}
        if tool == "replace_order_by_id":
            limit = str(args.get("limit_price"))
            if limit == self.resting or limit in self.reject_limits:
                self.rejected.append(limit)
                raise MCPError(
                    'HTTP error 422: {"message":"order parameters are not changed"}')
            self.resting = limit
            self._seq += 1
            self.transmitted.append({"tool": tool, "arguments": args})
            return {"id": f"sim-order-{self._seq}", "status": "new"}
        if tool == "get_order_by_id":
            return {"id": args.get("order_id"), "status": "new"}
        if tool == "get_order_by_client_id":
            raise MCPError("order not found")
        return {}


async def scenario_10_requote_ladder() -> Outcome:
    hdr("SCENARIO 10 — RE-QUOTE LADDER: DISTINCT PRICES, 422 NO-OP SKIPS ON")
    store = test_store("requote")
    events = EventLog.start(store)
    executor = Executor(None, None, store, events)
    results: list[bool] = []
    notes: list[str] = []

    sub("10a. Every ladder step must transmit a DIFFERENT rounded limit")
    print(f"  {DIM}net price is carried in dollars per spread but transmitted as a"
          f" per-share{RESET}")
    print(f"  {DIM}limit rounded to the cent, so steps under $1.00 of net collapse"
          f" together.{RESET}\n")
    geometries = [
        ("live SPY case (1c of net)", 71.0, 70.0),
        ("wide spread",               80.0, 68.0),
        ("uneven walk",               74.5, 70.2),
        ("3c of net",                 71.0, 68.0),
        ("sub-cent walk",             71.0, 70.6),
        ("mid already at cross",      71.0, 71.0),
    ]
    for label, mid, cross in geometries:
        st = _ladder_structure(mid, cross)
        ladder = executor.requote_ladder(st)
        limits = [executor._limit_price(p, 1) for p in ladder]
        resting = executor._limit_price(mid, 1)
        distinct = len(set(limits)) == len(limits)
        no_resting = resting not in limits
        ends_at_cross = (not ladder) or abs(ladder[-1] - cross) < 1e-9
        ok = distinct and no_resting and ends_at_cross
        results.append(ok)
        print(f"  {'OK ' if ok else 'BAD'} {label:<26} mid {resting} -> "
              f"{' -> '.join(limits) if limits else '(no distinct step)'}")
        if not ok:
            print(f"      distinct={distinct} excludes_resting={no_resting} "
                  f"ends_at_cross={ends_at_cross}")

    sub("10b. A 422 no-op skips to the next price instead of aborting the ladder")
    st = _ladder_structure(80.0, 68.0)
    ladder = executor.requote_ladder(st)
    doomed = executor._limit_price(ladder[0], 1)
    print(f"  ladder {[executor._limit_price(p, 1) for p in ladder]}; the broker is "
          f"forced to answer 422 for {doomed}")
    sim = NoOpReplaceMCP(reject_limits=(doomed,))
    sim_exec = Executor(sim, None, store, events)
    original_wait = EXECUTION.requote_wait_seconds
    object.__setattr__(EXECUTION, "requote_wait_seconds", 0.01)
    try:
        fill = await sim_exec.manage_fill(st, 3, "sim-order-0", "oaa-open-selftest10")
    finally:
        object.__setattr__(EXECUTION, "requote_wait_seconds", original_wait)

    sent = [c["arguments"]["limit_price"] for c in sim.transmitted]
    print(f"  rejected as no-op : {sim.rejected}")
    print(f"  transmitted       : {sent}")
    print(f"  result            : requotes={fill['requotes']} "
          f"final net ${fill['final_price']:.2f} (cross floor "
          f"${st.credit_at_cross:.2f}) filled={fill['filled']}")
    remaining = [executor._limit_price(p, 1) for p in ladder[1:]]
    checks = {
        "the 422 did not abort the ladder": sent == remaining,
        "every later step was still transmitted": len(sent) == len(ladder) - 1,
        "the skipped step is not counted as a re-quote": fill["requotes"] == len(ladder) - 1,
        "the walk still reached the cross floor":
            abs(fill["final_price"] - st.credit_at_cross) < 1e-9,
    }
    for label, ok in checks.items():
        results.append(ok)
        print(f"  {'OK ' if ok else 'BAD'} {label}")

    sub("10c. A real failure still stops the ladder")

    class BrokenMCP(NoOpReplaceMCP):
        async def call(self, tool: str, arguments: dict | None = None) -> Any:
            if tool == "replace_order_by_id":
                raise MCPError("HTTP error 403: insufficient buying power")
            return await NoOpReplaceMCP.call(self, tool, arguments)

    broken = BrokenMCP()
    object.__setattr__(EXECUTION, "requote_wait_seconds", 0.01)
    try:
        broke = await Executor(broken, None, store, events).manage_fill(
            st, 3, "sim-order-0", "oaa-open-selftest10b")
    finally:
        object.__setattr__(EXECUTION, "requote_wait_seconds", original_wait)
    ok = broke["requotes"] == 0 and not broke["filled"]
    results.append(ok)
    print(f"  {'OK ' if ok else 'BAD'} a 403 aborts after {broke['requotes']} "
          f"re-quote(s) — only the no-op is skipped, not every error")

    sub("10d. Recorded event log")
    show_events(store, events.cycle_id, (Stage.EXECUTION, Stage.ERROR))

    notes.append(
        "the live failure was a ladder whose next step rounded to the limit "
        "already resting: 0 re-quotes were transmitted before the 422 ended the walk")
    return Outcome("10 re-quote ladder", all(results),
                   f"{sum(results)}/{len(results)} assertions held; ladder steps are "
                   f"distinct and a 422 no-op costs one step, not the walk", notes)


def _ladder_structure(mid: float, cross: float):
    """A structure carrying only the two prices the ladder is built from."""
    from decision.structure import Leg, ProposedStructure
    from perception.market import OptionContract

    expiry = date(2026, 9, 4)
    short = OptionContract("SPY260904P00755000", "SPY", expiry, "P", 755.0,
                           bid=1.00, ask=1.05, delta=-0.28, open_interest=800)
    long = OptionContract("SPY260904P00750000", "SPY", expiry, "P", 750.0,
                          bid=0.20, ask=0.25, delta=-0.12, open_interest=700)
    return ProposedStructure(
        "SPY", "bull put credit spread", expiry, 3,
        [Leg(short, "sell", "sell_to_open"), Leg(long, "buy", "buy_to_open")],
        credit=mid, credit_at_cross=cross, width=5.0, max_loss=420.0, max_profit=mid,
        breakevens=[754.2], total_spread=0.10, short_delta=0.28,
        min_open_interest=700, is_credit=True)


# ---------------------------------------------------------------------------
# Scenario 11 — an unfilled entry is cancelled, and never closed while it lives
# ---------------------------------------------------------------------------


class RestingEntryMCP:
    """
    Broker where the entry order rests unfilled: accepted, never filled.

    Models the exact live situation. `get_all_positions` stays empty (nothing
    filled), while `get_orders` keeps returning the working order until it is
    cancelled — the two readings reconciliation has to combine.

    `fill_on_confirm` makes the order fill at the moment the sweep re-reads its
    status, which is the race the sweep must not lose.
    """

    def __init__(self, legs: list[dict], qty: int, limit_price: str,
                 fill_on_confirm: bool = False, orders_readable: bool = True) -> None:
        self.legs = legs
        self.qty = qty
        self.limit_price = limit_price
        self.fill_on_confirm = fill_on_confirm
        self.orders_readable = orders_readable
        self.orders: dict[str, dict] = {}
        self.cancels: list[str] = []
        self.status_reads = 0
        self._seq = 0

    def bind_events(self, events): pass
    def require(self, *t): pass
    def tool_names(self): return []

    def _order(self, coid: str, status: str) -> dict:
        return {
            "id": f"sim-broker-{self._seq}", "client_order_id": coid,
            "status": status, "qty": str(self.qty),
            "filled_qty": str(self.qty) if status == "filled" else "0",
            "limit_price": self.limit_price, "order_class": "mleg",
            "legs": [dict(l) for l in self.legs],
        }

    async def call(self, tool: str, arguments: dict | None = None) -> Any:
        args = arguments or {}
        if tool == "place_option_order":
            self._seq += 1
            coid = args["client_order_id"]
            self.orders[coid] = self._order(coid, "new")
            return self.orders[coid]
        if tool == "get_order_by_client_id":
            coid = args.get("client_order_id")
            if coid not in self.orders:
                raise MCPError("order not found")
            self.status_reads += 1
            if self.fill_on_confirm and self.orders[coid]["status"] == "new":
                self.orders[coid] = self._order(coid, "filled")
            return self.orders[coid]
        if tool == "get_order_by_id":
            return next((o for o in self.orders.values()
                         if o["id"] == args.get("order_id")), {"status": "new"})
        if tool == "get_orders":
            if not self.orders_readable:
                raise MCPError("HTTP error 500: orders unavailable")
            wanted = args.get("status", "open")
            out = list(self.orders.values())
            if wanted == "open":
                out = [o for o in out if o["status"] in ("new", "accepted",
                                                         "partially_filled")]
            return {"orders": out}
        if tool == "cancel_order_by_id":
            self.cancels.append(str(args.get("order_id")))
            for coid, order in self.orders.items():
                if order["id"] == str(args.get("order_id")) and order["status"] != "filled":
                    self.orders[coid] = self._order(coid, "canceled")
            return {}
        if tool == "get_all_positions":
            # Nothing ever filled, so the broker holds no legs.
            return [] if not any(o["status"] == "filled" for o in self.orders.values()) \
                else [{"symbol": l["symbol"],
                       "qty": str(self.qty * (-1 if l["side"] == "sell" else 1)),
                       "side": "short" if l["side"] == "sell" else "long",
                       "avg_entry_price": "1.00", "asset_class": "us_option"}
                      for l in self.legs]
        return {}


async def scenario_11_unfilled_entry() -> Outcome:
    hdr("SCENARIO 11 — AN UNFILLED ENTRY IS CANCELLED, NOT LEFT RESTING")
    from execution.orders import entry_sequence, structural_key
    from execution.reconcile import ENTRY_CANCELLED, RECONCILED_MISSING, Reconciler

    store = test_store("unfilled_entry")
    events = EventLog.start(store)
    results: list[bool] = []
    notes: list[str] = []

    st = _reentry_structure()
    risk = risk_rules.evaluate(
        structure=st, equity=100_000.0, open_positions=0, same_direction_positions=0,
        proposed_bias=1, corporate_actions=[], store=store, events=events,
        today=date(2026, 9, 1))
    if not risk.approved:
        return Outcome("11 unfilled entry", None, f"risk gate refused: {risk.reason}")

    sim = RestingEntryMCP([l.to_mcp_leg() for l in st.legs], risk.contracts, "-0.80")
    executor = Executor(sim, None, store, events)
    executor.set_market_open(True)
    structural = structural_key(st.symbol, st.structure, st.expiry, st.legs)
    coid = client_order_id(f"{structural}#0", OPEN_INTENT, today_et())

    was_dry, was_wait = ENV.dry_run, EXECUTION.requote_wait_seconds
    object.__setattr__(ENV, "dry_run", False)
    object.__setattr__(EXECUTION, "requote_wait_seconds", 0.01)
    try:
        sub("11a. The entry is placed and rests unfilled")
        placed = await executor.open_structure(st, risk, today=today_et())
        order_row = store.get_order(placed.client_order_id)
        open_rows = store.open_positions()
        print(f"  client_order_id : {placed.client_order_id}")
        print(f"  broker status   : {order_row['status']} (never fills)")
        print(f"  position rows   : {len(open_rows)} OPEN "
              f"(written at submission, nothing filled behind it)")
        print(f"  broker positions: {len(await sim.call('get_all_positions'))} option leg(s)")
        results += [placed.client_order_id == coid, len(open_rows) == 1,
                    str(order_row["status"]).lower() == "new"]

        sub("11b. THE BUG — reconciliation must NOT close a row whose entry is live")
        report = await Reconciler(sim, None, store, events).run()
        print(report.render())
        still_open = store.open_positions()
        kept = (len(report.closed_missing) == 0 and len(report.working_entry) == 1
                and len(still_open) == 1)
        results += [len(report.closed_missing) == 0, len(report.working_entry) == 1,
                    len(still_open) == 1]
        print(f"\n  {'OK ' if kept else 'BAD'} broker holds no legs, but the entry "
              f"order is still working -> row KEPT")
        print(f"      {DIM}closing it here is what advanced the entry sequence and "
              f"let the next cycle open a second position{RESET}")
        seq_now = entry_sequence(store, structural)
        results.append(seq_now == 0)
        print(f"  {'OK ' if seq_now == 0 else 'BAD'} entry sequence still #{seq_now} "
              f"— a second position in {st.symbol} cannot be opened")

        sub("11c. End-of-cycle sweep cancels the unfilled entry")
        cancelled = await executor.cancel_unfilled_entries()
        after_row = store.get_order(coid)
        position_rows = store.query(
            "SELECT * FROM positions WHERE entry_order_id=?", (coid,))
        print(f"  cancel_order_by_id calls : {sim.cancels}")
        print(f"  order status now         : {after_row['status']}")
        print(f"  position row             : {position_rows[0]['status']} / "
              f"{position_rows[0]['exit_reason']}")
        print(f"  realized_pnl             : {position_rows[0]['realized_pnl']} "
              f"({DIM}NULL — nothing was ever held{RESET})")
        checks = {
            "exactly one cancel was sent": len(sim.cancels) == 1,
            "it cancelled the entry's broker order": sim.cancels == ["sim-broker-1"],
            "the sweep reported it": len(cancelled) == 1,
            "the local order reads canceled":
                str(after_row["status"]).lower() == "canceled",
            "the position row is retired as ENTRY_CANCELLED":
                position_rows[0]["exit_reason"] == ENTRY_CANCELLED,
            "no P&L was invented": position_rows[0]["realized_pnl"] is None,
        }
        for label, ok in checks.items():
            results.append(ok)
            print(f"  {'OK ' if ok else 'BAD'} {label}")

        sub("11d. Next cycle can re-price: the sequence advanced, the id is fresh")
        seq_after = entry_sequence(store, structural)
        next_coid = client_order_id(f"{structural}#{seq_after}", OPEN_INTENT, today_et())
        ok = seq_after == 1 and next_coid != coid
        results.append(ok)
        print(f"  entry sequence #{seq_now} -> #{seq_after}")
        print(f"  next client_order_id: {next_coid}")
        print(f"  {'OK ' if ok else 'BAD'} a fresh entry against fresh quotes no longer "
              f"collides with the cancelled one")

        sub("11e. Nothing is left working, so a second sweep is a no-op")
        again = await executor.cancel_unfilled_entries()
        results.append(len(again) == 0 and len(sim.cancels) == 1)
        print(f"  {'OK ' if not again else 'BAD'} second sweep cancelled "
              f"{len(again)} order(s); broker cancel calls still {len(sim.cancels)}")

        sub("11f. A fill that races the cancel must survive it")
        race_store = test_store("cancel_race")
        race_events = EventLog.start(race_store)
        race_risk = risk_rules.evaluate(
            structure=st, equity=100_000.0, open_positions=0,
            same_direction_positions=0, proposed_bias=1, corporate_actions=[],
            store=race_store, events=race_events, today=date(2026, 9, 1))
        race_sim = RestingEntryMCP([l.to_mcp_leg() for l in st.legs],
                                   race_risk.contracts, "-0.80", fill_on_confirm=True)
        race_exec = Executor(race_sim, None, race_store, race_events)
        race_exec.set_market_open(True)
        await race_exec.open_structure(st, race_risk, today=today_et())
        raced = await race_exec.cancel_unfilled_entries()
        race_positions = race_store.open_positions()
        checks = {
            "no cancel was sent for an order that filled": len(race_sim.cancels) == 0,
            "the sweep cancelled nothing": len(raced) == 0,
            "the position row is still OPEN": len(race_positions) == 1,
        }
        for label, ok in checks.items():
            results.append(ok)
            print(f"  {'OK ' if ok else 'BAD'} {label}")

        sub("11g. If open orders cannot be read, nothing is closed as missing")
        blind_store = test_store("blind_reconcile")
        blind_events = EventLog.start(blind_store)
        blind_risk = risk_rules.evaluate(
            structure=st, equity=100_000.0, open_positions=0,
            same_direction_positions=0, proposed_bias=1, corporate_actions=[],
            store=blind_store, events=blind_events, today=date(2026, 9, 1))
        blind_sim = RestingEntryMCP([l.to_mcp_leg() for l in st.legs],
                                    blind_risk.contracts, "-0.80", orders_readable=False)
        blind_exec = Executor(blind_sim, None, blind_store, blind_events)
        blind_exec.set_market_open(True)
        await blind_exec.open_structure(st, blind_risk, today=today_et())
        blind_report = await Reconciler(blind_sim, None, blind_store, blind_events).run()
        blind_open = blind_store.open_positions()
        checks = {
            "no position was closed as missing": len(blind_report.closed_missing) == 0,
            "the row is still OPEN": len(blind_open) == 1,
            "the pass recorded why it declined": bool(blind_report.errors),
        }
        for label, ok in checks.items():
            results.append(ok)
            print(f"  {'OK ' if ok else 'BAD'} {label}")

        sub("11h. A row the broker really has lost is still closed as missing")
        gone_report = await Reconciler(sim, None, store, events).run()
        # The only row for this setup is already CLOSED by the sweep, so nothing
        # is left to close: prove the path still works on a fresh orphan row.
        store.add_position(
            position_key=f"{structural}#9", symbol=st.symbol, structure=st.structure,
            contracts=1, credit=80.0, width=5.0, max_loss=420.0,
            expiry=st.expiry.isoformat(), dry_run=False,
            legs=[{"symbol": l.symbol, "side": l.side,
                   "position_intent": l.position_intent, "ratio_qty": 1,
                   "strike": l.contract.strike, "right": l.contract.right}
                  for l in st.legs],
            entry_order_id="oaa-open-orphaned", detail={"dry_run": False})
        orphan_report = await Reconciler(sim, None, store, events).run()
        orphan = store.query("SELECT * FROM positions WHERE position_key=?",
                             (f"{structural}#9",))[0]
        ok = (len(orphan_report.closed_missing) == 1
              and orphan["exit_reason"] == RECONCILED_MISSING)
        results.append(ok)
        print(f"  {'OK ' if ok else 'BAD'} an orphan row with no working order behind "
              f"it is closed as {orphan['exit_reason']}")
        print(f"      {DIM}the guard narrows the close-missing rule, it does not "
              f"disable it{RESET}")
    finally:
        object.__setattr__(ENV, "dry_run", was_dry)
        object.__setattr__(EXECUTION, "requote_wait_seconds", was_wait)

    sub("11i. Recorded event log")
    show_events(store, events.cycle_id,
                (Stage.EXECUTION, Stage.POSITION_MANAGEMENT, Stage.ERROR))

    notes.append(
        "the position row is written at submission, before any fill; that is why "
        "an unfilled entry has to be cancelled by the cycle that placed it")
    return Outcome("11 unfilled entry", all(results),
                   f"{sum(results)}/{len(results)} assertions held; the entry was "
                   f"cancelled once, never closed while live, and re-entry is clean",
                   notes)
