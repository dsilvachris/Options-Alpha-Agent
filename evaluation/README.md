# evaluation/

**Spec sections:** 4 (opportunity evaluation framework), 5 (decision state machine).

Grades a candidate structure against the 9-check scored checklist, applies the
three hard gates, and resolves the opportunity to TRADE / WATCH / REJECT.

## Files

| File | Purpose |
|---|---|
| `scorer.py` | Section 4 — the checklist, hard gates and score bands. |
| `states.py` | Section 5 — the WATCH lifecycle and its transitions. |

## Inputs

`VolatilityReading`, `TrendReading`, `MatrixResult`, `ProposedStructure`, the
underlying's corporate actions, and a `PortfolioState`.

## Outputs

* `ScoreResult` — total score, resolved state, every `Check` with its measured
  value and threshold, the failing hard gate if any, a reason string, and (for
  WATCH) the promoting condition.
* Persistent `watch_items` rows and `WATCH_TRANSITION` events.

## The checklist

Each check is pass/fail and worth a fixed point value. **There are no fitted
coefficients and no tuned weights** — the score is the plain sum of points from
checks that passed, so it is always traceable back to the specific conditions
met (Section 4.5). Points total exactly 100.

## Non-obvious decisions

* **Hard gates short-circuit before the score sum.** Checks 7, 8 and 9 are
  evaluated first. If any fails, the result is REJECT with `score = 0` and the
  sum is never computed. The soft checks are still *recorded* so the decision
  card explains the full picture, but they contribute no points.

* **Check 2 is NOT EVALUABLE until it has real history — not failed.** Checks are
  tri-state: passed, failed, or not evaluable. A check needs
  `SIGNALS.iv_average_window` (3) prior sessions of ATM IV before its "trailing
  average" means anything; with one prior day the average is a single day of
  noise, which is not a volatility-stability signal. Scoring that as a failure
  penalised every candidate for history the agent had not accumulated yet.

  When fewer than 3 sessions exist the check's 10 points leave **both** the
  numerator and the denominator, and the score is rescaled over the available
  points so the bands keep their meaning: 75 earned of 90 available is the same
  quality as 83 of 100. The 1.15 threshold is untouched; once 3 sessions exist
  the check evaluates normally.

  ```
  sessions  check2           raw  avail  score   band
  0         NOT_EVALUABLE     90     90    100   TRADE
  1         NOT_EVALUABLE     90     90    100   TRADE
  2         NOT_EVALUABLE     90     90    100   TRADE
  3         PASS             100    100    100   TRADE
  ```

  It is stated on every decision card — `Not evaluated: #2 volatility stable —
  NOT EVALUABLE (needs 3 sessions, has 1)` — alongside the rescale, because an
  83 earned on eight checks must be distinguishable from an 83 earned on nine.
  The activity ledger counts non-evaluable checks separately so they never
  distort the most-failed ranking.

* **A hard gate that cannot be evaluated is a rejection, not a skip.** Gates 7,
  8 and 9 are mandatory; an unverifiable mandatory condition rejects the
  opportunity rather than dropping out of the denominator.

* **Check 8 is honest about the earnings gap.** Alpaca's MCP server exposes
  corporate action announcements (dividend, split, merger, spinoff, reorg) but
  **no earnings calendar**. For the configured ETF watchlist this is complete —
  SPY/QQQ/IWM have no issuer earnings — so the check passes on a clean corporate
  action result. For any non-ETF symbol the check **fails closed** and says why,
  rather than passing a condition it cannot verify. See `risk/README.md`.

* **The promoting condition is computed, not narrated.** For a WATCH result the
  scorer finds the smallest failing check whose points would close the gap to
  the TRADE band and records it as the specific condition that would promote the
  item. If no single check suffices, it says so and lists what is outstanding.

* **WATCH items are refreshed, never recreated.** `WatchRegistry.record()` keys
  on `symbol|structure|expiry`; a setup seen again has its age incremented and
  its score updated in place. Items are closed out as EXPIRED once
  `cycles_seen >= watch_expiry_cycles`, with the unmet promoting condition as
  the reason. Every transition is emitted as an event so the dashboard can show
  the lifecycle over a session.
