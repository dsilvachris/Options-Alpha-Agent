# execution/

**Spec section:** Section 2, Stage 6 (Execution).

Places and manages orders through Alpaca's Trading API **via the MCP server**,
and monitors open positions against the exit rules. No HTTP client is imported
here.

## Files

| File | Purpose |
|---|---|
| `orders.py` | Order placement, exit monitoring, position closing, flatten. |
| `reconcile.py` | Broker-truth reconciliation of positions and orders. |

## Inputs

An approved `RiskDecision` and its `ProposedStructure`; recorded positions from
the store.

## Outputs

* `OrderResult` — whether it was submitted, the client order id, broker order
  id, status.
* Rows in `orders` and `positions`; `EXECUTION` and `POSITION_MANAGEMENT` events.

## MCP tools used

`place_option_order` (entries and exits, as `mleg` multi-leg orders),
`get_order_by_client_id`, `get_order_by_id`, `get_orders`, `close_position`,
`close_all_positions`, `cancel_order_by_id`, `get_option_snapshot` (via
`perception/`, for mark-to-market).

## Non-obvious decisions

* **DRY_RUN is the default.** When true the full path runs — structure priced,
  risk gate applied, position and order recorded, events emitted — but
  `place_option_order` is never called. The recorded order carries status
  `DRY_RUN` so the dashboard and ledger distinguish it from a live fill.

* **A position identity carries an entry sequence.** `position_key` is
  `symbol|structure|expiry|legs#SEQ`, where SEQ is the number of *already closed*
  entries into that same setup. The sequence advances only on a close, which is
  exactly what separates the two cases that must behave differently:

  | Situation | Sequence | Result |
  |---|---|---|
  | Morning entry closed at profit, same setup qualifies in the afternoon | advances | new client_order_id -> **re-entry allowed**, own position row |
  | Same still-open setup proposed again next cycle | unchanged | same id -> idempotent skip |
  | Retry after a lost submit response | unchanged | same id -> collides locally and at Alpaca |

  Without the sequence both the order and the position row were blocked: the
  client_order_id was identical, and `positions.position_key` is UNIQUE with an
  `INSERT OR IGNORE`, so a genuine re-entry produced neither an order nor a row.

* **Idempotency uses a deterministic client order id.**
  `client_order_id = f"oaa-{intent}-{sha1(position_key|intent|date)[:20]}"`.
  Before submitting, the local `orders` table is checked, then the broker via
  `get_order_by_client_id`. A retry after a timeout resolves to the original
  order instead of opening a second position. A failed lookup is treated as
  "not found" — that is the normal response for a fresh id — which is safe
  because the local store is checked first and Alpaca enforces
  `client_order_id` uniqueness server-side as the real backstop.

* **Limit price sign convention.** The MCP tool takes a net per-strategy price
  where **positive is a debit and negative is a credit**. A collected credit is
  therefore submitted as a negative limit price.

* **Entries are limit orders, exits are market orders.** An entry that cannot
  fill at the priced credit should not fill at all. An exit triggered by a stop
  or a time limit must actually complete.

* **The cycle that places an order owns it.** `cancel_unfilled_entries` runs at
  the end of every live cycle and cancels every entry order still working, so
  none is ever left resting between cycles: an unfilled entry is a standing
  instruction priced against quotes the agent can no longer see. Its status is
  re-read from the broker immediately before the cancel, so a fill landing in
  that gap is never cancelled out from under its position, and the outcome is
  read back rather than assumed. Exits are deliberately untouched — a working
  exit is trying to close risk. The position row behind a cancelled entry is
  retired as `ENTRY_CANCELLED` with no realized P&L: nothing was ever held.

