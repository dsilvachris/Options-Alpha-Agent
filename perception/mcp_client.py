"""
Alpaca MCP session manager.

This is the only place in the repository that talks to Alpaca. perception/ and
execution/ call Alpaca exclusively through `AlpacaMCP.call()`; there is no
alpaca-py, requests or httpx dependency anywhere in either package.

Design points
-------------
* One long-lived stdio session against `uvx alpaca-mcp-server`, held open across
  scan cycles by an AsyncExitStack.
* Credentials are read from .env by config.py and injected into the subprocess
  environment. They are never written to any file in the repo.
* Reconnect handling: a dropped subprocess is detected on the next call, the
  session is torn down and re-established with exponential backoff, and the call
  is retried. The overnight loop survives a server death.
* Every tool call is recorded in the `mcp_calls` table with tool name, arguments,
  a response summary, latency and timestamp — this is demo evidence for the
  judges, surfaced in the dashboard's MCP Activity panel.
"""
from __future__ import annotations

import bootstrap  # noqa: F401  - must precede `logging.*` submodule imports

import asyncio
import json
import re
import time
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from config import ENV, MCP
from logging.events import EventLog, Stage
from logging.store import Store, get_store


class MCPError(RuntimeError):
    """Raised when a tool call fails after retries and reconnects."""


class MCPToolMissing(MCPError):
    """Raised when the server does not expose a tool the spec requires."""


#: Response keys whose values identify the account. These are scrubbed before a
#: response summary is persisted, because the mcp_calls table is rendered in the
#: dashboard's MCP Activity panel, which is demoed and screenshotted.
_ACCOUNT_KEYS = ("account_number", "account_id", "accountnumber")

#: Tools whose top-level "id" is the account UUID rather than an order/asset id.
_ACCOUNT_ID_TOOLS = frozenset({"get_account_info", "get_account_config",
                               "update_account_config"})


def _redact_identifiers(text: str, tool: str = "") -> str:
    """Scrub account identifiers from a response summary before it is stored."""
    for key in _ACCOUNT_KEYS:
        text = re.sub(
            rf'("{key}"\s*:\s*)"[^"]*"', r'\1"[REDACTED]"', text, flags=re.IGNORECASE
        )
        text = re.sub(
            rf'({key}\s*[:=]\s*)([^\s,;}}]+)', r'\1[REDACTED]', text, flags=re.IGNORECASE
        )
    if tool in _ACCOUNT_ID_TOOLS:
        text = re.sub(r'("id"\s*:\s*)"[^"]*"', r'\1"[REDACTED]"', text)
    # Alpaca paper account-number tokens.
    text = re.sub(r"\bPA[A-Z0-9]{6,}\b", "[REDACTED]", text)
    return text


def _summarize(value: Any, limit: int = 600, tool: str = "") -> str:
    try:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    text = " ".join(text.split())
    text = _redact_identifiers(text, tool)
    return text if len(text) <= limit else text[:limit] + f"... (+{len(text)-limit} chars)"


def _extract(result: Any) -> Any:
    """
    Normalize an MCP CallToolResult into plain Python.

    FastMCP servers may return structured content, text content, or both. Prefer
    structured; fall back to parsing JSON out of the text blocks; finally return
    the raw text.
    """
    for attr in ("structuredContent", "structured_content"):
        structured = getattr(result, attr, None)
        if structured:
            # FastMCP wraps bare return values under a "result" key.
            if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
                return structured["result"]
            return structured

    chunks: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            chunks.append(text)
    if not chunks:
        return None
    joined = "\n".join(chunks)
    try:
        return json.loads(joined)
    except (TypeError, ValueError):
        return joined


