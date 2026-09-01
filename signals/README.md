# signals/

**Spec sections:** 3.1 (volatility signal), 3.2 (trend signal).

Computes the two-factor regime read per underlying. Both modules are pure
functions over price and chain data — no I/O, no MCP calls, no state.

## Files

| File | Purpose |
|---|---|
| `volatility.py` | Section 3.1 — classifies premium as elevated or depressed. |
| `trend.py` | Section 3.2 — classifies trend as up, down, or range-bound. |

## Inputs

Daily closes (ascending), the option chain, and spot price.

## Outputs

* `VolatilityReading` — condition, ATM IV, realized vol, IV/RV ratio, the
  trailing IV average and today's ratio against it, and the prior-observation
  sample size.
* `TrendReading` — condition, MA10, MA30, signed separation as a fraction of
  price, and the threshold applied.

These feed the strategy matrix (Section 3.3) and checks 1, 2, 3 and 4.

## Non-obvious decisions

* **Realized volatility is close-to-close, annualized, over 20 sessions**, using
  the sample standard deviation of log returns × √252. It returns `None` rather
  than a bad number when there is not enough history, so check 1 fails
  explicitly instead of silently passing.

* **ATM IV averages the nearest call and put on the front eligible expiry.**
  Using one side alone picks up skew; averaging the two straddle legs closest to
  spot is a more stable read of at-the-money premium.

* **Trend requires separation before asserting a direction.** The MA
  relationship alone flips constantly in a flat market. Requiring
  `|MA10 − MA30| / price > 0.5%` means a quiet tape reads as range-bound — which
  routes to the iron condor rather than to a directional spread.

* **Check 2's trailing IV average is read from the store, not recomputed.**
  Alpaca does not serve historical implied volatility, so the agent records
  today's ATM IV per underlying in `iv_history` on every cycle and averages the
  prior sessions. On the first-ever session there is no history, and check 2
  fails with `sample_size: 0` recorded — deliberately conservative rather than
  assumed-pass.
