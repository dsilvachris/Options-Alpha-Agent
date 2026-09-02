"""
Single source of truth for every tunable value in the agent.

Nothing in this repository may hardcode a threshold, watchlist member, size cap
or interval. Import it from here. Section references point at docs/implementation.md.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# ---------------------------------------------------------------------------
# Timezone — the single source of truth for market time
# ---------------------------------------------------------------------------

#: All market-hours logic, scan scheduling, expiry/DTE arithmetic, session
#: boundaries and calendar handling use this zone. The machine's local timezone
#: is NEVER used: this agent runs on a host in Europe, and a local-time
#: assumption would silently shift every session boundary and DTE calculation.
MARKET_TZ = ZoneInfo("America/New_York")


def now_et() -> datetime:
    """Current time in market time."""
    return datetime.now(MARKET_TZ)


def today_et() -> date:
    """Today's date on the market calendar. Use instead of date.today()."""
    return now_et().date()


def to_et(value: datetime | str | None) -> datetime | None:
    """Convert a stored UTC timestamp (or ISO string) into market time."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MARKET_TZ)


def fmt_et(value: datetime | str | None, fmt: str = "%Y-%m-%d %H:%M:%S %Z") -> str:
    """
    Render a stored UTC timestamp in market time for human display.

    Timestamps are stored in UTC; every human-facing surface (CLI, decision
    cards, event log, dashboard) renders through this.
    """
    converted = to_et(value)
    return converted.strftime(fmt) if converted else "—"

# ---------------------------------------------------------------------------
# Environment / credentials
# ---------------------------------------------------------------------------


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Env:
    alpaca_api_key: str = os.environ.get("ALPACA_API_KEY", "")
    alpaca_secret_key: str = os.environ.get("ALPACA_SECRET_KEY", "")
    # Paper endpoint only. Production credentials are never wired.
    alpaca_base_url: str = os.environ.get(
        "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
    )
    dry_run: bool = _flag("DRY_RUN", True)

    @property
    def configured(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_secret_key)

    @property
    def is_paper(self) -> bool:
        return "paper-api" in self.alpaca_base_url

    def assert_paper(self) -> None:
        """Hard guard: refuse to run against a non-paper endpoint."""
        if not self.is_paper:
            raise RuntimeError(
                f"Refusing to run against non-paper endpoint {self.alpaca_base_url!r}. "
                "This agent is paper-only by design."
            )


ENV = Env()

# ---------------------------------------------------------------------------
# MCP server (perception + execution reach Alpaca only through this)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MCPConfig:
    #: Prefer the venv-local uvx so no activated shell is assumed.
    command: str = str(ROOT / ".venv" / "bin" / "uvx")
    args: tuple[str, ...] = ("alpaca-mcp-server",)
    #: Seconds to wait for the stdio session handshake.
    init_timeout: float = 180.0
    #: Seconds to wait for any single tool call.
    call_timeout: float = 120.0
    #: Reconnect policy for the long-running unattended loop.
    max_reconnect_attempts: int = 5
    reconnect_backoff_seconds: float = 2.0
    reconnect_backoff_max_seconds: float = 60.0
    #: Retries of an individual tool call before a reconnect is attempted.
    call_retries: int = 2

    def child_env(self) -> dict[str, str]:
        """Environment handed to the MCP subprocess. Keys come from .env only."""
        env = dict(os.environ)
        env.update(
            {
                "ALPACA_API_KEY": ENV.alpaca_api_key,
                "ALPACA_SECRET_KEY": ENV.alpaca_secret_key,
                "ALPACA_PAPER_TRADE": "True",
                "PAPER": "True",
                "ALPACA_BASE_URL": ENV.alpaca_base_url,
            }
        )
        return env


MCP = MCPConfig()

# ---------------------------------------------------------------------------
# Universe and expiry selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UniverseConfig:
    watchlist: tuple[str, ...] = ("SPY", "QQQ", "IWM")
    #: Section 8.3 passive baseline underlying.
    primary_underlying: str = "SPY"
    min_dte: int = 1
    max_dte: int = 3
    #: Latest eligible expiry, INCLUSIVE. DTE is counted in America/New_York
    #: calendar days (see config.today_et).
    expiry_cutoff: date = date(2026, 9, 4)
    #: Strike window around spot used when pulling the chain, as a fraction.
    chain_strike_window: float = 0.15
    #: Max option snapshots requested per chain page.
    chain_page_limit: int = 500
    #: Max chain pages to follow per underlying per cycle.
    chain_max_pages: int = 6
    #: Options data feed. None lets the server auto-select: it uses "opra" when
    #: the account has an OPRA subscription and "indicative" otherwise. Setting
    #: "opra" explicitly on an unsubscribed account returns HTTP 403.
    option_feed: str | None = None
    #: Target spread width in points between the short and long leg.
    #:
    #: This value trades the two liquidity-related checks against each other:
    #:   - WIDENING improves check 7 (liquidity). Leg bid-ask is roughly fixed
    #:     per leg, so a larger credit shrinks bid-ask as a percentage of it.
    #:   - WIDENING worsens check 5 (credit quality). Credit grows more slowly
    #:     than width, so credit/width falls as width grows.
    #: There is no theoretically correct value. Set it from the observed live
    #: score distribution (`cli.py scores`), not from reasoning in advance.
    spread_width_points: float = 5.0


UNIVERSE = UniverseConfig()

# ---------------------------------------------------------------------------
# Signals (Sections 3.1, 3.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalConfig:
    ma_short: int = 10
    ma_long: int = 30
    #: Section 3.2 / check 3 — MA separation as a fraction of price.
    trend_clarity_threshold: float = 0.005
    #: Realized-volatility lookback in trading days.
    realized_vol_window: int = 20
    #: Trading days per year used to annualize realized volatility.
    annualization_days: int = 252
    #: Daily bars pulled per underlying (enough for MA30 + RV20 with slack).
    bar_lookback_days: int = 120
    #: Check 2 — number of prior sessions of ATM IV averaged.
    iv_average_window: int = 3
    #: Section 3.3 depressed-premium branch says a debit spread is considered
    #: "only if trend conviction is high". The spec gives no number; conviction
    #: is defined here as MA separation exceeding this multiple of the trend
    #: clarity threshold (i.e. 1.0% of price at the defaults).
    high_conviction_multiple: float = 2.0


SIGNALS = SignalConfig()

# ---------------------------------------------------------------------------
# Opportunity evaluation (Section 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoringConfig:
    #: Check 1 — ATM IV must be at least this multiple of 20-day realized vol.
    premium_rich_iv_rv_ratio: float = 1.20
    #: Check 2 — today's ATM IV vs its own trailing average.
    iv_stability_max_ratio: float = 1.15
    #: Check 5 — credit / spread width floor.
    #: Calibrated from the 2026-09-01 observed distribution, not from a rule of
    #: thumb: at a 0.30 delta ceiling and 5.0-point width, live chains produced
    #: 0.149 (SPY) and 0.177 (QQQ), so the previous 0.30 floor failed 9/9 and
    #: discriminated nothing. 0.14 sits just below the observed range.
    min_credit_to_width: float = 0.14
    #: Check 6 — short-strike delta ceiling (absolute value).
    #: For a credit spread, credit/width is bounded roughly by the short
    #: strike's delta, so a 0.30 credit/width floor requires ~0.30 delta.
    #: These two must move together.
    max_short_delta: float = 0.30
    #: Check 7 — bid/ask spread as a fraction of credit, and OI floor per leg.
    max_spread_pct_of_credit: float = 0.25
    min_open_interest: int = 100

    points: dict[int, int] = field(
        default_factory=lambda: {
            1: 15,  # Premium rich
            2: 10,  # Volatility stable
            3: 15,  # Trend clarity
            4: 10,  # Directional agreement
            5: 15,  # Credit quality
            6: 10,  # Probability profile
            7: 10,  # Liquidity          (hard gate)
            8: 10,  # Event clear        (hard gate)
            9: 5,   # Portfolio fit      (hard gate)
        }
    )
    hard_gates: tuple[int, ...] = (7, 8, 9)

    #: Section 4.3 score bands.
    trade_band: int = 80
    watch_band: int = 60


SCORING = ScoringConfig()

# ---------------------------------------------------------------------------
# Risk (Section 3.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskConfig:
    #: Maximum possible loss on a single trade, as a fraction of account equity.
    max_loss_pct_of_equity: float = 0.02
    max_concurrent_positions: int = 4
    #: Check 9 references "directional exposure" without a number. Cap on open
    #: positions sharing the same directional bias (bullish or bearish).
    max_same_direction_positions: int = 3
    #: Exit discipline.
    profit_target_pct_of_credit: float = 0.50
    stop_loss_multiple_of_credit: float = 2.0
    #: Close at this DTE. -1 retires the DTE rule in favour of
    #: EXPIRY_DAY_CLOSE_TIME: a position expiring today is held through its
    #: expiry day and flattened at 15:30 ET, so the profit target and stop still
    #: govern it all morning instead of a 0-DTE reading closing it at the open.
    #:
    #: Nothing can reach this rule at -1. dte <= -1 means the expiry date has
    #: already passed, and past_expiry_day_close() returns True for any date
    #: before today — it fires first, ahead of the DTE arithmetic. The rule is
    #: kept as a floor for a future configuration, not as a live trigger.
    #:
    #: The same value gates entry in risk/rules.py ("expiry sanity"): at -1 that
    #: check no longer refuses a 0-DTE structure. Chain selection is what keeps
    #: 0-DTE out — UNIVERSE.min_dte = 1 — so that backstop is now single-layered.
    time_exit_dte: int = -1
    #: Daily circuit breaker.
    max_order_attempts_per_day: int = 10
    max_daily_drawdown_pct: float = 0.03
    #: Section 3.4 event avoidance — corporate action types treated as blocking.
    #: Values must be lowercase: the Alpaca API rejects capitalised types with
    #: HTTP 422 despite the MCP tool schema advertising them capitalised.
    #: "reorg" is NOT accepted by the API either — the only valid types are
    #: dividend, merger, split and spinoff. Routine ETF dividends are excluded
    #: deliberately; blocking on them would halt trading every quarter.
    blocking_corporate_actions: tuple[str, ...] = ("merger", "split", "spinoff")
    #: Underlyings known to be ETFs (no issuer earnings). See risk/README.md.
    etf_symbols: tuple[str, ...] = ("SPY", "QQQ", "IWM", "DIA", "XLF", "XLE")


RISK = RiskConfig()

# ---------------------------------------------------------------------------
# Loop / state machine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionConfig:
    """Order pricing and fill management."""

    #: Entries are submitted as LIMIT orders at the mid of the net spread price.
    #: The bid-ask is never crossed on entry.
    #: Number of price-improvement re-quotes after the initial mid-priced order
    #: before the attempt is abandoned. 0 disables re-quoting.
    requote_attempts: int = 3
    #: Seconds to wait for a fill before re-quoting.
    requote_wait_seconds: float = 20.0
    #: Each re-quote walks the limit price linearly from the mid toward the
    #: price that would cross the spread, in this many equal steps. With
    #: requote_attempts=3 the walk is mid -> 1/3 -> 2/3 -> crossing.
    #: The crossing price is never exceeded.
    requote_walk_to_cross: bool = True


EXECUTION = ExecutionConfig()


#: Force-close time on a position's OWN expiry date, in market time.
#: These are physically-settled ETF options: a short leg left open through
#: expiry can be assigned, so every position is flattened before the close on
#: the day it expires, whatever its P&L. HARD_CLOSE_AT remains the final
#: backstop for the end of the judging window.
EXPIRY_DAY_CLOSE_TIME = time(15, 30)


def past_expiry_day_close(expiry: date | str | None, when: datetime | None = None) -> bool:
    """True once a position must be flattened on account of its own expiry."""
    if expiry is None:
        return False
    if isinstance(expiry, str):
        try:
            expiry = date.fromisoformat(expiry[:10])
        except ValueError:
            return False
    now = when or now_et()
    if now.date() > expiry:
        return True
    if now.date() < expiry:
        return False
    return now.timetz().replace(tzinfo=None) >= EXPIRY_DAY_CLOSE_TIME


#: Hard end-of-window deadline. At or after this moment the agent force-closes
#: every open position regardless of P&L, and the risk gate refuses new entries,
#: so the judging window closes on realized cash rather than on a mark.
HARD_CLOSE_AT = datetime(2026, 9, 4, 15, 30, tzinfo=MARKET_TZ)


def past_hard_close(when: datetime | None = None) -> bool:
    """True once the end-of-window deadline has passed (market time)."""
    return (when or now_et()) >= HARD_CLOSE_AT


@dataclass(frozen=True)
class LoopConfig:
    #: Cadence while the market is OPEN.
    scan_interval_minutes: int = 15
    #: Section 5.1 — a WATCH item expires after this many scan cycles.
    watch_expiry_cycles: int = 2
    #: Cadence while the market is CLOSED. The pipeline still runs out of hours
    #: (the decision record and score distribution keep accumulating), but no
    #: order is submitted, so there is no reason to poll as hard. Raise this to
    #: scan less overnight; it is the real cadence, not a placeholder.
    closed_market_sleep_minutes: int = 15

    def interval_minutes(self, market_open: bool) -> int:
        return self.scan_interval_minutes if market_open else self.closed_market_sleep_minutes


LOOP = LoopConfig()

# ---------------------------------------------------------------------------
# Storage & dashboard
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StorageConfig:
    db_path: Path = ROOT / "data" / "agent.db"
    log_dir: Path = ROOT / "logs"


STORAGE = StorageConfig()


@dataclass(frozen=True)
class DashboardConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    #: Rows returned to the front end per panel.
    event_limit: int = 200
    mcp_call_limit: int = 200
    decision_limit: int = 100


DASHBOARD = DashboardConfig()


def summary() -> dict:
    """Flat view of the active configuration, surfaced by the CLI and dashboard."""
    return {
        "watchlist": list(UNIVERSE.watchlist),
        "dte_window": [UNIVERSE.min_dte, UNIVERSE.max_dte],
        "expiry_cutoff_inclusive": UNIVERSE.expiry_cutoff.isoformat(),
        "spread_width_points": UNIVERSE.spread_width_points,
        "market_timezone": str(MARKET_TZ),
        "today_et": today_et().isoformat(),
        "ma_periods": [SIGNALS.ma_short, SIGNALS.ma_long],
        "trend_clarity_threshold": SIGNALS.trend_clarity_threshold,
        "high_conviction_multiple": SIGNALS.high_conviction_multiple,
        "premium_rich_iv_rv_ratio": SCORING.premium_rich_iv_rv_ratio,
        "iv_stability_max_ratio": SCORING.iv_stability_max_ratio,
        "min_credit_to_width": SCORING.min_credit_to_width,
        "max_short_delta": SCORING.max_short_delta,
        "max_spread_pct_of_credit": SCORING.max_spread_pct_of_credit,
        "min_open_interest": SCORING.min_open_interest,
        "score_bands": {"trade": SCORING.trade_band, "watch": SCORING.watch_band},
        "max_loss_pct_of_equity": RISK.max_loss_pct_of_equity,
        "max_concurrent_positions": RISK.max_concurrent_positions,
        "max_same_direction_positions": RISK.max_same_direction_positions,
        "profit_target_pct_of_credit": RISK.profit_target_pct_of_credit,
        "stop_loss_multiple_of_credit": RISK.stop_loss_multiple_of_credit,
        "time_exit_dte": RISK.time_exit_dte,
        "hard_close_at": HARD_CLOSE_AT.isoformat(),
        "expiry_day_close_time": EXPIRY_DAY_CLOSE_TIME.isoformat(),
        "past_hard_close": past_hard_close(),
        "watch_expiry_cycles": LOOP.watch_expiry_cycles,
        "scan_interval_minutes": LOOP.scan_interval_minutes,
        "closed_market_sleep_minutes": LOOP.closed_market_sleep_minutes,
        "requote_attempts": EXECUTION.requote_attempts,
        "requote_wait_seconds": EXECUTION.requote_wait_seconds,
        "dry_run": ENV.dry_run,
        "base_url": ENV.alpaca_base_url,
        "credentials_configured": ENV.configured,
    }
