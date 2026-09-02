# Options Alpha Agent

An autonomous, volatility-aware options trading agent running on Alpaca's **paper**
trading environment. It reads a two-factor market regime (implied volatility
condition + trend condition), selects a defined-risk options structure from a
fixed matrix, grades every candidate against a 9-check scored checklist, passes
survivors through an independent risk gate, and records every decision — taken or
declined — as an inspectable decision card.

Built for the Alpaca AI Trading Agents Hackathon, Options Alpha Agents track.
Implements `docs/implementation.md` Sections 1–12.

Two outputs are produced at all times:

1. A trading record — positions, orders, P&L.
2. A complete decision log — every opportunity evaluated, scored and resolved,
   including the rejections.

---

## Alpaca MCP integration

**Every interaction with Alpaca goes through Alpaca's official MCP server.** There
is no REST fallback anywhere in the agent. `perception/` and `execution/` import
no `alpaca-py`, `requests` or `httpx` — account state, positions, price bars,
option chains, order placement, order status and position closing are all MCP
tool calls.

How it works:

* `perception/mcp_client.py` opens a **stdio MCP session** against
  `uvx alpaca-mcp-server` using the official `mcp` Python SDK
  (`ClientSession` + `stdio_client`).
* `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are read from `.env` and injected into
  the subprocess environment. They are never written to any file in this repo.
* The session is **held open across scan cycles** by an `AsyncExitStack`. A dead
  subprocess is detected on the next call, torn down, and re-established with
  exponential backoff, then the call is retried — the overnight loop survives a
  server death.
* **Every tool call is logged** as an event: tool name, arguments, response
  summary, latency and timestamp, stored in the same SQLite event store as the
  pipeline stages. The dashboard's **MCP Activity** panel renders this as a live
  call stream.

Verify the integration end to end:

```bash
.venv/bin/python cli.py doctor    # connects, lists tools, reads the account
.venv/bin/python cli.py mcp-log   # replays the recorded MCP call stream
```

MCP tools used: `get_account_info`, `get_all_positions`, `get_open_position`,
`get_clock`, `get_stock_bars`, `get_stock_latest_trade`, `get_option_contracts`,
`get_option_chain`, `get_option_snapshot`, `get_corporate_action_announcements`,
`get_portfolio_history`, `place_option_order`, `get_orders`, `get_order_by_id`,
`get_order_by_client_id`, `cancel_order_by_id`, `close_position`,
`close_all_positions`.

---

## Architecture: the perception–decision–action loop

Seven discrete stages, each emitting a timestamped backend event with a payload.
No stage is skipped, and the dashboard renders recorded state only — it never
simulates progress in the front end.

```
                    ┌──────────────── Alpaca MCP server (uvx alpaca-mcp-server) ───────────────┐
                    │  one long-lived stdio session · every call logged with latency           │
                    └────────▲───────────────────────────────────────────────────▲─────────────┘
                             │                                                   │
  ┌──────────────┐    ┌──────┴───────┐   ┌──────────────┐   ┌──────────────┐   ┌─┴────────────┐
  │ 1 PERCEPTION │───▶│ 2 SIGNALS    │──▶│ 3 DECISION   │──▶│ 4 EVALUATION │──▶│ 5 RISK GATE  │
  │ perception/  │    │ signals/     │   │ decision/    │   │ evaluation/  │   │ risk/        │
  │ account,     │    │ IV vs RV,    │   │ matrix picks │   │ 9 checks,    │   │ sizing,      │
  │ positions,   │    │ MA10 vs MA30 │   │ structure;   │   │ 3 hard gates │   │ limits,      │
  │ bars, chains │    │              │   │ strikes+expiry│  │ score bands  │   │ events, exits│
  └──────────────┘    └──────────────┘   └──────────────┘   └──────────────┘   └──────┬───────┘
                                                                                      │ approved only
                             ┌────────────────────────────────────────────────────────▼───────┐
                             │ 6 EXECUTION execution/ — multi-leg order, client_order_id       │
                             │   idempotency, DRY_RUN guard, exit monitoring                   │
                             └────────────────────────────────┬───────────────────────────────┘
                                                              ▼
                             ┌──────────────────────────────────────────────────────────────┐
                             │ 7 LOGGING & PRESENTATION  logging/ · monitoring/ · dashboard/ │
                             │   events, decision cards, ledger, baselines, terminal UI      │
                             └──────────────────────────────────────────────────────────────┘
