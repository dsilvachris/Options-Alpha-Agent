# risk/

**Spec section:** 3.4 (risk management rules).

The independent Risk Gate. Runs after the scorer and before any order reaches
Alpaca.

## Files

| File | Purpose |
|---|---|
| `rules.py` | Position sizing, portfolio limits, event avoidance, exit discipline, circuit breaker. |

## Inputs

The `ProposedStructure`, account equity, current open/same-direction position
counts, the proposed directional bias, and the underlying's corporate actions.

## Outputs

* `RiskDecision` — approved or refused, the number of contracts, total max loss,
  the per-trade cap, every refusal reason, and the `ExitPlan`.
* `ExitPlan` — profit target, stop loss and time exit, all as absolute
  cost-to-close dollar levels, stored with the position.
* Circuit breaker halt events.

## Constraints enforced

| Constraint | Rule |
|---|---|
| Position sizing | Max loss on a single trade ≤ 2% of account equity |
| Portfolio limits | ≤ 4 concurrent open positions |
| Directional exposure | ≤ 3 open positions sharing a directional bias |
| Event avoidance | No new position with a scheduled event before expiry |
| Exit discipline | Close at 50% of credit, 2× credit, or 1 DTE |
| Circuit breaker | Halt after 10 order attempts or 3% daily drawdown |

## Non-obvious decisions

* **The gate cannot be bypassed.** A score of 100 is still refused if a limit is
  breached. `evaluate()` is the only sanctioned path to an order, and
  `execution/orders.py` refuses to place anything whose `RiskDecision` was not
  approved.

* **All reasons are collected, not just the first.** The gate runs every
  constraint and returns the complete list, so a decision card records the full
  picture rather than one arbitrary blocker.

* **Contract count is floor division against the cap.** `contracts = cap //
  max_loss_per_contract`. If a single contract already exceeds the per-trade cap
  the trade is refused outright rather than sized to zero and silently skipped.

* **Event avoidance fails closed for non-ETFs.** Section 3.4 requires no
  earnings before expiry. The Alpaca MCP server has **no earnings calendar** —
  `get_corporate_action_announcements` covers only Dividend, Split, Merger,
  Spinoff and Reorg. For the configured watchlist (SPY, QQQ, IWM) this is
  sufficient: they are ETFs with no issuer earnings, so a clean corporate-action
  result genuinely satisfies the rule. For any symbol not in
  `RISK.etf_symbols`, the gate **refuses** and states that the condition cannot
  be verified. Adding a single-name underlying to the watchlist therefore
  requires wiring an earnings data source first — it will not silently trade
  through an unverified event.

* **Defined risk is re-checked here.** Even though `decision/structure.py`
  already asserts it, the gate independently verifies every short leg is paired.
  A defence that matters is worth checking twice.

* **The circuit breaker persists per day.** Once tripped it is written to
  `day_state` and stays tripped for the rest of the calendar day, surviving a
  process restart.
