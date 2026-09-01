"""
Section 10 — dashboard view builders.

Each function returns plain JSON-serializable state assembled from recorded
backend tables. No view computes a trading decision, and none of them simulate
data the agent did not record.
"""
from __future__ import annotations

import bootstrap  # noqa: F401  - must precede `logging.*` submodule imports

import json
from typing import Any

from config import DASHBOARD, RISK, UNIVERSE
from logging.events import Stage
from logging.store import Store
from monitoring import baseline as baseline_mod
from monitoring import ledger as ledger_mod
from monitoring import outcomes as outcomes_mod


def _loads(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def positions_view(store: Store) -> dict:
    """
    Open positions with mark-to-market, plus closed history.

    Every row carries its `dry_run` flag so the terminal can label simulated
    positions. Counts are split live/simulated: `open_count` is LIVE only, so
    the header slot counter never implies real exposure that does not exist.
    """
    positions = store.all_positions()
    open_rows = []
    for position in positions:
        if position["status"] != "OPEN":
            continue
        detail = _loads(position.get("detail")) or {}
        open_rows.append(
            {
                "dry_run": bool(position.get("dry_run") or detail.get("dry_run")),
                "symbol": position["symbol"],
                "structure": position["structure"],
                "contracts": position["contracts"],
                "credit": position["credit"],
                "width": position["width"],
                "max_loss": position["max_loss"],
                "expiry": position["expiry"],
                "opened_ts": position["opened_ts"],
                "legs": _loads(position.get("legs")),
                "exit_plan": detail.get("exit_plan"),
                "breakevens": detail.get("breakevens"),
                "reconciled": bool(detail.get("reconciled")),
            }
        )
    closed_rows = [
        {
            "dry_run": bool(p.get("dry_run")),
            "symbol": p["symbol"],
            "structure": p["structure"],
            "contracts": p["contracts"],
            "credit": p["credit"],
            "realized_pnl": p["realized_pnl"],
            "exit_reason": p["exit_reason"],
            "closed_ts": p["closed_ts"],
        }
        for p in positions
        if p["status"] == "CLOSED"
    ]
    live_open = [r for r in open_rows if not r["dry_run"]]
    sim_open = [r for r in open_rows if r["dry_run"]]
    return {
        "open": open_rows,
        "closed": closed_rows,
        # LIVE counts only — simulated positions never imply real exposure.
        "open_count": len(live_open),
        "dry_run_open_count": len(sim_open),
        "dry_run_closed_count": sum(1 for r in closed_rows if r["dry_run"]),
        "limit": RISK.max_concurrent_positions,
    }


def equity_view(store: Store) -> dict:
    """
    Account equity curve with the Section 8.3 baselines alongside it.

    The equity series is sampled from Alpaca's own `get_account_info` on every
    cycle, so it is real broker equity by construction and needs no dry-run
    filtering: a simulated position places no order and therefore never moves it.
    The unfiltered baseline is explicitly notional (Section 8.3) and is labelled
    as such in the UI rather than plotted as money.
    """
    curve = store.equity_curve()
    passive = store.baseline_rows(baseline_mod.PASSIVE)
    unfiltered = store.baseline_rows(baseline_mod.UNFILTERED)

    passive_points = [{"ts": r["ts"], "value": r["value"]} for r in passive]
    base = passive_points[0]["value"] if passive_points else None
    passive_indexed = (
        [{"ts": p["ts"], "pct": (p["value"] - base) / base * 100.0} for p in passive_points]
        if base else []
    )

    equity_points = [{"ts": r["ts"], "equity": r["equity"]} for r in curve]
    eq_base = equity_points[0]["equity"] if equity_points else None
    equity_indexed = (
        [{"ts": p["ts"], "pct": (p["equity"] - eq_base) / eq_base * 100.0} for p in equity_points]
        if eq_base else []
    )

    running = 0.0
    unfiltered_points = []
    for row in unfiltered:
        detail = _loads(row.get("detail")) or {}
        running += float(detail.get("credit") or 0.0)
        unfiltered_points.append({"ts": row["ts"], "value": running})

    return {
        "equity": equity_points,
        "equity_indexed": equity_indexed,
        "passive": passive_points,
        "passive_indexed": passive_indexed,
        "passive_symbol": UNIVERSE.primary_underlying,
        "unfiltered_notional": unfiltered_points,
    }


def reasoning_log_view(store: Store, limit: int | None = None) -> list[dict]:
    """Chronological decision cards — trades taken, watched and declined."""
    rows = store.recent_decisions(limit or DASHBOARD.decision_limit)
    return [
        {
            "id": r["id"],
            "ts": r["ts"],
            "cycle_id": r["cycle_id"],
            "symbol": r["symbol"],
            "state": r["state"],
            "score": r["score"],
            "structure": r["structure"],
            "iv_condition": r["iv_condition"],
            "trend_condition": r["trend_condition"],
            "reason": r["reason"],
            "card": r["card"],
            "detail": _loads(r.get("detail")),
        }
        for r in rows
    ]


def regime_view(store: Store) -> list[dict]:
    """Latest recorded volatility and trend reading per watchlist underlying."""
    out = []
    for symbol in UNIVERSE.watchlist:
        rows = store.query(
            "SELECT * FROM events WHERE stage=? AND symbol=? ORDER BY id DESC LIMIT 1",
            (Stage.MARKET_ANALYSIS, symbol),
        )
        if not rows:
            out.append({"symbol": symbol, "status": "no reading recorded yet"})
            continue
        payload = _loads(rows[0].get("payload")) or {}
        out.append({
            "symbol": symbol,
            "ts": rows[0]["ts"],
            "iv_condition": payload.get("iv_condition"),
            "trend_condition": payload.get("trend_condition"),
            "atm_iv": payload.get("atm_iv"),
            "realized_vol": payload.get("realized_vol"),
            "iv_rv_ratio": payload.get("iv_rv_ratio"),
            "ma_short": payload.get("ma_short"),
            "ma_long": payload.get("ma_long"),
            "separation": payload.get("separation"),
        })
    return out


def mcp_activity_view(store: Store, limit: int | None = None) -> dict:
    """
    MCP Activity panel — the live Alpaca MCP tool call stream.

    This is the evidence that every Alpaca interaction goes through the MCP
    server: tool name, arguments, response summary, latency and timestamp.
    """
    calls = store.recent_mcp_calls(limit or DASHBOARD.mcp_call_limit)
    return {
        "calls": [
            {
                "id": c["id"],
                "ts": c["ts"],
                "cycle_id": c["cycle_id"],
                "tool": c["tool"],
                "arguments": _loads(c.get("arguments")),
                "response_summary": c["response_summary"],
                "latency_ms": c["latency_ms"],
                "ok": bool(c["ok"]),
                "error": c["error"],
            }
            for c in calls
        ],
        "stats": store.mcp_call_stats(),
        "total": store.query("SELECT COUNT(*) AS n FROM mcp_calls")[0]["n"],
    }


def monitoring_view(store: Store) -> dict:
    """Section 8 panels: activity ledger, outcomes and baselines."""
    return {
        "ledger": ledger_mod.build(store).to_dict(),
        "outcomes": outcomes_mod.build(store).to_dict(),
        "baselines": baseline_mod.build(store).to_dict(),
    }


def watch_view(store: Store) -> list[dict]:
    """WATCH items and their lifecycle state (Section 5.1)."""
    return [
        {
            "key": w["key"],
            "symbol": w["symbol"],
            "structure": w["structure"],
            "status": w["status"],
            "score": w["score"],
            "cycles_seen": w["cycles_seen"],
            "expires_after_cycle": w["expires_after_cycle"],
            "promoting_condition": w["promoting_condition"],
            "resolution": w["resolution"],
            "updated_ts": w["updated_ts"],
        }
        for w in store.all_watch_items(limit=100)
    ]