```

Full flow: market scan → regime detection → strategy selection → opportunity
scoring → risk evaluation → TRADE / WATCH / REJECT → execution → decision card →
position and decision-quality monitoring.

### Strategy selection matrix (Section 3.3)

| Volatility | Trend | Structure |
|---|---|---|
| Premium elevated | Clear uptrend | Bull put credit spread |
| Premium elevated | Clear downtrend | Bear call credit spread |
| Premium elevated | Range-bound | Iron condor |
| Premium depressed | Any | Stand aside; a small defined-risk directional debit spread only if trend conviction is high |

The matrix determines **eligibility only**. It does not authorize a trade.

### Scored checklist (Section 4)

| # | Check | Points | |
|---|---|---|---|
| 1 | Premium rich — ATM IV ≥ 1.2 × 20-day realized vol | 15 | |
| 2 | Volatility stable — ATM IV ≤ 1.15 × its 3-session average | 10 | tri-state |
| 3 | Trend clarity — MA separation > 0.5% of price | 15 | |
| 4 | Directional agreement — structure bias matches trend | 10 | |
| 5 | Credit quality — credit / width ≥ 0.30 | 15 | |
| 6 | Probability profile — short-strike delta ≤ 0.30 | 10 | |
| 7 | Liquidity — bid-ask ≤ 25% of credit, OI ≥ 100 per leg | 10 | **hard gate** |
| 8 | Event clear — no scheduled event before expiry | 10 | **hard gate** |
| 9 | Portfolio fit — exposure and count inside limits | 5 | **hard gate** |

Hard gates are evaluated **first** and short-circuit: failing any one rejects the
opportunity outright, before the score sum is ever reached. Bands: **≥80 TRADE**,
**60–79 WATCH**, **<60 REJECT**.

Checks are tri-state: passed, failed, or **not evaluable**. Check 2 needs three
prior sessions of ATM IV before its trailing average means anything; until then
its 10 points leave both the numerator and the denominator and the score is
rescaled over the available points, so the bands keep their meaning. Every card
states which checks were skipped and that the score was rescaled.

---

## Setup

Requires Python 3.11+ and an Alpaca **paper** account with options trading
enabled (level 3 for multi-leg spreads).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# edit .env and fill in ALPACA_API_KEY and ALPACA_SECRET_KEY

.venv/bin/python cli.py doctor
```

`uvx` (shipped by the `uv` package in `requirements.txt`) launches the MCP
server; it downloads `alpaca-mcp-server` on first run.

