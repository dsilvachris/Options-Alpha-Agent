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

## Hosting on Vercel (static snapshot)

The agent runs locally against SQLite. The hosted site is a **published snapshot**
of what the dashboard reads — the same payload, written to static JSON.

```bash
.venv/bin/python cli.py publish            # write dashboard/public/
.venv/bin/python cli.py publish --commit   # ... and commit if it changed
.venv/bin/python cli.py publish --push     # ... and push
.venv/bin/python cli.py loop --publish     # publish after every scan cycle
```

### Deploy steps

1. `git init` the repository and add a remote (the publish path degrades
   gracefully without one, writing files but committing nothing).
2. Import the repo at [vercel.com/new](https://vercel.com/new).
3. Framework preset **Other**; no build command, no install command. The root
   `vercel.json` already sets `outputDirectory` to `dashboard/public`.
4. Deploy. Every pushed snapshot triggers a redeploy.

`vercel.json` exists in two places on purpose: the repo root drives the build
(`outputDirectory`), and a copy is written into `dashboard/public/` so the
directory also deploys standalone if you point Vercel's root directory at it.

### Files written

| File | Contents |
|---|---|
| `data/state.json` | The complete payload — identical in shape to `GET /api/state` |
| `data/decisions.json` | Decision cards and WATCH items |
| `data/positions.json` | Open and closed positions |
| `data/equity.json` | Equity curve and baselines |
| `data/ledger.json`, `outcomes.json`, `baselines.json` | Section 8 monitoring |
| `data/mcp.json` | MCP call log |
| `data/regimes.json` | Per-underlying volatility and trend readings |
| `data/pipeline.json` | Decision pipeline and recent cycles |
| `data/index.json` | Manifest, digest and generation metadata |
| `index.html`, `config.js`, `vercel.json` | The site itself |

### Dual mode — one UI, two data sources

`index.html` is shared byte-for-byte between local and hosted. It loads
`config.js`, which sets the data source:

| Mode | `config.js` served by | Data source | Poll |
|---|---|---|---|
| live | the local FastAPI server | `/api/state` (SQLite, current) | 5s |
| static | the published copy | `./data/state.json` | 60s, age only |

There is no second UI and no build step — the dashboard is vanilla JS, so what
Vercel serves is exactly what runs locally.

**Snapshot age is shown prominently.** In static mode the header carries a
`PUBLISHED SNAPSHOT` chip and an age chip reading *"SNAPSHOT as of 07:13 ET ·
24 min ago"*, green under 20 minutes, amber under 90, and red and pulsing beyond
that. In live mode it reads `LIVE`. A stale snapshot can never be mistaken for
live data.

### Redaction

`dashboard/public/` is committed to a public repository, so `assert_clean()` runs
over the serialized payload **before anything is written** and raises
`RedactionError` — aborting the publish entirely — if it finds the API key or
secret from the environment, a `PK`/`AK` key id, a `PA` account-number token, or
any of `account_number`, `account_id`, `api_key`, `secret_key` and friends
carrying a real value. Failures report a masked prefix (`PKRT...26 chars`) rather
than echoing the credential into a terminal or CI log.

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
