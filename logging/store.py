"""
SQLite persistence for the whole agent.

Every table here is written by the backend and read by the dashboard. The
dashboard renders recorded state only (Section 6), so anything the UI shows must
first land in one of these tables.

Tables
------
events        Pipeline stage events (Section 6) and lifecycle events.
mcp_calls     Every Alpaca MCP tool call: name, args, response summary, latency.
decisions     One row per evaluated opportunity, with its decision card.
watch_items   Persistent WATCH state (Section 5.1), re-evaluated across cycles.
positions     Agent-opened positions with entry credit and exit bookkeeping.
orders        Submitted orders keyed by client_order_id (idempotency).
baselines     Section 8.3 unfiltered + passive baseline rows.
iv_history    Daily ATM IV per underlying, for the check-2 trailing average.
equity_curve  Account equity samples across the judging window.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from config import STORAGE, today_et

_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    cycle_id      TEXT,
    stage         TEXT NOT NULL,
    symbol        TEXT,
    message       TEXT,
    payload       TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_cycle ON events(cycle_id);

CREATE TABLE IF NOT EXISTS mcp_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    cycle_id      TEXT,
    tool          TEXT NOT NULL,
    arguments     TEXT,
    response_summary TEXT,
    latency_ms    REAL,
    ok            INTEGER NOT NULL,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_mcp_ts ON mcp_calls(ts);

CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    cycle_id      TEXT,
    symbol        TEXT NOT NULL,
    state         TEXT NOT NULL,
    score         INTEGER,
    structure     TEXT,
    iv_condition  TEXT,
    trend_condition TEXT,
    reason        TEXT,
    card          TEXT,
    detail        TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS watch_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key             TEXT UNIQUE NOT NULL,
    symbol          TEXT NOT NULL,
    structure       TEXT,
    created_ts      TEXT NOT NULL,
    updated_ts      TEXT NOT NULL,
    cycles_seen     INTEGER NOT NULL DEFAULT 1,
    expires_after_cycle INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'WATCH',
    score           INTEGER,
    promoting_condition TEXT,
    resolution      TEXT,
    detail          TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    position_key    TEXT UNIQUE NOT NULL,
    symbol          TEXT NOT NULL,
    structure       TEXT NOT NULL,
    opened_ts       TEXT NOT NULL,
    closed_ts       TEXT,
    status          TEXT NOT NULL DEFAULT 'OPEN',
    -- Simulated (DRY_RUN) positions are flagged here, not only inside `detail`,
    -- so P&L and outcome queries can exclude them in SQL. Orders carry the same
    -- flag. Decisions deliberately do NOT: the reasoning log and activity ledger
    -- report every decision the agent made, simulated or not.
    dry_run         INTEGER NOT NULL DEFAULT 0,
    contracts       INTEGER NOT NULL,
    credit          REAL NOT NULL,
    width           REAL NOT NULL,
    max_loss        REAL NOT NULL,
    expiry          TEXT,
    legs            TEXT,
    entry_order_id  TEXT,
    exit_order_id   TEXT,
    exit_reason     TEXT,
    realized_pnl    REAL,
    detail          TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id  TEXT UNIQUE NOT NULL,
    ts               TEXT NOT NULL,
    trade_date       TEXT NOT NULL,
    symbol           TEXT,
    intent           TEXT,
    dry_run          INTEGER NOT NULL,
    status           TEXT,
    broker_order_id  TEXT,
    request          TEXT,
    response         TEXT
);

CREATE TABLE IF NOT EXISTS baselines (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    cycle_id      TEXT,
    kind          TEXT NOT NULL,
    symbol        TEXT,
    value         REAL,
    detail        TEXT
);

CREATE TABLE IF NOT EXISTS iv_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    observed_date TEXT NOT NULL,
    atm_iv        REAL NOT NULL,
    ts            TEXT NOT NULL,
    UNIQUE(symbol, observed_date)
);

CREATE TABLE IF NOT EXISTS equity_curve (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    equity        REAL NOT NULL,
    last_equity   REAL,
    cycle_id      TEXT
);

CREATE TABLE IF NOT EXISTS day_state (
    trade_date        TEXT PRIMARY KEY,
    order_attempts    INTEGER NOT NULL DEFAULT 0,
    starting_equity   REAL,
    halted            INTEGER NOT NULL DEFAULT 0,
    halt_reason       TEXT
);
"""