All commands use `.venv/bin/python` explicitly — no activated shell is assumed.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `ALPACA_API_KEY` | *(empty)* | Paper API key. Required. |
| `ALPACA_SECRET_KEY` | *(empty)* | Paper secret. Required. |
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets` | Paper endpoint. The agent refuses to start against a non-paper URL. |
| `DRY_RUN` | `true` | When true the full loop runs and logs decisions but **places no orders**. Live placement requires `DRY_RUN=false` explicitly. |

`.env` is gitignored and is never written by the agent.

---

## Commands

```bash
.venv/bin/python cli.py doctor        # verify .env, MCP session, tool surface, account
.venv/bin/python cli.py scan          # one full scan cycle, printing every pipeline stage
.venv/bin/python cli.py scan -v       # ... plus the full per-check score breakdown
.venv/bin/python cli.py loop          # run continuously every 15 minutes
.venv/bin/python cli.py scores        # Section 4.4 score-distribution calibration report
.venv/bin/python cli.py reconcile     # reconcile local positions against the broker
.venv/bin/python cli.py clear-simulated  # retire DRY_RUN positions before going live
.venv/bin/python cli.py selftest      # dry-run harness for exit/order/failure paths
.venv/bin/python cli.py flatten       # close all open positions (end-of-window)
.venv/bin/python cli.py ledger        # Section 8 activity, outcomes and baselines
.venv/bin/python cli.py cards         # recorded decision cards
.venv/bin/python cli.py mcp-log       # recorded MCP call stream
.venv/bin/python cli.py config        # active configuration
.venv/bin/python cli.py dashboard     # financial-terminal dashboard on :8787
.venv/bin/python cli.py publish       # export the dashboard as static JSON (Vercel)
.venv/bin/python cli.py publish --push  # ... and commit + push it (implies --commit)
.venv/bin/python cli.py loop --publish  # scan continuously; publish, commit and push each cycle
```

Going live is a deliberate, explicit act:

```bash
.venv/bin/python cli.py clear-simulated   # retire simulated positions first
DRY_RUN=false .venv/bin/python cli.py loop
```

The agent **refuses to start** with `DRY_RUN=false` while simulated positions are
open, and tells you to run `clear-simulated`. A simulated position occupies a real
portfolio slot — check 9 and the risk gate both count it — so leaving one in place
would silently block every real trade with exposure that does not exist.

---

## Safety properties

* **Paper only.** `config.ENV.assert_paper()` refuses any non-paper base URL.
* **DRY_RUN defaults to true.** Orders are logged, never transmitted, until
  `DRY_RUN=false` is set explicitly.
* **Defined risk only.** `decision/structure.py` asserts that every short leg is
  covered by a long leg in the same expiry and right before a structure is
  returned, and the risk gate re-checks it. There is no code path that can
  produce a naked position.
* **The risk gate cannot be bypassed.** A score of 100 is still refused if any
  limit is breached. `execution/` refuses to place anything the gate did not
  approve.
* **Order idempotency, without blocking re-entry.** Every order carries a
  deterministic `client_order_id` derived from position identity, intent, date
  and an **entry sequence**. A retry after a timeout resolves to the original
  order and Alpaca rejects the duplicate server-side; but once a position has
  closed, the sequence advances so the same setup can legitimately be traded
  again later the same day.
* **No order is submitted while the market is closed.** The session state comes
  from `get_clock` on every cycle and arms two independent gates: the risk gate
  refuses new entries with reason `MARKET_CLOSED`, and the order layer refuses
  *all* submission — entries and exits alike, since exits bypass the risk gate.
  A stop firing overnight off a stale mark cannot queue an order into a closed
  book. The full pipeline still runs, so the decision record and score
  distribution keep accumulating out of hours; exit triggers are still evaluated
  and logged as `EXIT_DEFERRED` and acted on at the next open. The manual
  `flatten` command can force through.
* **Broker-truth reconciliation.** At startup and at the top of every scan
  cycle, positions and orders are reconciled against Alpaca before any new
  opportunity is evaluated. Untracked broker positions are adopted so the exit
  rules manage them; local positions the broker no longer holds are closed as
  `RECONCILED_MISSING` — unless their entry order is still working, which is
  read from the broker's open ORDERS, not just its positions. A locally `FAILED`
  order is re-checked at the broker before being treated as never sent.
* **Daily circuit breaker.** All new orders halt after 10 order attempts or 3%
  account drawdown in one day. The halt is recorded as an event and persists for
  the rest of the day.

---

## Configuration

`config.py` is the single source of truth. No threshold, watchlist member, size
cap or interval is hardcoded anywhere else.

| Setting | Value |
|---|---|
| Market timezone | America/New_York (all DTE, sessions, scheduling) |
| Watchlist | SPY, QQQ, IWM |
| Expiry selection | 1–3 DTE, expiries on or before 2026-09-04 (inclusive) |
| MA periods | 10 / 30 on daily bars |
| Trend clarity threshold | MA separation > 0.5% of price |
| Premium rich (check 1) | ATM IV ≥ 1.2 × 20-day realized vol |
| Volatility stable (check 2) | ATM IV ≤ 1.15 × its 3-day average |
| Credit quality (check 5) | credit / width ≥ 0.30 |
| Probability (check 6) | short-strike delta ≤ 0.30 |
| Liquidity (check 7) | bid-ask ≤ 25% of credit, OI ≥ 100 per leg |
| Max loss per trade | 2% of account equity |
| Max concurrent positions | 4 |
| Profit target / stop | 50% of credit / 2× credit |
| Expiry handling | held through expiry day, force-closed 15:30 ET (`time_exit_dte = -1`) |
| WATCH expiry window | 2 scan cycles |
| Scan interval | 15 minutes open, 15 minutes closed (`closed_market_sleep_minutes`) |
| Spread width | 5.0 points (see the check 5 / check 7 tradeoff in `config.py`) |
| Order pricing | limit at the net mid, then 3 re-quotes toward the crossing price |
| Score bands | ≥80 TRADE, 60–79 WATCH, <60 REJECT |

### Time

All market-hours logic, scan scheduling, expiry/DTE arithmetic and session
boundaries use **America/New_York**, via the single `config.MARKET_TZ` constant
and the `today_et()` / `now_et()` / `fmt_et()` helpers. `date.today()` is not
used anywhere in the agent. Timestamps are **stored in UTC** and **rendered in
market time** on every human-facing surface — CLI, event log, decision cards and
dashboard. This host runs in Europe, where a local-time assumption would shift
every session boundary and DTE by a day for part of each evening.

### Scheduling

`run_forever` fires the first cycle **immediately** — starting at 09:30 scans at
09:30 — then paces off a monotonic deadline advanced by the interval, not a
`sleep(interval)` after each cycle. Sleeping afterwards makes the period
`interval + cycle duration`, drifting ~8s per iteration and never landing on a
boundary; that matters because the expiry-day 15:30 close and the 09-04 hard
close fire on a *scan*, not on a timer. If a cycle overruns its slot the loop
skips whole intervals rather than firing catch-up scans back to back.

### Exit precedence

Checked in this order by `Executor.exit_trigger`; the first match wins.

| # | Rule | Fires when | Outranks |
|---|---|---|---|
| 1 | Hard close | at/after 2026-09-04 15:30 ET | everything, P&L included |
| 2 | Expiry-day close | 15:30 ET on the position's own expiry date, or any time after it | profit target, stop |
| 3 | Time exit (DTE) | `dte <= time_exit_dte` | profit target, stop |
| 4 | Profit target | cost to close ≤ 50% of credit | — |
| 5 | Stop loss | cost to close ≥ 2× credit | — |

`time_exit_dte` is **-1**, which retires rule 3: a position expiring today reads
`dte = 0` and is held, managed on its profit target and stop like any other,
until the 15:30 ET expiry-day flatten takes it. The rule is unreachable at this
setting by construction — `dte <= -1` means the expiry date has already passed,
and rule 2 fires for any date before today, so it always gets there first.

These are physically-settled ETF options, so rule 2 is not a P&L judgement: a
short leg carried through expiry can be assigned, which is why it outranks both
P&L rules. Rule 1 is the end-of-window backstop and closes everything still open
regardless of expiry.

### Order pricing

Entries are submitted as **limit orders at the mid of the net spread price** —
the bid-ask is never crossed on entry. If the order does not fill within
`requote_wait_seconds`, the limit walks linearly from the mid toward the
crossing price (shorts at the bid, longs at the ask) over `requote_attempts`
steps. The crossing price is a floor that is never exceeded, so the agent
concedes at most the spread. Every re-quote is logged as an event.

Each step must transmit a *different* limit price. The net price is carried in
dollars per spread but transmitted as a per-share limit rounded to the cent, so
a walk narrower than $1.00 of net per step collapses onto one price; replacing
an order with the limit it already carries is refused by Alpaca with 422 "order
parameters are not changed". Steps that would collapse are dropped (the walk
still ends exactly on the crossing price), and a 422 no-op skips to the next
distinct price instead of abandoning the remaining steps.

Any entry still working at the end of the cycle that placed it is **cancelled**.
An unfilled entry left resting fills later against quotes nothing re-evaluated,
and its position row — written at submission — would otherwise be closed as
missing while the order is still live. The next cycle re-prices against fresh
quotes.

Two values the spec references without a number, chosen here and documented:
`high_conviction_multiple` (2.0 × the trend threshold, for the depressed-premium
debit-spread branch) and `max_same_direction_positions` (3, for check 9's
"directional exposure").

---

## Repository layout

```
├── config.py               Single source of truth for every threshold
├── bootstrap.py            Makes the spec-mandated `logging/` package importable
├── agent.py                The perception-decision-action loop
├── cli.py                  Command line interface
├── perception/             Stage 1 — account/position/market data via Alpaca MCP
│   ├── mcp_client.py         MCP stdio session, reconnect, per-call logging
│   ├── market.py             Typed wrappers over the MCP tools
│   └── normalize.py          Tolerant parsing of MCP responses
├── signals/                Stage 2 — Sections 3.1, 3.2
│   ├── volatility.py         ATM IV vs realized vol -> elevated / depressed
│   └── trend.py              MA10 vs MA30 -> up / down / range-bound
├── decision/               Stage 3 — Section 3.3
│   ├── matrix.py             Strategy selection matrix
│   └── structure.py          Strikes, expiry, credit, defined-risk legs
├── evaluation/             Stage 4 — Sections 4, 5
│   ├── scorer.py             9-check checklist, hard gates, score bands
│   └── states.py             TRADE / WATCH / REJECT / EXPIRED state machine
├── risk/                   Stage 5 — Section 3.4
│   └── rules.py              Sizing, limits, event avoidance, exits, breaker
├── execution/              Stage 6 — order placement and management via MCP
│   ├── orders.py             Idempotent multi-leg orders, exits, flatten
│   └── reconcile.py          Broker-truth position/order reconciliation
├── logging/                Stage 7 — Sections 6, 7
│   ├── store.py              SQLite persistence
│   ├── events.py             Timestamped pipeline stage events
│   └── decision_card.py      Templated decision cards
├── monitoring/             Section 8
│   ├── ledger.py             Activity ledger (raw counts)
│   ├── outcomes.py           Outcome measures (with sample sizes)
│   └── baseline.py           Unfiltered + passive baselines
├── dashboard/              Sections 6, 10
│   ├── pipeline.py           Visible decision pipeline
│   ├── server.py             FastAPI JSON API + static UI
│   ├── views/                Positions, P&L curve, reasoning log, regimes
│   └── static/index.html     Financial-terminal front end
├── data/agent.db           SQLite store (gitignored)
└── docs/implementation.md  The build specification
```

### A note on the `logging/` package

Section 12 mandates a top-level package named `logging`, which shadows Python's
stdlib `logging` for the whole process. The layout is preserved as specified;
`logging/__init__.py` loads the real stdlib module and extends its `__path__` to
cover this directory, and `bootstrap.py` repairs the same path when something
else (`asyncio`, `dotenv`, `uvicorn`) imports stdlib `logging` first. Both
namespaces work: `import logging` gets the stdlib module, `from logging.events
import ...` gets ours. See the docstrings in those two files.

---

## Data model

SQLite at `data/agent.db`:

| Table | Contents |
|---|---|
| `events` | Pipeline stage events with payloads |
| `mcp_calls` | Every MCP tool call: name, args, response, latency |
| `decisions` | One row per evaluated opportunity, with its decision card |
| `watch_items` | Persistent WATCH state, re-evaluated across cycles |
| `positions` | Agent-opened positions, entry credit, exit bookkeeping |
| `orders` | Submitted orders keyed by `client_order_id` |
| `baselines` | Unfiltered and passive baseline rows |
| `iv_history` | Daily ATM IV per underlying (check 2's trailing average) |
| `equity_curve` | Account equity samples |
| `day_state` | Per-day order attempts and circuit-breaker state |
