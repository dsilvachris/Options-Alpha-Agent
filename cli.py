#!/usr/bin/env python
"""
Command line interface for the Options Alpha Agent.

    .venv/bin/python cli.py doctor      Verify .env, MCP session and tool surface
    .venv/bin/python cli.py scan        Run one full scan cycle, printing each stage
    .venv/bin/python cli.py loop        Run continuously at the configured interval
    .venv/bin/python cli.py scores      Section 4.4 score-distribution report
    .venv/bin/python cli.py reconcile   Reconcile local positions against Alpaca
    .venv/bin/python cli.py clear-simulated  Close all DRY_RUN positions
    .venv/bin/python cli.py publish     Export the dashboard as static JSON
    .venv/bin/python cli.py flatten     Close all open positions
    .venv/bin/python cli.py dashboard   Serve the financial-terminal dashboard
    .venv/bin/python cli.py ledger      Section 8.1/8.2/8.3 monitoring report
    .venv/bin/python cli.py cards       Print recorded decision cards
    .venv/bin/python cli.py mcp-log     Print the recorded MCP call stream
    .venv/bin/python cli.py config      Print the active configuration
"""
from __future__ import annotations

import bootstrap  # noqa: F401  - must precede `logging.*` submodule imports

import argparse
import asyncio
import json
import sys
from collections import Counter

import config
from agent import Agent
from config import ENV, SCORING, fmt_et, now_et
from evaluation.scorer import CHECK_NAMES
from execution.orders import Executor
from logging.events import EventLog, Stage
from logging.store import get_store
from monitoring import baseline as baseline_mod
from monitoring import ledger as ledger_mod
from monitoring import outcomes as outcomes_mod
from perception.market import MarketData
from execution.reconcile import SimulatedPositionsPresent
from perception.mcp_client import AlpacaMCP, MCPError

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, RED, AMBER, CYAN = "\033[32m", "\033[31m", "\033[33m", "\033[36m"


def rule(title: str = "") -> None:
    print(f"\n{BOLD}{'=' * 78}{RESET}")
    if title:
        print(f"{BOLD}{title}{RESET}")
        print(f"{BOLD}{'=' * 78}{RESET}")


def banner() -> None:
    mode = f"{AMBER}DRY_RUN{RESET}" if ENV.dry_run else f"{RED}LIVE{RESET}"
    print(f"{BOLD}OPTIONS ALPHA AGENT{RESET}  mode={mode}  endpoint={ENV.alpaca_base_url}")
    print(f"{DIM}market time: {now_et().strftime('%Y-%m-%d %H:%M:%S %Z')} "
          f"(all dates and DTE are America/New_York){RESET}")
    if not ENV.configured:
        print(f"{RED}ALPACA_API_KEY / ALPACA_SECRET_KEY are not set. "
              f"Copy .env.example to .env and fill them in.{RESET}")


def require_credentials() -> None:
    if not ENV.configured:
        print(f"{RED}Missing credentials. Create .env from .env.example.{RESET}")
        sys.exit(2)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