class AlpacaMCP:
    """Long-lived Alpaca MCP stdio session with reconnect and call logging."""

    def __init__(self, store: Store | None = None, events: EventLog | None = None) -> None:
        self.store = store or get_store()
        self.events = events
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._tools: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self.connect_count = 0

    # -- lifecycle ---------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._session is not None

    def bind_events(self, events: EventLog | None) -> None:
        """Point subsequent call logging at the current cycle."""
        self.events = events

    @property
    def _cycle_id(self) -> str | None:
        return self.events.cycle_id if self.events else None

    async def connect(self) -> None:
        if self._session is not None:
            return
        if not ENV.configured:
            raise MCPError(
                "ALPACA_API_KEY / ALPACA_SECRET_KEY are not set. Create .env from "
                ".env.example and fill in your paper credentials."
            )
        ENV.assert_paper()

        stack = AsyncExitStack()
        try:
            params = StdioServerParameters(
                command=MCP.command,
                args=list(MCP.args),
                env=MCP.child_env(),
            )
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), timeout=MCP.init_timeout)
            listing = await asyncio.wait_for(session.list_tools(), timeout=MCP.init_timeout)
        except BaseException:
            await stack.aclose()
            raise

        self._stack = stack
        self._session = session
        self._tools = {t.name: t for t in listing.tools}
        self.connect_count += 1

        self.store.add_event(
            Stage.MCP,
            f"MCP session established ({len(self._tools)} tools available)",
            cycle_id=self._cycle_id,
            payload={
                "command": f"{MCP.command} {' '.join(MCP.args)}",
                "tool_count": len(self._tools),
                "connect_count": self.connect_count,
            },
        )

    async def disconnect(self) -> None:
        stack, self._stack = self._stack, None
        self._session = None
        self._tools = {}
        if stack is not None:
            try:
                await stack.aclose()
            except BaseException:
                # A dead subprocess frequently raises on teardown; the session is
                # gone either way and a fresh one will be created on reconnect.
                pass

    async def _reconnect(self, reason: str) -> None:
        await self.disconnect()
        delay = MCP.reconnect_backoff_seconds
        last: BaseException | None = None
        for attempt in range(1, MCP.max_reconnect_attempts + 1):
            self.store.add_event(
                Stage.MCP,
                f"Reconnecting MCP session (attempt {attempt}/{MCP.max_reconnect_attempts})",
                cycle_id=self._cycle_id,
                payload={"reason": reason, "delay_seconds": delay},
            )
            try:
                await self.connect()
                return
            except BaseException as exc:  # noqa: BLE001 - retried below
                last = exc
                await self.disconnect()
                await asyncio.sleep(delay)
                delay = min(delay * 2, MCP.reconnect_backoff_max_seconds)
        raise MCPError(f"MCP reconnect failed after {MCP.max_reconnect_attempts} attempts: {last}")

    async def __aenter__(self) -> "AlpacaMCP":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.disconnect()

    # -- tool discovery ----------------------------------------------------
    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    def require(self, *tools: str) -> None:
        """Fail loudly if the server lacks a tool the spec depends on."""
        missing = [t for t in tools if t not in self._tools]
        if missing:
            raise MCPToolMissing(
                "Alpaca MCP server does not expose required tool(s): "
                + ", ".join(missing)
                + ". The agent will not fall back to a REST client."
            )

    # -- calling -----------------------------------------------------------
    async def call(self, tool: str, arguments: dict | None = None) -> Any:
        """
        Invoke an MCP tool, logging the call and recovering from a dead session.

        Retries transient failures, then reconnects and retries once more.
        """
        args = arguments or {}
        async with self._lock:
            if self._session is None:
                await self.connect()
            if tool not in self._tools:
                raise MCPToolMissing(
                    f"Alpaca MCP server does not expose required tool {tool!r}. "
                    f"Available: {', '.join(self.tool_names()[:20])}..."
                )

            attempts = MCP.call_retries + 1
            last_error: BaseException | None = None

            for attempt in range(1, attempts + 1):
                started = time.perf_counter()
                try:
                    assert self._session is not None
                    result = await asyncio.wait_for(
                        self._session.call_tool(tool, args), timeout=MCP.call_timeout
                    )
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    payload = _extract(result)

                    if getattr(result, "isError", False) or getattr(result, "is_error", False):
                        message = _summarize(payload, tool=tool)
                        self.store.add_mcp_call(
                            tool, args, message, latency_ms, ok=False,
                            error=message, cycle_id=self._cycle_id,
                        )
                        raise MCPError(f"{tool} returned an error: {message}")

                    self.store.add_mcp_call(
                        tool, args, _summarize(payload, tool=tool), latency_ms, ok=True,
                        cycle_id=self._cycle_id,
                    )
                    return payload

                except MCPError:
                    raise
                except BaseException as exc:  # noqa: BLE001 - classified below
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    last_error = exc
                    self.store.add_mcp_call(
                        tool, args, "", latency_ms, ok=False,
                        error=f"{type(exc).__name__}: {exc}", cycle_id=self._cycle_id,
                    )
                    if attempt < attempts:
                        await asyncio.sleep(0.5 * attempt)
                        continue
                    # Out of in-session retries: the transport is likely dead.
                    await self._reconnect(reason=f"{type(exc).__name__}: {exc}")
                    started = time.perf_counter()
                    try:
                        assert self._session is not None
                        result = await asyncio.wait_for(
                            self._session.call_tool(tool, args), timeout=MCP.call_timeout
                        )
                        latency_ms = (time.perf_counter() - started) * 1000.0
                        payload = _extract(result)
                        self.store.add_mcp_call(
                            tool, args, _summarize(payload, tool=tool), latency_ms, ok=True,
                            cycle_id=self._cycle_id,
                        )
                        return payload
                    except BaseException as exc2:  # noqa: BLE001
                        latency_ms = (time.perf_counter() - started) * 1000.0
                        self.store.add_mcp_call(
                            tool, args, "", latency_ms, ok=False,
                            error=f"post-reconnect {type(exc2).__name__}: {exc2}",
                            cycle_id=self._cycle_id,
                        )
                        raise MCPError(f"{tool} failed after reconnect: {exc2}") from exc2

            raise MCPError(f"{tool} failed: {last_error}")
