# logging/

**Spec sections:** 6 (pipeline stage events), 7 (decision cards).

Records everything the agent does. The dashboard reads these tables and nothing
else.

## Files

| File | Purpose |
|---|---|
| `store.py` | SQLite persistence for every table in the system. |
| `events.py` | Timestamped pipeline stage events (Section 6). |
| `decision_card.py` | Templated decision card generation (Section 7). |
| `__init__.py` | Stdlib-compatibility shim — see below. |

## Inputs / outputs

`EventLog.emit()` writes a stage event with a JSON payload, scoped to a cycle id.
`decision_card.*` renders card text from recorded numbers. `Store` is the single
persistence layer, used by every other module.

## Tables

`events`, `mcp_calls`, `decisions`, `watch_items`, `positions`, `orders`,
`baselines`, `iv_history`, `equity_curve`, `day_state`.

## Non-obvious decisions

* **This package name shadows Python's stdlib `logging`.** Section 12 mandates
  it. `__init__.py` loads the real stdlib `logging` from its own file location,
  installs it as `sys.modules["logging"]`, and extends its `__path__` to include
  this directory — so `import logging` yields the genuine stdlib module,
  `import logging.handlers` finds the stdlib submodule, and `from logging.events
  import ...` finds ours.

  That shim only wins if our package is touched before anything imports stdlib
  `logging`. `asyncio`, `dotenv` and `uvicorn` all import it first, which would
  otherwise make our submodules unreachable. Root-level `bootstrap.py` repairs
  that case by appending this directory to the already-loaded package's
  `__path__`, and is imported first in every module that could be an entry
  point. Both orderings now work.

* **Cards are templated, never free-form.** Every line of every card is a format
  string filled from recorded values, so the narrative cannot drift from the
  data it describes (Section 7). The same text is reused unchanged by the
  dashboard, the CLI and the documentation.

* **The spec fixes templates for TRADE and NO TRADE only.** `watch_card`,
  `expired_card` and `exit_card` follow the same templated style and carry the
  fields Sections 5 and 9 require — for WATCH: failing checks, current score,
  and the condition that would promote it.

* **Events are written straight through, not buffered.** A crash mid-cycle
  leaves everything up to that point on disk, so the dashboard shows exactly how
  far the agent actually got.

* **`positions` and `orders` carry a real `dry_run` column; `decisions` do not.**
  The flag lives in a column rather than inside the `detail` JSON so P&L queries
  can exclude simulated positions in SQL. Decisions are deliberately unflagged:
  the reasoning log and activity ledger report every decision the agent made.
  `Store._migrate()` adds the column and backfills it from the legacy `detail`
  location for stores created by an earlier version.

* **`open_positions()` includes dry-run by default.** The exit rules and the risk
  gate's portfolio-fit check must treat a simulated position exactly like a real
  one, or a dry run would never exercise them. Only reporting callers pass
  `include_dry_run=False`.

* **WAL mode is enabled** so the dashboard can read while the agent writes.