def utcnow() -> str:
    """Storage timestamp. Always UTC — rendering happens at the display layer."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_str() -> str:
    """
    The trading date, in market time.

    Session boundaries (daily order counts, circuit-breaker state) must roll at
    the US market day, not the host's local midnight.
    """
    return today_et().isoformat()


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


class Store:
    """Thin synchronous SQLite wrapper. Safe for the agent + dashboard reader."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or STORAGE.db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with _LOCK:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Additive migrations for stores created by an earlier version."""
        with _LOCK:
            columns = {r[1] for r in self._conn.execute(
                "PRAGMA table_info(positions)").fetchall()}
            if "dry_run" not in columns:
                self._conn.execute(
                    "ALTER TABLE positions ADD COLUMN dry_run INTEGER NOT NULL DEFAULT 0")
                # Backfill from the legacy location inside `detail`.
                for row in self._conn.execute(
                        "SELECT id, detail FROM positions").fetchall():
                    try:
                        detail = json.loads(row[1]) if row[1] else {}
                    except (TypeError, ValueError):
                        detail = {}
                    if detail.get("dry_run"):
                        self._conn.execute(
                            "UPDATE positions SET dry_run=1 WHERE id=?", (row[0],))
                self._conn.commit()

    # -- low level ---------------------------------------------------------
    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with _LOCK:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            finally:
                cur.close()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with _LOCK:
            cur = self._conn.execute(sql, tuple(params))
            rows = [dict(r) for r in cur.fetchall()]
            cur.close()
        return rows

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._cursor() as cur:
            cur.execute(sql, tuple(params))
            return cur.lastrowid

    # -- events ------------------------------------------------------------
    def add_event(
        self,
        stage: str,
        message: str = "",
        *,
        cycle_id: str | None = None,
        symbol: str | None = None,
        payload: Any = None,
    ) -> int:
        return self.execute(
            "INSERT INTO events (ts, cycle_id, stage, symbol, message, payload)"
            " VALUES (?,?,?,?,?,?)",
            (utcnow(), cycle_id, stage, symbol, message, _dumps(payload)),
        )

    def recent_events(self, limit: int = 200, cycle_id: str | None = None) -> list[dict]:
        if cycle_id:
            return self.query(
                "SELECT * FROM events WHERE cycle_id=? ORDER BY id DESC LIMIT ?",
                (cycle_id, limit),
            )
        return self.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # -- mcp calls ---------------------------------------------------------
    def add_mcp_call(
        self,
        tool: str,
        arguments: Any,
        response_summary: str,
        latency_ms: float,
        ok: bool,
        error: str | None = None,
        cycle_id: str | None = None,
    ) -> int:
        return self.execute(
            "INSERT INTO mcp_calls (ts, cycle_id, tool, arguments, response_summary,"
            " latency_ms, ok, error) VALUES (?,?,?,?,?,?,?,?)",
            (
                utcnow(),
                cycle_id,
                tool,
                _dumps(arguments),
                response_summary[:2000] if response_summary else "",
                latency_ms,
                1 if ok else 0,
                error,
            ),
        )

    def recent_mcp_calls(self, limit: int = 200) -> list[dict]:
        return self.query("SELECT * FROM mcp_calls ORDER BY id DESC LIMIT ?", (limit,))

    def mcp_call_stats(self) -> list[dict]:
        return self.query(
            "SELECT tool, COUNT(*) AS calls, SUM(ok) AS ok_calls,"
            " ROUND(AVG(latency_ms),1) AS avg_latency_ms"
            " FROM mcp_calls GROUP BY tool ORDER BY calls DESC"
        )

    # -- decisions ---------------------------------------------------------
    def add_decision(self, **kw: Any) -> int:
        return self.execute(
            "INSERT INTO decisions (ts, cycle_id, symbol, state, score, structure,"
            " iv_condition, trend_condition, reason, card, detail)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                kw.get("ts") or utcnow(),
                kw.get("cycle_id"),
                kw["symbol"],
                kw["state"],
                kw.get("score"),
                kw.get("structure"),
                kw.get("iv_condition"),
                kw.get("trend_condition"),
                kw.get("reason"),
                kw.get("card"),
                _dumps(kw.get("detail")),
            ),
        )

    def recent_decisions(self, limit: int = 100) -> list[dict]:
        return self.query("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))

    def all_decisions(self) -> list[dict]:
        return self.query("SELECT * FROM decisions ORDER BY id ASC")

    # -- watch items -------------------------------------------------------
    def get_watch_item(self, key: str) -> dict | None:
        rows = self.query("SELECT * FROM watch_items WHERE key=?", (key,))
        return rows[0] if rows else None

    def open_watch_items(self) -> list[dict]:
        return self.query(
            "SELECT * FROM watch_items WHERE status='WATCH' ORDER BY id ASC"
        )

    def all_watch_items(self, limit: int = 200) -> list[dict]:
        return self.query("SELECT * FROM watch_items ORDER BY id DESC LIMIT ?", (limit,))

    def upsert_watch_item(self, key: str, **kw: Any) -> dict:
        existing = self.get_watch_item(key)
        now = utcnow()
        if existing is None:
            self.execute(
                "INSERT INTO watch_items (key, symbol, structure, created_ts, updated_ts,"
                " cycles_seen, expires_after_cycle, status, score, promoting_condition, detail)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    kw["symbol"],
                    kw.get("structure"),
                    now,
                    now,
                    1,
                    kw["expires_after_cycle"],
                    "WATCH",
                    kw.get("score"),
                    kw.get("promoting_condition"),
                    _dumps(kw.get("detail")),
                ),
            )
        else:
            self.execute(
                "UPDATE watch_items SET updated_ts=?, cycles_seen=cycles_seen+1,"
                " score=?, promoting_condition=?, detail=? WHERE key=?",
                (
                    now,
                    kw.get("score"),
                    kw.get("promoting_condition"),
                    _dumps(kw.get("detail")),
                    key,
                ),
            )
        return self.get_watch_item(key)  # type: ignore[return-value]

    def resolve_watch_item(self, key: str, status: str, resolution: str) -> None:
        self.execute(
            "UPDATE watch_items SET status=?, resolution=?, updated_ts=? WHERE key=?",
            (status, resolution, utcnow(), key),
        )

    # -- positions ---------------------------------------------------------
    def add_position(self, **kw: Any) -> int:
        return self.execute(
            "INSERT OR IGNORE INTO positions (position_key, symbol, structure, opened_ts,"
            " status, dry_run, contracts, credit, width, max_loss, expiry, legs,"
            " entry_order_id, detail)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                kw["position_key"],
                kw["symbol"],
                kw["structure"],
                kw.get("opened_ts") or utcnow(),
                kw.get("status", "OPEN"),
                1 if kw.get("dry_run") else 0,
                kw["contracts"],
                kw["credit"],
                kw["width"],
                kw["max_loss"],
                kw.get("expiry"),
                _dumps(kw.get("legs")),
                kw.get("entry_order_id"),
                _dumps(kw.get("detail")),
            ),
        )

    def open_positions(self, include_dry_run: bool = True) -> list[dict]:
        """
        Open positions.

        Defaults to INCLUDING dry-run positions: the exit rules and the risk
        gate's portfolio-fit check must treat a simulated position exactly like a
        real one, otherwise dry runs would never exercise those paths. Reporting
        callers that must not mix simulated money into real results pass
        include_dry_run=False.
        """
        if include_dry_run:
            return self.query(
                "SELECT * FROM positions WHERE status='OPEN' ORDER BY id ASC")
        return self.query(
            "SELECT * FROM positions WHERE status='OPEN' AND dry_run=0 ORDER BY id ASC")

    def all_positions(self, include_dry_run: bool = True) -> list[dict]:
        if include_dry_run:
            return self.query("SELECT * FROM positions ORDER BY id DESC")
        return self.query("SELECT * FROM positions WHERE dry_run=0 ORDER BY id DESC")

    def closed_entry_count(self, structural_key: str) -> int:
        """
        Completed round trips for one structural identity.

        Drives the entry sequence in `execution.orders.position_key`: it advances
        only when a prior entry has CLOSED, so a genuine re-entry gets a fresh
        client_order_id while a retry of a still-open or never-recorded
        submission keeps the old one and collides as intended.

        Matched in Python rather than SQL LIKE: structural keys contain
        characters (`|`, `%`, `_`) that would need escaping in a LIKE pattern.
        """
        prefix = f"{structural_key}#"
        return sum(
            1 for row in self.query("SELECT position_key, status FROM positions")
            if str(row["position_key"]).startswith(prefix) and row["status"] == "CLOSED"
        )

    def dry_run_positions(self) -> list[dict]:
        return self.query("SELECT * FROM positions WHERE dry_run=1 ORDER BY id DESC")

    def close_position(
        self,
        position_key: str,
        exit_reason: str,
        realized_pnl: float | None,
        exit_order_id: str | None = None,
    ) -> None:
        self.execute(
            "UPDATE positions SET status='CLOSED', closed_ts=?, exit_reason=?,"
            " realized_pnl=?, exit_order_id=? WHERE position_key=?",
            (utcnow(), exit_reason, realized_pnl, exit_order_id, position_key),
        )

    # -- orders (idempotency) ---------------------------------------------
    def get_order(self, client_order_id: str) -> dict | None:
        rows = self.query(
            "SELECT * FROM orders WHERE client_order_id=?", (client_order_id,)
        )
        return rows[0] if rows else None

    def record_order(self, client_order_id: str, **kw: Any) -> None:
        """
        Record a submitted order.

        If a simulated row already exists under this client_order_id and a LIVE
        submission is now being recorded, the row is upgraded to dry_run=0 rather
        than ignored. `client_order_id` is derived from position identity, intent
        and date, so a dry-run scan and a live run on the same day collide; if
        the row kept its dry_run flag the executor would go on treating a real,
        transmitted order as simulated and could submit it twice.
        """
        existing = self.get_order(client_order_id)
        if existing is not None:
            if existing.get("dry_run") and not kw.get("dry_run"):
                self.execute(
                    "UPDATE orders SET dry_run=0, ts=?, trade_date=?, symbol=?,"
                    " intent=?, status=?, request=? WHERE client_order_id=?",
                    (
                        utcnow(), today_str(), kw.get("symbol"), kw.get("intent"),
                        kw.get("status", "SUBMITTED"), _dumps(kw.get("request")),
                        client_order_id,
                    ),
                )
            return
        self.execute(
            "INSERT INTO orders (client_order_id, ts, trade_date, symbol,"
            " intent, dry_run, status, broker_order_id, request, response)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                client_order_id,
                utcnow(),
                today_str(),
                kw.get("symbol"),
                kw.get("intent"),
                1 if kw.get("dry_run") else 0,
                kw.get("status", "SUBMITTED"),
                kw.get("broker_order_id"),
                _dumps(kw.get("request")),
                _dumps(kw.get("response")),
            ),
        )

    def update_order(self, client_order_id: str, **kw: Any) -> None:
        self.execute(
            "UPDATE orders SET status=?, broker_order_id=?, response=?"
            " WHERE client_order_id=?",
            (
                kw.get("status"),
                kw.get("broker_order_id"),
                _dumps(kw.get("response")),
                client_order_id,
            ),
        )

    def orders_today(self) -> list[dict]:
        return self.query(
            "SELECT * FROM orders WHERE trade_date=? ORDER BY id ASC", (today_str(),)
        )

    # -- day state / circuit breaker --------------------------------------
    def day_state(self, trade_date: str | None = None) -> dict:
        d = trade_date or today_str()
        rows = self.query("SELECT * FROM day_state WHERE trade_date=?", (d,))
        if rows:
            return rows[0]
        self.execute("INSERT OR IGNORE INTO day_state (trade_date) VALUES (?)", (d,))
        return self.query("SELECT * FROM day_state WHERE trade_date=?", (d,))[0]

    def set_day_starting_equity(self, equity: float) -> None:
        state = self.day_state()
        if state.get("starting_equity") is None:
            self.execute(
                "UPDATE day_state SET starting_equity=? WHERE trade_date=?",
                (equity, today_str()),
            )

    def increment_order_attempts(self) -> int:
        self.day_state()
        self.execute(
            "UPDATE day_state SET order_attempts=order_attempts+1 WHERE trade_date=?",
            (today_str(),),
        )
        return int(self.day_state()["order_attempts"])

    def halt_day(self, reason: str) -> None:
        self.day_state()
        self.execute(
            "UPDATE day_state SET halted=1, halt_reason=? WHERE trade_date=?",
            (reason, today_str()),
        )

    # -- baselines / iv history / equity ----------------------------------
    def add_baseline(
        self,
        kind: str,
        symbol: str | None,
        value: float | None,
        detail: Any = None,
        cycle_id: str | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO baselines (ts, cycle_id, kind, symbol, value, detail)"
            " VALUES (?,?,?,?,?,?)",
            (utcnow(), cycle_id, kind, symbol, value, _dumps(detail)),
        )

    def baseline_rows(self, kind: str | None = None) -> list[dict]:
        if kind:
            return self.query(
                "SELECT * FROM baselines WHERE kind=? ORDER BY id ASC", (kind,)
            )
        return self.query("SELECT * FROM baselines ORDER BY id ASC")

    def record_atm_iv(self, symbol: str, observed_date: str, atm_iv: float) -> None:
        self.execute(
            "INSERT INTO iv_history (symbol, observed_date, atm_iv, ts)"
            " VALUES (?,?,?,?) ON CONFLICT(symbol, observed_date)"
            " DO UPDATE SET atm_iv=excluded.atm_iv, ts=excluded.ts",
            (symbol, observed_date, atm_iv, utcnow()),
        )

    def recent_atm_iv(self, symbol: str, before_date: str, limit: int) -> list[float]:
        rows = self.query(
            "SELECT atm_iv FROM iv_history WHERE symbol=? AND observed_date < ?"
            " ORDER BY observed_date DESC LIMIT ?",
            (symbol, before_date, limit),
        )
        return [float(r["atm_iv"]) for r in rows]

    def add_equity_sample(
        self, equity: float, last_equity: float | None, cycle_id: str | None = None
    ) -> None:
        self.execute(
            "INSERT INTO equity_curve (ts, equity, last_equity, cycle_id) VALUES (?,?,?,?)",
            (utcnow(), equity, last_equity, cycle_id),
        )

    def equity_curve(self, limit: int = 1000) -> list[dict]:
        return self.query(
            "SELECT * FROM equity_curve ORDER BY id ASC LIMIT ?", (limit,)
        )


_STORE: Store | None = None


def get_store() -> Store:
    """Process-wide singleton store."""
    global _STORE
    if _STORE is None:
        _STORE = Store()
    return _STORE