* **Every re-quote must transmit a different limit price.** The ladder walks the
  net price in dollars per spread, but the order carries a per-share limit
  rounded to the cent, so steps narrower than $1.00 of net collapse onto one
  price. Alpaca refuses a replacement that changes nothing with 422 "order
  parameters are not changed". Collapsing steps are dropped when the ladder is
  built — keeping the last of each group, so the walk still ends exactly on the
  crossing price — and a 422 at run time skips to the next distinct price rather
  than abandoning the rest of the walk. Any other error still stops it.

* **Multi-leg orders are submitted as a single `mleg` order**, so a long leg can
  never fill without its short leg — the defined-risk guarantee holds at the
  broker, not just in this codebase.

* **Exit triggers are checked against cost-to-close**, computed by re-pricing
  every leg at its current mid: buying back shorts costs, selling longs returns.
  Time exit is evaluated first and does not require a quote, so a position still
  exits on schedule when the chain is unquotable.

* **The broker is the source of truth, not the store.** `reconcile.py` runs at
  agent startup and at the top of every scan cycle, before any opportunity is
  evaluated, and repairs divergence in both directions: a broker position the
  store does not track is **adopted** (its structure, legs, credit, width and
  expiry reconstructed from the originating order) so the exit rules manage it;
  a local position the broker does not hold is closed as `RECONCILED_MISSING`.
  Each repair is emitted as its own event.

  A position row is *not* closed as missing while its entry order is still
  working. The row is written at submission, so an unfilled entry has no broker
  legs behind it yet — closing it would advance the entry sequence and let the
  next cycle open a second position in the same underlying if the order then
  filled. Open **orders** are read alongside positions for exactly this, and a
  broker that cannot be asked closes nothing.

  This exists because of a real failure found by the self-test harness: a submit
  that timed out after Alpaca accepted it left a live spread with no position
  row, so no profit target, stop or time exit would ever evaluate against it.

* **A local FAILED status is never trusted.** `FAILED`, `SUBMITTING` and
  `UNKNOWN` mean the *response* was lost, not that the order was refused, so
  `_existing_order` asks the broker before concluding an order never landed.
  If the broker holds it, the local record is repaired and the position adopted.
  If the broker cannot be reached, a retry is allowed — Alpaca enforces
  `client_order_id` uniqueness server-side, so a duplicate is refused there
  rather than becoming a second position.

* **`sync_order_status` repairs the position, not just the status.** Fixing the
  status alone was the original bug: it left an order marked live with no
  position row behind it.

* **The session gate lives at the submission layer, not only in the risk gate.**
  `Executor.market_open` blocks `open_structure` and `close_structure` when the
  market is known to be closed. Entries are already refused by the risk gate, but
  **exits bypass the risk gate entirely**, so without a second gate a stop or
  time exit firing overnight would queue a market order into a closed book to
  fill at the next open at an unevaluated price. `close_structure(force=True)` is
  the deliberate exception, used only by the manual `flatten` command.

* **Simulated state must be cleared before going live.** `clear-simulated`
  retires open DRY_RUN positions with reason `DRY_RUN_CLEARED` and neutralises
  their order rows; decisions and events are left intact. `Agent.start()` refuses
  to run live while simulated positions are open.

  Both halves matter. `client_order_id` is derived from
  `position_key|intent|date`, so a dry-run scan and a live run on the same day
  produce the **same id**: a leftover DRY_RUN order row would make the
  idempotency probe skip the real order. The executor therefore ignores
  `dry_run=1` rows while live, and `record_order` upgrades a simulated row to
  `dry_run=0` when a live submission is recorded under the same id — without
  that upgrade the live retry path would double-submit.

* **DRY_RUN positions are exempt from the missing-position sweep.** They are
  simulated and the broker will never hold them; closing them as missing would
  destroy the dry-run exit-testing path. They are counted and reported as
  skipped.

* **Flatten closes recorded positions first, then calls
  `close_all_positions`.** Closing the agent's own positions individually keeps
  realized P&L and exit reasons correct in the store; the broker-level call then
  catches anything opened outside the agent's records.
