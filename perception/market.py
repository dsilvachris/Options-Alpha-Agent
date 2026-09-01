"""
Stage 1 (Section 2) — account, position and market/chain perception via MCP.

Every function here is a thin, typed wrapper over an Alpaca MCP tool call. No
HTTP client is imported in this package: account state, positions, price bars,
option chains and market status all arrive through `AlpacaMCP.call()`.

MCP tools used
--------------
get_account_info, get_all_positions, get_open_position, get_clock,
get_stock_bars, get_stock_latest_trade, get_option_contracts, get_option_chain,
get_option_snapshot, get_corporate_action_announcements, get_portfolio_history
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from config import RISK, SIGNALS, UNIVERSE, today_et
from perception.mcp_client import AlpacaMCP
from perception.normalize import (
    as_obj,
    find_number_in_text,
    iter_records,
    next_page_token,
    occ_parse,
    parse_date,
    pick,
    to_float,
    to_int,
)

#: Tools this module and execution/ depend on. Checked at startup.
REQUIRED_TOOLS = (
    "get_account_info",
    "get_all_positions",
    "get_open_position",
    "get_clock",
    "get_stock_bars",
    "get_stock_latest_trade",
    "get_option_contracts",
    "get_option_chain",
    "get_option_snapshot",
    "get_corporate_action_announcements",
    "place_option_order",
    "get_orders",
    "get_order_by_id",
    "get_order_by_client_id",
    "close_position",
    "close_all_positions",
    "cancel_order_by_id",
    "replace_order_by_id",
    "get_portfolio_history",
)


@dataclass
class Account:
    equity: float
    last_equity: float | None
    cash: float | None
    buying_power: float | None
    options_approved_level: int | None
    status: str | None = None
    trading_blocked: bool = False
    account_blocked: bool = False
    raw: Any = None

    @property
    def tradable(self) -> bool:
        """False when Alpaca has blocked the account or suspended trading."""
        return (
            not self.trading_blocked
            and not self.account_blocked
            and (self.status or "ACTIVE").upper() == "ACTIVE"
        )


@dataclass
class OptionContract:
    """One option contract, merged from chain snapshot + contract metadata."""

    symbol: str
    underlying: str
    expiry: date
    right: str  # 'C' or 'P'
    strike: float
    bid: float | None = None
    ask: float | None = None
    implied_volatility: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    open_interest: int | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        if self.bid <= 0 and self.ask <= 0:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return max(0.0, self.ask - self.bid)

    def dte(self, today: date | None = None) -> int:
        return (self.expiry - (today or today_et())).days


@dataclass
class UnderlyingSnapshot:
    """Everything perception gathers for one watchlist underlying in a cycle."""

    symbol: str
    spot: float
    closes: list[float] = field(default_factory=list)
    contracts: list[OptionContract] = field(default_factory=list)
    expiries: list[date] = field(default_factory=list)
    corporate_actions: list[dict] = field(default_factory=list)


class MarketData:
    """Perception facade. All calls go through the MCP session."""

    def __init__(self, mcp: AlpacaMCP) -> None:
        self.mcp = mcp

    async def verify_tools(self) -> None:
        self.mcp.require(*REQUIRED_TOOLS)

    # -- account & positions ----------------------------------------------
    async def account(self) -> Account:
        raw = await self.mcp.call("get_account_info")
        obj = as_obj(raw)
        if isinstance(obj, dict):
            equity = to_float(pick(obj, "equity", "portfolio_value", "Equity"))
            last_equity = to_float(pick(obj, "last_equity", "LastEquity"))
            cash = to_float(pick(obj, "cash", "Cash"))
            buying_power = to_float(pick(obj, "buying_power", "buyingPower"))
            level = to_int(
                pick(obj, "options_approved_level", "options_trading_level",
                     "optionsApprovedLevel")
            )
            status = pick(obj, "status", "account_status")
            trading_blocked = bool(pick(obj, "trading_blocked", default=False))
            account_blocked = bool(pick(obj, "account_blocked", default=False))
        else:
            text = str(obj)
            equity = find_number_in_text(text, "Equity", "Portfolio Value")
            last_equity = find_number_in_text(text, "Last Equity")
            cash = find_number_in_text(text, "Cash")
            buying_power = find_number_in_text(text, "Buying Power")
            level = None
            found = find_number_in_text(text, "Options Approved Level",
                                        "Options Trading Level")
            if found is not None:
                level = int(found)
            match = re.search(r"status\s*[:=]\s*(\w+)", text, re.IGNORECASE)
            status = match.group(1) if match else None
            trading_blocked = "trading_blocked: true" in text.lower()
            account_blocked = "account_blocked: true" in text.lower()

        if equity is None:
            raise ValueError(f"Could not read account equity from get_account_info: {obj!r}")
        return Account(equity, last_equity, cash, buying_power, level, status,
                       trading_blocked, account_blocked, raw)

    async def positions(self) -> list[dict]:
        return iter_records(await self.mcp.call("get_all_positions"))

    async def portfolio_history(self, period: str = "1W", timeframe: str = "1D") -> Any:
        return await self.mcp.call(
            "get_portfolio_history", {"period": period, "timeframe": timeframe}
        )

    # -- market status -----------------------------------------------------
    async def clock(self) -> dict:
        raw = await self.mcp.call("get_clock")
        obj = as_obj(raw)
        if isinstance(obj, dict):
            is_open = pick(obj, "is_open", "isOpen")
            if isinstance(is_open, str):
                is_open = is_open.strip().lower() in {"true", "yes", "open"}
            return {"is_open": bool(is_open), "raw": obj}
        text = str(obj).lower()
        return {"is_open": ("market is open" in text) or ("is_open: true" in text),
                "raw": obj}

    # -- price history -----------------------------------------------------
    async def daily_closes(self, symbol: str, lookback_days: int | None = None) -> list[float]:
        """Ascending daily closes, long enough for MA30 and 20-day realized vol."""
        days = lookback_days or SIGNALS.bar_lookback_days
        raw = await self.mcp.call(
            "get_stock_bars",
            {
                "symbols": symbol,
                "timeframe": "1Day",
                "days": days,
                "limit": days + 20,
                "adjustment": "split",
                "sort": "asc",
            },
        )
        records = iter_records(raw)
        closes: list[tuple[Any, float]] = []
        for record in records:
            close = to_float(pick(record, "close", "c", "Close"))
            stamp = pick(record, "timestamp", "t", "time", "date")
            if close is not None:
                closes.append((stamp, close))
        # Records may arrive keyed by symbol; keep only this symbol when tagged.
        closes.sort(key=lambda item: str(item[0]))
        return [c for _, c in closes]

    async def spot(self, symbol: str) -> float | None:
        raw = await self.mcp.call("get_stock_latest_trade", {"symbols": symbol})
        for record in iter_records(raw):
            price = to_float(pick(record, "price", "p", "Price", "last_price"))
            if price:
                return price
        obj = as_obj(raw)
        if not isinstance(obj, dict):
            return find_number_in_text(str(obj), "Price", "Last")
        return None

    # -- options -----------------------------------------------------------
    def _eligible_expiries(self, today: date) -> tuple[date, date]:
        """The DTE window intersected with the configured expiry cutoff."""
        lo = today + timedelta(days=UNIVERSE.min_dte)
        hi = today + timedelta(days=UNIVERSE.max_dte)
        # expiry_cutoff is INCLUSIVE.
        return lo, min(hi, UNIVERSE.expiry_cutoff)

    async def option_contracts_meta(
        self, symbol: str, spot: float, today: date
    ) -> dict[str, dict]:
        """
        Contract metadata keyed by OCC symbol.

        Open interest is only available from get_option_contracts (the chain
        snapshot carries quotes/greeks but not OI), so check 7's open-interest
        floor is sourced here and merged into the chain below.
        """
        lo, hi = self._eligible_expiries(today)
        if lo > hi:
            return {}
        low_strike = spot * (1 - UNIVERSE.chain_strike_window)
        high_strike = spot * (1 + UNIVERSE.chain_strike_window)

        meta: dict[str, dict] = {}
        page_token: str | None = None
        for _ in range(UNIVERSE.chain_max_pages):
            args: dict[str, Any] = {
                "underlying_symbols": symbol,
                "status": "active",
                "expiration_date_gte": lo.isoformat(),
                "expiration_date_lte": hi.isoformat(),
                "strike_price_gte": round(low_strike, 2),
                "strike_price_lte": round(high_strike, 2),
                "limit": UNIVERSE.chain_page_limit,
            }
            if page_token:
                args["page_token"] = page_token
            raw = await self.mcp.call("get_option_contracts", args)
            for record in iter_records(raw):
                occ = pick(record, "symbol", "id")
                if not isinstance(occ, str):
                    continue
                meta[occ.upper()] = record
            page_token = next_page_token(raw)
            if not page_token:
                break
        return meta

    async def option_chain(
        self, symbol: str, spot: float, today: date
    ) -> list[OptionContract]:
        """Chain snapshots (quotes, IV, greeks) merged with contract metadata."""
        lo, hi = self._eligible_expiries(today)
        if lo > hi:
            return []
        low_strike = spot * (1 - UNIVERSE.chain_strike_window)
        high_strike = spot * (1 + UNIVERSE.chain_strike_window)
        meta = await self.option_contracts_meta(symbol, spot, today)

        snapshots: dict[str, dict] = {}
        page_token: str | None = None
        for _ in range(UNIVERSE.chain_max_pages):
            args: dict[str, Any] = {
                "underlying_symbol": symbol,
                "expiration_date_gte": lo.isoformat(),
                "expiration_date_lte": hi.isoformat(),
                "strike_price_gte": round(low_strike, 2),
                "strike_price_lte": round(high_strike, 2),
                "limit": UNIVERSE.chain_page_limit,
            }
            if UNIVERSE.option_feed:
                args["feed"] = UNIVERSE.option_feed
            if page_token:
                args["page_token"] = page_token
            raw = await self.mcp.call("get_option_chain", args)
            for record in iter_records(raw):
                occ = pick(record, "symbol", "id")
                if isinstance(occ, str):
                    snapshots[occ.upper()] = record
            page_token = next_page_token(raw)
            if not page_token:
                break

        contracts: list[OptionContract] = []
        for occ, snap in snapshots.items():
            parsed = occ_parse(occ)
            if not parsed or parsed["underlying"] != symbol.upper():
                continue
            record_meta = meta.get(occ, {})
            contract = OptionContract(
                symbol=occ,
                underlying=parsed["underlying"],
                expiry=parsed["expiry"],
                right=parsed["right"],
                strike=parsed["strike"],
                open_interest=to_int(
                    pick(record_meta, "open_interest", "openInterest")
                ),
            )
            self._apply_snapshot(contract, snap)
            contracts.append(contract)

        contracts.sort(key=lambda c: (c.expiry, c.right, c.strike))
        return contracts

    @staticmethod
    def _apply_snapshot(contract: OptionContract, snap: dict) -> None:
        quote = pick(snap, "latestQuote", "latest_quote", "quote") or {}
        if isinstance(quote, dict):
            contract.bid = to_float(pick(quote, "bid_price", "bp", "bid"))
            contract.ask = to_float(pick(quote, "ask_price", "ap", "ask"))
        if contract.bid is None:
            contract.bid = to_float(pick(snap, "bid_price", "bid", "bp"))
        if contract.ask is None:
            contract.ask = to_float(pick(snap, "ask_price", "ask", "ap"))

        contract.implied_volatility = to_float(
            pick(snap, "impliedVolatility", "implied_volatility", "iv")
        )
        greeks = pick(snap, "greeks", "Greeks") or {}
        if isinstance(greeks, dict):
            contract.delta = to_float(pick(greeks, "delta"))
            contract.gamma = to_float(pick(greeks, "gamma"))
            contract.theta = to_float(pick(greeks, "theta"))
            contract.vega = to_float(pick(greeks, "vega"))
        if contract.delta is None:
            contract.delta = to_float(pick(snap, "delta"))
        if contract.open_interest is None:
            contract.open_interest = to_int(
                pick(snap, "open_interest", "openInterest", "oi")
            )

    async def option_snapshot(self, symbols: list[str]) -> dict[str, OptionContract]:
        """
        Refresh quotes for specific contracts (used by exit monitoring).

        IMPORTANT: verified against the live API on 2026-09-01 — despite its tool
        description, `get_option_snapshot` returns only dailyBar / latestQuote /
        latestTrade / minuteBar / prevDailyBar. It carries **no
        impliedVolatility, no greeks and no open_interest** on either the
        default or `indicative` feed. Only `get_option_chain` returns IV and
        greeks, and only `get_option_contracts` returns open interest.

        That is fine for this method's purpose: exit monitoring needs mid prices
        only. Do not use it as a source of IV or delta.
        """
        if not symbols:
            return {}
        raw = await self.mcp.call(
            "get_option_snapshot", {"symbols": ",".join(symbols)}
        )
        out: dict[str, OptionContract] = {}
        for record in iter_records(raw):
            occ = pick(record, "symbol", "id")
            if not isinstance(occ, str):
                continue
            parsed = occ_parse(occ)
            if not parsed:
                continue
            contract = OptionContract(
                symbol=occ.upper(),
                underlying=parsed["underlying"],
                expiry=parsed["expiry"],
                right=parsed["right"],
                strike=parsed["strike"],
            )
            self._apply_snapshot(contract, record)
            out[occ.upper()] = contract
        return out

    # -- events (Section 3.4 event avoidance) ------------------------------
    async def corporate_actions(self, symbol: str, today: date, until: date) -> list[dict]:
        """
        Scheduled corporate actions on the underlying before expiry.

        NOTE: the Alpaca MCP server exposes Spinoff / Merger / Split / Reorg /
        Dividend announcements. It has no earnings calendar — see risk/README.md
        for how check 8 is satisfied for the ETF watchlist.
        """
        raw = await self.mcp.call(
            "get_corporate_action_announcements",
            {
                "ca_types": list(RISK.blocking_corporate_actions),
                "since": today.isoformat(),
                "until": until.isoformat(),
                "symbol": symbol,
                "date_type": "ex_date",
            },
        )
        records = iter_records(raw)
        return [r for r in records if str(pick(r, "symbol", "ticker", default=symbol)).upper()
                == symbol.upper() or "symbol" not in r]

    # -- composite ---------------------------------------------------------
    async def snapshot_underlying(self, symbol: str, today: date) -> UnderlyingSnapshot:
        closes = await self.daily_closes(symbol)
        spot = await self.spot(symbol)
        if spot is None and closes:
            spot = closes[-1]
        if spot is None:
            raise ValueError(f"No spot price available for {symbol}")

        contracts = await self.option_chain(symbol, spot, today)
        expiries = sorted({c.expiry for c in contracts})
        _, hi = self._eligible_expiries(today)
        actions = await self.corporate_actions(
            symbol, today, max(hi, today + timedelta(days=1))
        )
        return UnderlyingSnapshot(
            symbol=symbol,
            spot=spot,
            closes=closes,
            contracts=contracts,
            expiries=expiries,
            corporate_actions=actions,
        )
