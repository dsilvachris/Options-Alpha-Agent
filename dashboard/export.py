"""
Static snapshot export for hosting the dashboard on Vercel.

The agent runs locally against SQLite; the hosted site is a static copy of what
the dashboard reads. `build_snapshot()` produces exactly the payload the live
`/api/state` endpoint returns, so the front end uses one code path for both and
no UI is duplicated.

Safety
------
The exported files land in a PUBLIC repository. `assert_clean()` scans the
serialized payload for credentials and account identifiers and raises
`RedactionError` rather than writing anything. It reuses the same scrub as the
MCP call logger, plus a check for the literal key values from the environment.

Filtering
---------
The same live-only / dry-run filtering the dashboard already applies is used
here, because it is the same code: `views.positions_view`, `monitoring.outcomes`
and the rest are called directly rather than reimplemented.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import bootstrap  # noqa: F401  - must precede `logging.*` submodule imports
import config
from datetime import timezone

from config import DASHBOARD, ENV, MARKET_TZ, fmt_et, now_et
from dashboard import pipeline as pipeline_mod
from dashboard import views
from logging.events import Stage
from logging.store import Store
from perception.mcp_client import _redact_identifiers

#: Where the published site is assembled. Served by Vercel as the site root.
PUBLIC_DIR = Path(__file__).resolve().parent / "public"
DATA_DIR = PUBLIC_DIR / "data"


class RedactionError(RuntimeError):
    """Raised when the export would leak a credential or account identifier."""


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

#: Alpaca account-number tokens, e.g. PA3ABCD1234X.
_ACCOUNT_NUMBER = re.compile(r"\bPA[A-Z0-9]{6,}\b")
#: Alpaca key ids begin with PK (paper) or AK (live).
_KEY_ID = re.compile(r"\b(?:PK|AK)[A-Z0-9]{10,}\b")
#: JSON keys that must never carry a real value in a published file.
_FORBIDDEN_KEYS = (
    "account_number", "account_id", "accountnumber", "api_key", "secret_key",
    "alpaca_api_key", "alpaca_secret_key", "authorization", "apca_api_key_id",
    "apca_api_secret_key",
)
_REDACTED_MARKERS = ("[REDACTED]", "[REDACTED-UUID]", "***")


def _mask(value: str, keep: int = 4) -> str:
    """
    Show enough of a found secret to identify it, never enough to use it.

    A redaction failure must not itself print the credential it caught — the
    message goes to a terminal, a log, and possibly CI output.
    """
    value = str(value)
    return f"{value[:keep]}...{len(value)} chars" if len(value) > keep else "***"


def _forbidden_key_hits(text: str) -> list[str]:
    hits: list[str] = []
    for key in _FORBIDDEN_KEYS:
        for match in re.finditer(rf'"{re.escape(key)}"\s*:\s*("([^"]*)"|[^,}}\s]+)',
                                 text, re.IGNORECASE):
            value = (match.group(2) or match.group(1) or "").strip()
            if value and value not in _REDACTED_MARKERS and value.lower() not in ("null", "none", ""):
                hits.append(f"{key}={_mask(value)}")
    return hits


def assert_clean(payload: Any) -> None:
    """
    Refuse to publish anything containing a credential or account identifier.

    Raises RedactionError listing every hit. This runs on the serialized text,
    so it catches values nested anywhere — including inside recorded MCP
    arguments and response summaries.
    """
    text = json.dumps(payload, default=str)
    problems: list[str] = []

    for name, secret in (("ALPACA_API_KEY", ENV.alpaca_api_key),
                         ("ALPACA_SECRET_KEY", ENV.alpaca_secret_key)):
        if secret and secret in text:
            problems.append(f"{name} value appears in the export")

    for match in set(_ACCOUNT_NUMBER.findall(text)):
        problems.append(f"account-number token {_mask(match)}")
    for match in set(_KEY_ID.findall(text)):
        problems.append(f"API key id {_mask(match)}")
    problems.extend(_forbidden_key_hits(text))

    if problems:
        raise RedactionError(
            "Refusing to publish — the export contains "
            + str(len(problems))
            + " sensitive item(s):\n  "
            + "\n  ".join(sorted(set(problems)))
        )


def scrub(payload: Any) -> Any:
    """
    Defence in depth: re-run the MCP identifier scrub over recorded strings.

    Response summaries are already scrubbed when written, but an older row may
    predate that, and this file is going somewhere public.
    """
    if isinstance(payload, dict):
        return {k: scrub(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [scrub(v) for v in payload]
    if isinstance(payload, str):
        return _redact_identifiers(payload)
    return payload


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


@dataclass
class ExportResult:
    files: list[tuple[Path, int]] = field(default_factory=list)
    digest: str = ""
    changed: bool = True
    generated_at: str = ""
    market_open: bool | None = None

    def render(self) -> str:
        lines = [f"generated_at : {self.generated_at}",
                 f"market       : "
                 f"{'OPEN' if self.market_open else 'CLOSED' if self.market_open is not None else 'unknown'}",
                 f"digest       : {self.digest[:16]}",
                 f"changed      : {self.changed}", ""]
        total = 0
        for path, size in self.files:
            total += size
            lines.append(f"  {size:>9,} B  {path.relative_to(PUBLIC_DIR.parent)}")
        lines.append(f"  {'-' * 9}")
        lines.append(f"  {total:>9,} B  total across {len(self.files)} file(s)")
        return "\n".join(lines)


def _market_state(store: Store) -> bool | None:
    """Most recent recorded market state, from the last cycle's scan event."""
    rows = store.query(
        "SELECT payload FROM events WHERE stage=? AND payload LIKE '%market_open%'"
        " ORDER BY id DESC LIMIT 1", (Stage.MARKET_SCAN,))
    if not rows:
        return None
    try:
        return bool(json.loads(rows[0]["payload"]).get("market_open"))
    except (TypeError, ValueError):
        return None


