"""
Section 10 — financial-terminal dashboard server.

A small FastAPI app exposing the recorded backend state as JSON, plus the static
terminal UI. Every endpoint is a read of SQLite: the server computes no trading
decisions and the front end simulates nothing. If the agent has not recorded a
value, the dashboard shows it as absent.
"""
from __future__ import annotations

import bootstrap  # noqa: F401  - must precede `logging.*` submodule imports

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

import config
from dashboard import pipeline as pipeline_mod
from dashboard import views
from logging.store import get_store

STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Options Alpha Agent", docs_url="/api/docs")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/config.js")
def config_js() -> FileResponse:
    """Data-source selector. The published site ships a static-mode version."""
    return FileResponse(STATIC / "config.js", media_type="application/javascript")


@app.get("/api/state")
def state() -> JSONResponse:
    """Everything the terminal renders, in one poll."""
    store = get_store()
    return JSONResponse(
        {
            "meta": {
                "generated_at": config.now_et().isoformat(),
                "market_open": None,
                "mode": "live",
                "dry_run": config.ENV.dry_run,
            },
            "config": config.summary(),
            "pipeline": pipeline_mod.cycle_pipeline(store),
            "cycles": pipeline_mod.recent_cycles(store),
            "positions": views.positions_view(store),
            "equity": views.equity_view(store),
            "decisions": views.reasoning_log_view(store),
            "regimes": views.regime_view(store),
            "mcp": views.mcp_activity_view(store),
            "monitoring": views.monitoring_view(store),
            "watch": views.watch_view(store),
        }
    )


@app.get("/api/pipeline")
def pipeline(cycle_id: str | None = None) -> JSONResponse:
    return JSONResponse(pipeline_mod.cycle_pipeline(get_store(), cycle_id))


@app.get("/api/mcp")
def mcp(limit: int = 200) -> JSONResponse:
    return JSONResponse(views.mcp_activity_view(get_store(), limit))


@app.get("/api/decisions")
def decisions(limit: int = 100) -> JSONResponse:
    return JSONResponse(views.reasoning_log_view(get_store(), limit))


@app.get("/api/positions")
def positions() -> JSONResponse:
    return JSONResponse(views.positions_view(get_store()))


@app.get("/api/monitoring")
def monitoring() -> JSONResponse:
    return JSONResponse(views.monitoring_view(get_store()))


def serve(host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=host or config.DASHBOARD.host,
        port=port or config.DASHBOARD.port,
        log_level="info",
    )
