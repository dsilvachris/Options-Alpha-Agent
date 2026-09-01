# perception/

**Spec section:** Section 2, Stage 1 (Perception).

Pulls account state, current positions, and market/options-chain data for the
watchlist underlyings. **This is the only package that talks to Alpaca**, and it
does so exclusively through the official Alpaca MCP server.

## Files

| File | Purpose |
|---|---|
| `mcp_client.py` | The MCP stdio session: connect, reconnect, call, log. |
| `market.py` | Typed wrappers over the MCP tools; assembles an `UnderlyingSnapshot`. |
| `normalize.py` | Tolerant parsing of MCP responses into typed values. |

## Inputs

`ALPACA_API_KEY` / `ALPACA_SECRET_KEY` from `.env` (via `config.py`), and a
watchlist symbol.

## Outputs

* `Account` — equity, cash, buying power, options approval level.
* `UnderlyingSnapshot` — spot, daily closes, merged option chain, eligible
  expiries, corporate actions.
* `OptionContract` — strike, right, expiry, bid/ask, IV, greeks, open interest.
* A row in `mcp_calls` for every tool call.

## MCP tools used

`get_account_info`, `get_all_positions`, `get_open_position`, `get_clock`,
`get_stock_bars`, `get_stock_latest_trade`, `get_option_contracts`,
`get_option_chain`, `get_option_snapshot`,
`get_corporate_action_announcements`, `get_portfolio_history`.

`REQUIRED_TOOLS` in `market.py` lists every tool the agent depends on, including
the execution ones. `verify_tools()` is called at startup and raises
`MCPToolMissing` if the server does not expose one — the agent stops rather than
falling back to a REST client.

## Non-obvious decisions

* **Verified live API behaviour (2026-09-01).** The three option tools do *not*
  each return what their descriptions imply. Confirmed by direct calls:

  | Field | `get_option_chain` | `get_option_snapshot` | `get_option_contracts` |
  |---|---|---|---|
  | bid / ask | yes | yes | no |
  | impliedVolatility | **yes** | **no** | no |
  | greeks (delta) | **yes** | **no** | no |
  | open_interest | no | no | **yes** |

  So IV and delta come from the chain, open interest from contract metadata, and
  `get_option_snapshot` is used only for mid-price refreshes during exit
  monitoring. The `opra` feed returns **HTTP 403** without a subscription; the
  server auto-selects `indicative`, which does carry IV and greeks.

  Coverage is not uniform across the chain — near the money everything is
  populated, but roughly 40% of far-OTM contracts have no IV/greeks and ~30%
  have no open interest. `_pick_short()` only considers contracts with a delta,
  and check 7 fails when open interest is absent, so missing data can never be
  silently treated as passing.

* **Open interest is merged from two tools.** `get_option_chain` returns quotes,
  IV and greeks but not open interest; `get_option_contracts` returns contract
  metadata including `open_interest`. Check 7's open-interest floor needs both,
  so `option_chain()` pulls both and merges them by OCC symbol.

* **Contract identity comes from the OCC symbol, not the payload.** Strike,
  expiry and right are decoded from the symbol (`occ_parse`) rather than trusted
  from response fields, whose spelling varies between endpoints.

* **Responses are parsed defensively.** Some MCP tools return structured JSON and
  others return human-formatted text. `normalize.py` accepts either, tries
  several key spellings (`snake_case`, `camelCase`, Alpaca short forms), and
  falls back to regex extraction from formatted text.

* **The chain is filtered server-side.** Requests are constrained by expiry
  window and a ±15% strike band around spot, and paginated, to keep responses
  small — `get_option_chain` caps at 1000 data points per page.

* **Reconnect, not restart.** A dropped subprocess is detected on the next call;
  the session is torn down and rebuilt with exponential backoff and the call is
  retried. A cycle that still fails is recorded and the loop continues.