def build_snapshot(store: Store) -> dict:
    """
    The full dashboard payload, identical in shape to GET /api/state.

    One shape for both modes means the front end switches only its data source.
    """
    market_open = _market_state(store)
    snapshot = {
        "meta": {
            "generated_at": now_et().isoformat(),
            "generated_at_display": fmt_et(now_et().astimezone(), "%H:%M %Z"),
            "generated_at_utc": now_et().astimezone(timezone.utc).isoformat(),
            "timezone": str(MARKET_TZ),
            "market_open": market_open,
            "mode": "static",
            "dry_run": ENV.dry_run,
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
    # config.summary() reports whether credentials are set; never publish even
    # that inference alongside an endpoint.
    snapshot["config"].pop("credentials_configured", None)
    return scrub(snapshot)


#: Panels excluded from the change digest.
#:
#: `meta` carries the generation timestamp, which changes every run by
#: definition. `pipeline` and `cycles` are derived from the events table — and
#: publishing itself writes an event, so including them would make every publish
#: dirty the next one and produce a commit per cycle, which is exactly what the
#: digest exists to prevent. A real scan still moves `decisions`, `positions`,
#: `regimes`, `mcp` and `monitoring`, so genuine changes are never missed.
_VOLATILE_PANELS = ("meta", "pipeline", "cycles")


def material_digest(snapshot: dict) -> str:
    """
    Stable hash of the substantive payload.

    Used by `loop --publish` so an unchanged snapshot produces no commit.
    """
    import hashlib

    body = {k: v for k, v in snapshot.items() if k not in _VOLATILE_PANELS}
    body["_market_open"] = snapshot.get("meta", {}).get("market_open")
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode()
    ).hexdigest()


#: Individual panel files, so a consumer can fetch just what it needs.
PANEL_FILES = {
    "decisions.json":  lambda s: {"decisions": s["decisions"], "watch": s["watch"]},
    "positions.json":  lambda s: s["positions"],
    "equity.json":     lambda s: s["equity"],
    "ledger.json":     lambda s: s["monitoring"]["ledger"],
    "outcomes.json":   lambda s: s["monitoring"]["outcomes"],
    "baselines.json":  lambda s: s["monitoring"]["baselines"],
    "mcp.json":        lambda s: s["mcp"],
    "regimes.json":    lambda s: {"regimes": s["regimes"]},
    "pipeline.json":   lambda s: {"pipeline": s["pipeline"], "cycles": s["cycles"]},
}


def _write(path: Path, payload: Any) -> tuple[Path, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=1, default=str, sort_keys=False)
    path.write_text(text)
    return path, len(text.encode())


def publish(store: Store, previous_digest: str | None = None) -> ExportResult:
    """
    Write the static site payload.

    Every file is redaction-checked BEFORE anything is written, so a leak aborts
    the publish rather than leaving a partial public directory behind.
    """
    snapshot = build_snapshot(store)

    # Check the whole payload, and each panel file, before touching the disk.
    assert_clean(snapshot)
    panels = {name: builder(snapshot) for name, builder in PANEL_FILES.items()}
    for name, payload in panels.items():
        try:
            assert_clean(payload)
        except RedactionError as exc:
            raise RedactionError(f"{name}: {exc}") from exc

    digest = material_digest(snapshot)
    result = ExportResult(
        digest=digest,
        changed=previous_digest is None or previous_digest != digest,
        generated_at=snapshot["meta"]["generated_at"],
        market_open=snapshot["meta"]["market_open"],
    )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    result.files.append(_write(DATA_DIR / "state.json", snapshot))
    for name, payload in panels.items():
        wrapped = {"meta": snapshot["meta"], **payload} if isinstance(payload, dict) else payload
        result.files.append(_write(DATA_DIR / name, wrapped))

    result.files.append(_write(DATA_DIR / "index.json", {
        "meta": snapshot["meta"],
        "digest": digest,
        "files": ["state.json", *PANEL_FILES.keys()],
    }))

    # The site itself: one index.html shared with the live server, plus a
    # config.js that selects the static data source.
    static_index = Path(__file__).resolve().parent / "static" / "index.html"
    result.files.append(_write_text(PUBLIC_DIR / "index.html", static_index.read_text()))
    result.files.append(_write_text(
        PUBLIC_DIR / "config.js",
        "// Generated by `cli.py publish`. Selects the static snapshot as the\n"
        "// data source; the local FastAPI server serves its own live version.\n"
        'window.OAA_MODE = "static";\n'
        'window.OAA_DATA_URL = "./data/state.json";\n'))
    result.files.append(_write_text(PUBLIC_DIR / "vercel.json", json.dumps({
        "$schema": "https://openapi.vercel.sh/vercel.json",
        "cleanUrls": True,
        "headers": [
            {"source": "/data/(.*)",
             "headers": [{"key": "Cache-Control",
                          "value": "public, max-age=0, must-revalidate"}]},
            {"source": "/(.*)",
             "headers": [{"key": "X-Content-Type-Options", "value": "nosniff"},
                         {"key": "Referrer-Policy", "value": "no-referrer"}]},
        ],
    }, indent=2)))
    return result


def _write_text(path: Path, text: str) -> tuple[Path, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path, len(text.encode())
