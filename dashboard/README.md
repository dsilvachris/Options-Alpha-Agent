# dashboard/

**Spec sections:** 6 (visible decision pipeline), 10 (dashboard requirements).

A financial-terminal interface over recorded backend state.

## Files

| File | Purpose |
|---|---|
| `pipeline.py` | Section 6 — groups stage events into the six-stage pipeline. |
| `server.py` | FastAPI JSON API and static file serving. |
| `views/__init__.py` | Section 10 — positions, P&L curve, reasoning log, regimes, MCP activity. |
| `static/index.html` | The terminal front end (no build step, no dependencies). |

## Running

```bash
.venv/bin/python cli.py dashboard      # http://127.0.0.1:8787
```

## Panels

| Panel | Section | Source |
|---|---|---|
| Decision Pipeline | 6 | `events` grouped by cycle and stage |
| Regime Readings | 10 | latest `MARKET_ANALYSIS` event per underlying |
| **MCP Activity** | — | `mcp_calls` — live Alpaca MCP tool call stream |
| Open Positions | 10 | `positions` with exit plan levels |
| WATCH Items | 5.1 | `watch_items` with age and promoting condition |
| Equity & Baselines | 10, 8.3 | `equity_curve` vs passive baseline, indexed |
| Reasoning Log | 7, 10 | `decisions` with full per-check breakdown |
| Activity Ledger & Outcomes | 8 | `monitoring/` |

## API

`GET /api/state` returns everything in one poll. `GET /api/pipeline`,
`/api/mcp`, `/api/decisions`, `/api/positions`, `/api/monitoring` are also
available individually.

## Non-obvious decisions

* **Simulated positions are visibly labelled and excluded from money.** Rows
  carry a `SIM` badge, the slot counter and header `REALIZED` are live-only, and
  simulated notional P&L gets its own violet chip. The equity curve is real
  broker equity by construction. The reasoning log and activity ledger show every
  decision, simulated or not.

* **The front end simulates nothing.** Section 6 is explicit: the dashboard
  renders recorded state only. A pipeline stage with no recorded event is shown
  as `pending` and greyed out — it is never filled in, animated forward, or
  interpolated. Every number on screen traces to a database row.

* **Polling, not streaming.** The front end re-fetches `/api/state` every 5
  seconds. A scan cycle takes far longer than that, so the pipeline visibly
  advances stage by stage during a live demo without needing a websocket.

* **The MCP Activity panel is deliberately prominent.** Meaningful use of
  Alpaca's MCP server is a judged criterion, so the live call stream — tool
  name, arguments, latency, response summary — is a first-class panel rather
  than debug output.

* **The equity chart indexes to percent from the first sample**, so the agent's
  equity curve and the passive buy-and-hold baseline are comparable on one axis
  despite completely different absolute scales.

* **No JavaScript dependencies and no build step.** One static HTML file with
  inline CSS and vanilla JS, served by FastAPI. The dashboard runs anywhere the
  agent runs.
