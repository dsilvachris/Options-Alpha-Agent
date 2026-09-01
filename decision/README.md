# decision/

**Spec section:** 3.3 (strategy selection matrix), plus concrete strike/expiry
selection.

Turns a regime reading into a specific, priced, defined-risk structure.

## Files

| File | Purpose |
|---|---|
| `matrix.py` | The Section 3.3 lookup table. Returns eligibility only. |
| `structure.py` | Selects expiry and strikes; builds and prices the legs. |

## Inputs

`VolatilityReading`, `TrendReading`, the option chain, and spot.

## Outputs

* `MatrixResult` — the eligible structure, its directional bias, whether it is a
  credit structure, and the rationale text used in decision cards.
* `ProposedStructure` — expiry, DTE, legs, net credit, spread width, max loss,
  max profit, breakevens, summed bid-ask, short-strike delta, minimum open
  interest. One contract's worth; the risk gate decides quantity.

## Non-obvious decisions

* **The matrix authorizes nothing.** It returns eligibility, which is then input
  to the scorer (Section 4) and the risk gate (Section 3.4).

* **Short strike = highest |delta| still at or below the 0.30 ceiling.** That
  collects the most credit consistent with check 6. When no strike satisfies the
  ceiling, the closest miss is returned anyway so the scorer records a real
  measured value and fails check 6 explicitly, rather than the symbol vanishing
  with no decision recorded.

* **Long strike = the strike closest to `spread_width_points` further out of
  the money** (default 5.0 points). Width is a genuine tradeoff, not a free
  parameter: widening improves check 7 (a bigger credit shrinks bid-ask as a
  percentage of it) but worsens check 5 (credit grows more slowly than width,
  so credit/width falls). The right value comes from the observed live score
  distribution, not from theory.

* **The delta ceiling and the credit/width floor must move together.** For a
  credit spread, credit/width is bounded roughly by the short strike's delta,
  so a 0.30 credit/width floor is unreachable at a 0.20-delta short. The two
  are set consistently at 0.30.

* **Iron condor risk is the wider wing, not the sum.** Only one side can be
  breached at expiry, so `width = max(put_width, call_width)`.

* **Defined risk is asserted, not assumed.** `_assert_defined_risk()` verifies
  every short leg has a covering long leg in the same expiry and right, further
  out of the money, and raises `StructureError` otherwise. There is no code path
  that returns a naked position.

* **A non-positive credit is refused.** If the chain's quotes do not actually
  support a credit for a credit structure, `StructureError` is raised and the
  symbol is rejected with that reason recorded.

* **The depressed-premium debit branch rarely reaches TRADE, by construction.**
  A debit spread fails check 1 (premium rich) and check 5 (credit quality) by
  definition, capping it at 70/100 — inside the WATCH band. This matches Section
  3.3's intent that the agent primarily *stands aside* when premium is
  depressed. `high_conviction_multiple` in `config.py` quantifies "trend
  conviction is high", which the spec leaves as prose.