async def cmd_doctor(_: argparse.Namespace) -> int:
    banner()
    require_credentials()
    rule("MCP CONNECTIVITY")
    store = get_store()
    mcp = AlpacaMCP(store, EventLog.start(store))
    try:
        await mcp.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"{RED}Could not start the Alpaca MCP server: {exc}{RESET}")
        return 1

    tools = mcp.tool_names()
    print(f"{GREEN}Connected.{RESET} {len(tools)} tools exposed by the MCP server.")

    market = MarketData(mcp)
    from perception.market import REQUIRED_TOOLS

    missing = [t for t in REQUIRED_TOOLS if t not in tools]
    for tool in REQUIRED_TOOLS:
        mark = f"{GREEN}ok{RESET}" if tool in tools else f"{RED}MISSING{RESET}"
        print(f"  {tool:<38} {mark}")
    if missing:
        print(f"\n{RED}Missing required tools: {', '.join(missing)}{RESET}")
        await mcp.disconnect()
        return 1

    rule("ACCOUNT (via get_account_info)")
    try:
        account = await market.account()
        print(f"  equity              ${account.equity:,.2f}")
        print(f"  cash                {account.cash}")
        print(f"  buying power        {account.buying_power}")
        print(f"  options level       {account.options_approved_level}")
        if account.options_approved_level is not None and account.options_approved_level < 3:
            print(f"{AMBER}  Multi-leg credit spreads normally require options level 3."
                  f" This account reports level {account.options_approved_level}.{RESET}")
    except Exception as exc:  # noqa: BLE001
        print(f"{RED}  Failed: {exc}{RESET}")
        await mcp.disconnect()
        return 1

    rule("MARKET CLOCK (via get_clock)")
    print(f"  {await market.clock()}")
    await mcp.disconnect()
    print(f"\n{GREEN}Doctor checks passed.{RESET}")
    return 0


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------
async def cmd_scan(args: argparse.Namespace) -> int:
    banner()
    require_credentials()
    rule("SCAN CYCLE")
    async with Agent(echo=True) as agent:
        result = await agent.run_cycle()

    rule("CYCLE SUMMARY")
    print(f"cycle id: {result.cycle_id}")
    print(f"equity:   {'—' if result.equity is None else f'${result.equity:,.2f}'}")
    print(f"closed:   {len(result.closed_positions)} position(s)")
    print(f"expired:  {len(result.expired_watches)} watch item(s)")
    if result.cancelled_entries:
        print(f"cancelled:{len(result.cancelled_entries)} unfilled entry order(s) "
              f"— none left resting at the broker")
    if result.halted:
        print(f"{RED}circuit breaker: ACTIVE — no new orders today{RESET}")
    if result.error:
        print(f"{RED}error: {result.error}{RESET}")

    for outcome in result.outcomes:
        colour = {"TRADE": GREEN, "WATCH": AMBER, "REJECT": DIM, "ERROR": RED}.get(
            outcome.state, ""
        )
        print(f"\n{colour}{outcome.symbol}: {outcome.state}"
              f"{f' ({outcome.score}/100)' if outcome.score else ''}{RESET}")
        if outcome.card:
            for line in outcome.card.splitlines():
                print(f"  {line}")
        elif outcome.reason:
            print(f"  {outcome.reason}")

        if args.verbose and outcome.detail.get("checks"):
            print(f"  {DIM}--- check breakdown ---{RESET}")
            for check in outcome.detail["checks"]:
                mark = f"{GREEN}PASS{RESET}" if check["passed"] else f"{RED}FAIL{RESET}"
                gate = f" {CYAN}[HARD GATE]{RESET}" if check["hard_gate"] else ""
                print(f"  #{check['id']} {check['name']:<22} {mark} "
                      f"{check['awarded']:>2}/{check['points']:<2}{gate}")
                print(f"       measured: {check['measured']}")
    return 0 if not result.error else 1


# ---------------------------------------------------------------------------
# loop
# ---------------------------------------------------------------------------
async def cmd_loop(args: argparse.Namespace) -> int:
    banner()
    require_credentials()
    print(f"Scanning every {config.LOOP.scan_interval_minutes} minutes "
          f"({config.LOOP.closed_market_sleep_minutes} while closed). Ctrl-C to stop.")
    publish, push = resolve_loop_publish_flags(args)
    if publish:
        print(f"Publishing a static snapshot after each cycle; committing only on "
              f"a material change, and {'pushing' if push else 'NOT pushing (--no-push)'}.")
    async with Agent(echo=True) as agent:
        await agent.run_forever(publish=publish, push=push)
    return 0


