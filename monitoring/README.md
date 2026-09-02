# monitoring/

**Spec section:** 8 (decision quality monitoring).

Reports what the agent actually did, with the statistical discipline Section 8.4
requires.

## Files

| File | Purpose |
|---|---|
| `ledger.py` | Section 8.1 — activity ledger, raw counts only. |
| `outcomes.py` | Section 8.2 — outcome measures with explicit sample sizes. |
| `baseline.py` | Section 8.3 — unfiltered and passive baselines. |

## Inputs

The `decisions`, `positions`, `watch_items`, `baselines` and `equity_curve`
tables.

## Outputs

* `ActivityLedger` — opportunities scanned by underlying; decisions issued by
  state; rejection reasons ranked; checks most often failed; WATCH promotions.
* `Outcomes` — realized and open P&L, equity curve, exits by trigger, result per
  closed position, average score of executed vs declined.
* `BaselineSnapshot` — unfiltered setup count and notional credit; passive
  buy-and-hold return.

All three render to text for the CLI (`cli.py ledger`) and to dicts for the
dashboard.

## Dry-run isolation

Simulated money never mixes with real results, but simulated *decisions* are
still reported. The split is deliberate:

| Surface | Includes DRY_RUN? | Filter |
|---|---|---|
| Realized P&L, closed/open position lists, exits by trigger | no | `store.all_positions(include_dry_run=False)` |
| Simulated notional P&L, sim open/closed counts | separate fields | `store.dry_run_positions()` |
| Account equity curve | n/a | sampled from Alpaca `get_account_info`; a simulated position places no order, so it cannot move it |
| Average opportunity score (executed vs declined) | yes | reads `decisions`, which carry no dry-run flag |
| Activity ledger — all counts | yes | `store.all_decisions()` |

P&L measures money; the ledger measures decision behaviour. A dry run should
show exactly what the agent decided while claiming to have earned nothing.

## Session isolation

Decisions carry `market_open`. Out-of-session candidates are priced off wide
after-hours quotes, so their rejection reasons and failed checks describe the
spread rather than the strategy. The ledger and the score-distribution report
therefore default to **in-session only**, counting out-of-session and
market-state-unrecorded decisions separately. `--all-sessions` includes them.

## Non-obvious decisions

* **The ledger computes no ratios at all.** Section 8.1 says raw counts, and
  over a five-session window a rate computed from three observations is
  misleading. Interpretation is left to the reader.

* **Every average carries `n=`.** `outcomes.py` labels each figure with its
  sample size and flags segments below five observations as small samples to be
  read as raw counts (Section 8.4).

* **Rejection reasons are bucketed for ranking.** Raw reason strings embed
  measured values and would each be unique, so `_reason_bucket()` groups them
  into stable categories (which hard gate, weak score, risk refusal, WATCH
  expiry) before ranking by frequency.

* **Baselines carry no capital and place no orders.** The unfiltered baseline
  records one row per setup the matrix matched, *ignoring the score and every
  soft check* — the counterfactual for "what if the evaluation framework were
  switched off". The passive baseline records a spot observation for the primary
  underlying. Both are appended inside the loop the agent already runs, so they
  cost no additional MCP traffic.

* **The unfiltered baseline is notional credit, not modelled P&L.** Marking a
  hypothetical position to expiry would require modelling fills the agent never
  made. Recording the credit that *would* have been collected, with the setup
  count, is a claim the data actually supports.
