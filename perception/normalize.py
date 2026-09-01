"""
Tolerant parsing helpers for MCP tool responses.

The Alpaca MCP server returns a mix of structured JSON and human-formatted text
depending on the tool. These helpers accept either shape and pull out typed
values, so the rest of the agent works against plain dataclasses. Key spellings
vary between snake_case, camelCase and Alpaca's short forms, so every lookup
tries a list of aliases.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any, Iterable


def as_obj(value: Any) -> Any:
    """Coerce a response into dict/list where possible."""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return json.loads(text)
            except (TypeError, ValueError):
                return value
    return value


#: The Alpaca MCP server wraps every payload in a security envelope:
#: {"_alpaca_mcp_security": {...}, "data": <actual payload>}
ENVELOPE_MARKER = "_alpaca_mcp_security"

#: Keys that hold a collection of records, in the order they should be tried.
CONTAINER_KEYS = (
    "positions", "orders", "contracts", "option_contracts", "snapshots",
    "bars", "trades", "quotes", "results", "items", "announcements",
    "corporate_action_announcements", "data", "result",
)


def unwrap(value: Any) -> Any:
    """
    Strip the MCP security envelope, returning the actual payload.

    The server returns {"_alpaca_mcp_security": {...}, "data": {...}}; the
    envelope carries a trust marker telling clients to treat tool output as
    data, not instructions. Only the payload is of interest downstream.
    """
    obj = as_obj(value)
    while isinstance(obj, dict) and ENVELOPE_MARKER in obj and "data" in obj:
        obj = as_obj(obj["data"])
    return obj


def pick(obj: Any, *names: str, default: Any = None) -> Any:
    """First present key among `names`, searching nested 'result'/'data' wrappers."""
    if isinstance(obj, dict) and ENVELOPE_MARKER in obj:
        obj = unwrap(obj)
    if not isinstance(obj, dict):
        return default
    for name in names:
        if name in obj and obj[name] is not None:
            return obj[name]
    for wrapper in ("result", "data", "snapshot", "snapshots", "account", "position"):
        inner = obj.get(wrapper)
        if isinstance(inner, dict):
            found = pick(inner, *names, default=None)
            if found is not None:
                return found
    return default


def to_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("$", "").replace("%", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return default
    return default


def to_int(value: Any, default: int | None = None) -> int | None:
    result = to_float(value, None)
    return int(result) if result is not None else default


def find_number_in_text(text: str, *labels: str) -> float | None:
    """Pull `Label: 12,345.67` out of a formatted text response."""
    for label in labels:
        match = re.search(
            rf"{re.escape(label)}\s*[:=]\s*\$?(-?[\d,]+\.?\d*)", text, re.IGNORECASE
        )
        if match:
            return to_float(match.group(1))
    return None


def iter_records(value: Any) -> list[dict]:
    """
    Normalize a response into a list of dict records.

    Handles the MCP security envelope, Alpaca's symbol-keyed containers
    (`{"trades": {"SPY": {...}}}`), and bare symbol->record mappings.
    """
    obj = unwrap(value)
    if isinstance(obj, list):
        return [r for r in obj if isinstance(r, dict)]
    if isinstance(obj, dict):
        for key in CONTAINER_KEYS:
            inner = obj.get(key)
            if isinstance(inner, list):
                return [r for r in inner if isinstance(r, dict)]
            if isinstance(inner, dict):
                # Alpaca keys snapshots/bars by symbol.
                out: list[dict] = []
                for sym, rec in inner.items():
                    if isinstance(rec, dict):
                        out.append({"symbol": sym, **rec})
                    elif isinstance(rec, list):
                        out.extend({"symbol": sym, **r} for r in rec if isinstance(r, dict))
                if out:
                    return out
        # A bare symbol->record mapping.
        if obj and all(isinstance(v, (dict, list)) for v in obj.values()):
            out = []
            for sym, rec in obj.items():
                if isinstance(rec, dict):
                    out.append({"symbol": sym, **rec})
                elif isinstance(rec, list):
                    out.extend({"symbol": sym, **r} for r in rec if isinstance(r, dict))
            return out
        return [obj]
    return []


def parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        text = value.strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(text[:10], fmt).date()
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def next_page_token(value: Any) -> str | None:
    obj = unwrap(value)
    if isinstance(obj, dict):
        token = pick(obj, "next_page_token", "nextPageToken", "page_token")
        if isinstance(token, str) and token:
            return token
    return None


def occ_parse(symbol: str) -> dict | None:
    """
    Decode an OCC option symbol, e.g. SPY260903P00640000.

    Returns underlying, expiry, right ('C'/'P') and strike.
    """
    match = re.fullmatch(r"([A-Z]{1,6})(\d{6})([CP])(\d{8})", symbol.strip().upper())
    if not match:
        return None
    root, yymmdd, right, strike = match.groups()
    try:
        expiry = datetime.strptime(yymmdd, "%y%m%d").date()
    except ValueError:
        return None
    return {
        "underlying": root,
        "expiry": expiry,
        "right": right,
        "strike": int(strike) / 1000.0,
    }


def first_of(records: Iterable[dict], **match: Any) -> dict | None:
    for record in records:
        if all(record.get(k) == v for k, v in match.items()):
            return record
    return None