# ---------------------------------------------------------------------------
# scores (Section 4.4)
# ---------------------------------------------------------------------------
def cmd_scores(args: argparse.Namespace) -> int:
    """
    Section 4.4 calibration report.

    Shows the distribution of scores actually produced, so thresholds can be
    adjusted against observed data rather than assumptions made in advance.
    """
    store = get_store()
    session_only = not args.all_sessions
    decisions = [d for d in store.all_decisions(session_only=session_only)
                 if d["score"] is not None]
    counts = store.decision_session_counts()
    rule("SCORE DISTRIBUTION (Section 4.4 calibration)")
    scope = "IN-SESSION ONLY" if session_only else "ALL SESSIONS"
    print(f"Scope: {scope}"
          + (f"  ({counts['out_of_session']} out-of-session and "
             f"{counts['unrecorded']} unrecorded excluded; --all-sessions to include)"
             if session_only else ""))
    if session_only and not decisions and (counts["out_of_session"] or counts["unrecorded"]):
        print(f"\n{AMBER}No in-session scored opportunities yet.{RESET} "
              f"Out-of-session candidates are priced off wide after-hours quotes, "
              f"so they are\nexcluded by default — the real calibration run is "
              f"during market hours. Use --all-sessions to see them anyway.")
        return 0

    if not decisions:
        print("No scored opportunities recorded yet. Run `cli.py scan` first.")
        return 0

    scores = [int(d["score"]) for d in decisions]
    print(f"Observations: {len(scores)}   "
          f"min {min(scores)}   max {max(scores)}   "
          f"mean {sum(scores)/len(scores):.1f}")
    print(f"Bands: TRADE >= {SCORING.trade_band}, "
          f"WATCH {SCORING.watch_band}-{SCORING.trade_band - 1}, "
          f"REJECT < {SCORING.watch_band}\n")

    buckets = Counter((s // 10) * 10 for s in scores)
    peak = max(buckets.values()) if buckets else 1
    for low in range(0, 101, 10):
        count = buckets.get(low, 0)
        band = ("TRADE " if low >= SCORING.trade_band
                else "WATCH " if low >= SCORING.watch_band else "REJECT")
        bar = "█" * int(40 * count / peak) if count else ""
        colour = (GREEN if low >= SCORING.trade_band
                  else AMBER if low >= SCORING.watch_band else DIM)
        print(f"  {low:>3}-{min(low+9,100):<3} {band} {count:>4} {colour}{bar}{RESET}")

    print()
    by_state = Counter(d["state"] for d in decisions)
    for state, count in by_state.most_common():
        print(f"  {state:<8} {count}")

    rule("CHECK PASS RATES (what is actually gating)")
    passed: Counter = Counter()
    seen: Counter = Counter()
    skipped: Counter = Counter()
    for decision in decisions:
        try:
            detail = json.loads(decision["detail"] or "{}")
        except (TypeError, ValueError):
            continue
        for check in detail.get("checks", []) or []:
            cid = check["id"]
            if not check.get("evaluable", True):
                skipped[cid] += 1
                continue
            seen[cid] += 1
            if check["passed"]:
                passed[cid] += 1
    if not seen:
        print("No check-level detail recorded.")
    for cid in sorted(CHECK_NAMES):
        total = seen.get(cid, 0)
        skip = skipped.get(cid, 0)
        if not total and not skip:
            continue
        ok = passed.get(cid, 0)
        gate = " [HARD GATE]" if cid in SCORING.hard_gates else ""
        note = (f"  {AMBER}[{skip} NOT EVALUABLE — excluded from scoring]{RESET}"
                if skip else "")
        print(f"  #{cid} {CHECK_NAMES[cid]:<22} {ok:>4}/{total:<4} passed"
              f"  ({SCORING.points[cid]:>2} pts){gate}{note}")

    rule("PER-CANDIDATE MEASURED VALUES")
    print(f"{DIM}Values behind checks 5, 6 and 7. Thresholds: credit/width >= "
          f"{SCORING.min_credit_to_width}, |delta| <= {SCORING.max_short_delta}, "
          f"bid-ask <= {SCORING.max_spread_pct_of_credit:.0%} of credit, "
          f"OI >= {SCORING.min_open_interest}.{RESET}")
    hdr = (f"  {'TIME (ET)':<9}{'SYM':<5}{'STRUCTURE':<24}{'SCORE':>6}{'STATE':>8}"
           f"{'DELTA':>8}{'CR/WID':>8}{'BA%CR':>8}{'CREDIT':>9}{'WIDTH':>8}{'OI':>7}")
    print(hdr); print("  " + "-" * (len(hdr) - 2))

    for decision in decisions[-40:]:
        try:
            detail = json.loads(decision["detail"] or "{}")
        except (TypeError, ValueError):
            detail = {}
        if not detail.get("structure"):
            continue
        delta = detail.get("short_delta")
        cw = detail.get("credit_to_width")
        ba = detail.get("spread_pct_of_credit")
        credit = detail.get("credit")
        width = detail.get("width")
        oi = detail.get("min_open_interest")

        def mark(value, limit, higher_is_better):
            if value is None:
                return f"{DIM}    —   {RESET}"
            ok = value >= limit if higher_is_better else value <= limit
            return f"{GREEN if ok else RED}{value:>8.3f}{RESET}"

        print(f"  {fmt_et(decision['ts'], '%H:%M:%S'):<9}"
              f"{decision['symbol']:<5}{(detail.get('structure') or '')[:23]:<24}"
              f"{(decision['score'] if decision['score'] is not None else 0):>6}"
              f"{decision['state']:>8}"
              f"{mark(delta, SCORING.max_short_delta, False)}"
              f"{mark(cw, SCORING.min_credit_to_width, True)}"
              f"{mark(ba, SCORING.max_spread_pct_of_credit, False)}"
              f"{('—' if credit is None else f'${credit:,.2f}'):>9}"
              f"{('—' if width is None else f'${width*100:,.0f}'):>8}"
              f"{(oi if oi is not None else '—'):>7}")

    print(f"\n{DIM}Section 4.4: if genuine opportunities cluster below the execution "
          f"band, or almost everything clears, adjust thresholds in config.py "
          f"against this observed distribution.{RESET}")
    return 0


# ---------------------------------------------------------------------------
# flatten
# ---------------------------------------------------------------------------
async def cmd_flatten(args: argparse.Namespace) -> int:
    banner()
    require_credentials()
    store = get_store()
    open_positions = store.open_positions()

    rule("FLATTEN — CLOSE ALL OPEN POSITIONS")
    print(f"Recorded open positions: {len(open_positions)}")
    for position in open_positions:
        print(f"  {position['symbol']:<6} {position['structure']:<26} "
              f"x{position['contracts']} credit ${float(position['credit']):.2f}")

    if not ENV.dry_run and not args.yes:
        answer = input(f"\n{RED}LIVE MODE — close these positions? [y/N] {RESET}").strip().lower()
        if answer != "y":
            print("Aborted.")
            return 1

    events = EventLog.start(store, echo=True)
    async with Agent(store=store, echo=True) as agent:
        agent.mcp.bind_events(events)
        executor = Executor(agent.mcp, agent.market, store, events)
        result = await executor.flatten_all()

    print(json.dumps(result, indent=2, default=str))
    return 0


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------
def cmd_ledger(args: argparse.Namespace) -> int:
    store = get_store()
    rule()
    print(ledger_mod.build(store, session_only=not args.all_sessions).render())
    rule()
    print(outcomes_mod.build(store).render())
    rule()
    print(baseline_mod.build(store).render())
    return 0


def cmd_cards(args: argparse.Namespace) -> int:
    store = get_store()
    rows = store.recent_decisions(args.limit)
    if not rows:
        print("No decisions recorded yet.")
        return 0
    for row in reversed(rows):
        colour = {"TRADE": GREEN, "WATCH": AMBER, "REJECT": DIM,
                  "EXPIRED": DIM, "EXIT": CYAN}.get(row["state"], "")
        print(f"\n{DIM}{fmt_et(row['ts'])} · cycle {row['cycle_id']}{RESET}")
        print(f"{colour}{row['card'] or row['reason']}{RESET}")
    return 0


def cmd_mcp_log(args: argparse.Namespace) -> int:
    store = get_store()
    calls = store.recent_mcp_calls(args.limit)
    rule("MCP CALL STREAM")
    if not calls:
        print("No MCP calls recorded yet.")
        return 0
    for call in reversed(calls):
        mark = f"{GREEN}ok {RESET}" if call["ok"] else f"{RED}ERR{RESET}"
        print(f"{DIM}{fmt_et(call['ts'], '%H:%M:%S')}{RESET} {mark} {CYAN}{call['tool']:<34}{RESET} "
              f"{call['latency_ms']:>7.0f}ms  {(call['arguments'] or '')[:70]}")
        summary = call["response_summary"] if call["ok"] else (call["error"] or "")
        if summary:
            print(f"    {DIM}{summary[:150]}{RESET}")
    rule("BY TOOL")
    for stat in store.mcp_call_stats():
        print(f"  {stat['tool']:<34} calls {stat['calls']:>4}  ok {stat['ok_calls']:>4}  "
              f"avg {stat['avg_latency_ms']}ms")
    return 0


def resolve_publish_flags(args: argparse.Namespace) -> tuple[bool, bool]:
    """
    Resolve `cli.py publish` flags to (commit, push).

    --push implies --commit: pushing without committing is not something git can
    do, so asking to push is asking to commit.
    """
    push = bool(getattr(args, "push", False))
    commit = bool(getattr(args, "commit", False)) or push
    return commit, push


def resolve_loop_publish_flags(args: argparse.Namespace) -> tuple[bool, bool]:
    """
    Resolve `cli.py loop --publish` flags to (publish, push).

    Pushing is the DEFAULT when publishing in the loop. The point of publishing
    every cycle is that the hosted dashboard refreshes during the session; a
    commit that never leaves the machine does not do that. `--no-push` opts out
    for a local-only run.
    """
    publish = bool(getattr(args, "publish", False))
    push = publish and not bool(getattr(args, "no_push", False))
    return publish, push


def cmd_publish(args: argparse.Namespace) -> int:
    """Export the dashboard as static JSON for hosting."""
    from dashboard import export as export_mod
    from dashboard import git_publish

    banner()
    store = get_store()
    events = EventLog.start(store, echo=False)
    rule("PUBLISH — STATIC DASHBOARD SNAPSHOT")

    try:
        result = export_mod.publish(store)
    except export_mod.RedactionError as exc:
        print(f"{RED}{exc}{RESET}")
        print(f"\n{RED}Nothing was written.{RESET}")
        return 2

    print(result.render())
    events.emit(
        Stage.EXECUTION,
        f"Published static dashboard snapshot ({len(result.files)} files, "
        f"digest {result.digest[:12]})",
        payload={"digest": result.digest, "generated_at": result.generated_at,
                 "files": [str(p.name) for p, _ in result.files]},
    )

    commit, push = resolve_publish_flags(args)
    if commit:
        rule("GIT")
        git = git_publish.commit_and_push(
            [p for p, _ in result.files],
            f"dashboard: snapshot {result.generated_at}", events, push=push)
        print(f"  {git.reason}")
        if not git.committed and "not a git repository" in git.reason:
            print(f"  {AMBER}Run `git init` and add a remote to enable publishing "
                  f"to the hosted dashboard.{RESET}")
    else:
        print(f"\n{DIM}Not committing (pass --commit to commit, "
              f"--push to commit and push).{RESET}")
    return 0


def cmd_clear_simulated(args: argparse.Namespace) -> int:
    """Retire simulated positions so they stop occupying live portfolio slots."""
    from execution.reconcile import clear_simulated_positions, open_simulated_positions

    banner()
    store = get_store()
    events = EventLog.start(store, echo=True)

    rule("CLEAR SIMULATED POSITIONS")
    pending = open_simulated_positions(store)
    if not pending:
        print("No open simulated positions. Nothing to clear.")
        return 0

    print(f"{len(pending)} open simulated (DRY_RUN) position(s):")
    for position in pending:
        print(f"  {position['symbol']:<6} {position['structure']:<26} "
              f"x{position['contracts']}  notional credit "
              f"${float(position['credit']):.2f}  expiry {position['expiry']}")
    print(f"\n{DIM}These were never transmitted to the broker. Their decisions and "
          f"events stay in the log.{RESET}")

    if not args.yes:
        answer = input("\nClear them? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return 1

    cleared = clear_simulated_positions(store, events)
    rule("RESULT")
    print(f"Cleared {len(cleared)} position(s) with reason DRY_RUN_CLEARED.")
    print(f"Open positions remaining: {len(store.open_positions())} "
          f"({len(store.open_positions(include_dry_run=False))} live)")
    print(f"Decisions still in the log: "
          f"{len(store.all_decisions())} (untouched)")
    return 0


async def cmd_reconcile(_: argparse.Namespace) -> int:
    banner()
    require_credentials()
    rule("RECONCILIATION — BROKER IS SOURCE OF TRUTH")
    store = get_store()
    events = EventLog.start(store, echo=True)

    print(f"{DIM}before: {len(store.open_positions())} open position(s) in the store{RESET}")
    # Connect directly rather than through Agent(), whose start() already runs a
    # reconciliation pass — going through it would run and print the pass twice.
    from execution.reconcile import Reconciler
    from perception.market import MarketData
    from perception.mcp_client import AlpacaMCP

    mcp = AlpacaMCP(store, events)
    await mcp.connect()
    try:
        market = MarketData(mcp)
        await market.verify_tools()
        report = await Reconciler(mcp, market, store, events).run()
    finally:
        await mcp.disconnect()

    rule("RESULT")
    print(report.render())
    print(f"\nafter: {len(store.open_positions())} open position(s) in the store")
    return 1 if report.errors else 0


async def cmd_selftest(args: argparse.Namespace) -> int:
    from tests.harness import run_all

    return await run_all(args.scenario)


def cmd_config(_: argparse.Namespace) -> int:
    print(json.dumps(config.summary(), indent=2))
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    from dashboard.server import serve

    print(f"Dashboard: http://{args.host or config.DASHBOARD.host}:"
          f"{args.port or config.DASHBOARD.port}")
    serve(args.host, args.port)
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """The CLI parser, exposed so tests can assert on flag resolution."""
    parser = argparse.ArgumentParser(
        prog="cli.py", description="Options Alpha Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="verify .env, MCP session and tool surface")

    scan = sub.add_parser("scan", help="run one full scan cycle end to end")
    scan.add_argument("-v", "--verbose", action="store_true",
                      help="print the full check breakdown per symbol")

    loop_p = sub.add_parser("loop", help="run continuously at the configured interval")
    loop_p.add_argument("--publish", action="store_true",
                        help="publish a static snapshot after each cycle, "
                             "committing only when it materially changed")
    loop_p.add_argument("--no-push", action="store_true",
                        help="commit each publish but do not push (default is to "
                             "push, so the hosted dashboard refreshes)")
    scores = sub.add_parser("scores", help="Section 4.4 score-distribution report")
    scores.add_argument("--all-sessions", action="store_true",
                        help="include decisions made outside market hours")

    flatten = sub.add_parser("flatten", help="close all open positions")
    flatten.add_argument("-y", "--yes", action="store_true", help="skip confirmation")

    ledger_p = sub.add_parser("ledger", help="Section 8 monitoring report")
    ledger_p.add_argument("--all-sessions", action="store_true",
                          help="include decisions made outside market hours")

    cards = sub.add_parser("cards", help="print recorded decision cards")
    cards.add_argument("-n", "--limit", type=int, default=20)

    mcp_log = sub.add_parser("mcp-log", help="print the recorded MCP call stream")
    mcp_log.add_argument("-n", "--limit", type=int, default=40)

    sub.add_parser("config", help="print the active configuration")
    sub.add_parser("reconcile",
                   help="reconcile local positions against the broker")

    publish_p = sub.add_parser(
        "publish", help="export the dashboard as static JSON for hosting")
    publish_p.add_argument("--commit", action="store_true",
                           help="git commit the published files if they changed")
    publish_p.add_argument("--push", action="store_true",
                           help="commit and push (implies --commit)")

    clear_sim = sub.add_parser(
        "clear-simulated",
        help="close all DRY_RUN positions (reason DRY_RUN_CLEARED)")
    clear_sim.add_argument("-y", "--yes", action="store_true",
                           help="skip confirmation")

    selftest = sub.add_parser(
        "selftest", help="dry-run harness for the exit/order/failure paths")
    selftest.add_argument("--scenario", type=int, default=None, choices=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
                          help="run only one scenario")

    dash = sub.add_parser("dashboard", help="serve the dashboard")
    dash.add_argument("--host", default=None)
    dash.add_argument("--port", type=int, default=None)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    sync = {
        "scores": cmd_scores, "ledger": cmd_ledger, "cards": cmd_cards,
        "mcp-log": cmd_mcp_log, "config": cmd_config, "dashboard": cmd_dashboard,
        "clear-simulated": cmd_clear_simulated, "publish": cmd_publish,
    }
    if args.command in sync:
        return sync[args.command](args)

    async_map = {
        "doctor": cmd_doctor, "scan": cmd_scan,
        "loop": cmd_loop, "flatten": cmd_flatten, "selftest": cmd_selftest,
        "reconcile": cmd_reconcile,
    }
    try:
        return asyncio.run(async_map[args.command](args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except SimulatedPositionsPresent as exc:
        print(f"\n{RED}REFUSING TO START LIVE — "
              f"{len(exc.positions)} simulated position(s) are still open.{RESET}\n")
        print("A DRY_RUN position occupies a real portfolio slot: check 9 "
              "(portfolio fit) and\nthe risk gate both count it, so these would "
              "silently block every real trade\nwith exposure that does not exist.\n")
        for position in exc.positions:
            print(f"  {position['symbol']:<6} {position['structure']:<26} "
                  f"x{position['contracts']}  notional credit "
                  f"${float(position['credit']):.2f}  expiry {position['expiry']}")
        print(f"\n{AMBER}Run this first:{RESET}\n"
              f"    .venv/bin/python cli.py clear-simulated\n\n"
              f"Their decisions and events stay in the log; only the position "
              f"rows are retired.")
        return 2
    except MCPError as exc:
        print(f"{RED}MCP error: {exc}{RESET}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
