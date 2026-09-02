"""
Section 8.1 — Activity ledger.

Raw counts only. This module deliberately computes no ratios, percentages or
derived rates: over a five-session window the sample sizes are too small for a
rate to mean anything (Section 8.4), so the ledger reports what happened and
leaves interpretation to the reader.
"""
from __future__ import annotations

import bootstrap  # noqa: F401  - must precede `logging.*` submodule imports

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from evaluation.scorer import CHECK_NAMES
from logging.store import Store


@dataclass
class ActivityLedger:
    opportunities_by_underlying: dict[str, int] = field(default_factory=dict)
    decisions_by_state: dict[str, int] = field(default_factory=dict)
    rejection_reasons: list[tuple[str, int]] = field(default_factory=list)
    failed_checks: list[tuple[str, int]] = field(default_factory=list)
    watch_promotions: list[dict] = field(default_factory=list)
    total_decisions: int = 0
    #: Section 4.4 context — out-of-session decisions are priced off wide
    #: after-hours quotes and are counted, but not mixed into the totals above.
    session_only: bool = True
    out_of_session: int = 0
    unrecorded_session: int = 0

    def to_dict(self) -> dict:
        return {
            "opportunities_by_underlying": self.opportunities_by_underlying,
            "decisions_by_state": self.decisions_by_state,
            "rejection_reasons": [{"reason": r, "count": c} for r, c in self.rejection_reasons],
            "failed_checks": [{"check": c, "count": n} for c, n in self.failed_checks],
            "watch_promotions": self.watch_promotions,
            "total_decisions": self.total_decisions,
            "session_only": self.session_only,
            "out_of_session": self.out_of_session,
            "unrecorded_session": self.unrecorded_session,
        }

    def render(self) -> str:
        scope = "in-session only" if self.session_only else "all sessions"
        lines = [f"ACTIVITY LEDGER (Section 8.1) — raw counts, {scope}", ""]
        lines.append(f"Total decisions recorded: {self.total_decisions}")
        if self.out_of_session or self.unrecorded_session:
            lines.append(
                f"Excluded: {self.out_of_session} out-of-session, "
                f"{self.unrecorded_session} with no market state recorded")
        lines.append("")
        lines.append("Opportunities scanned, by underlying:")
        for symbol, count in sorted(self.opportunities_by_underlying.items()):
            lines.append(f"  {symbol:<6} {count}")
        lines.append("")
        lines.append("Decisions issued:")
        for state in ("TRADE", "WATCH", "REJECT", "EXPIRED"):
            lines.append(f"  {state:<8} {self.decisions_by_state.get(state, 0)}")
        lines.append("")
        lines.append("Rejection reasons, ranked by frequency:")
        if not self.rejection_reasons:
            lines.append("  (none)")
        for reason, count in self.rejection_reasons:
            lines.append(f"  {count:>4}  {reason}")
        lines.append("")
        lines.append("Checks most frequently failed:")
        if not self.failed_checks:
            lines.append("  (none)")
        for check, count in self.failed_checks:
            lines.append(f"  {count:>4}  {check}")
        lines.append("")
        lines.append("WATCH items promoted to TRADE:")
        if not self.watch_promotions:
            lines.append("  (none)")
        for item in self.watch_promotions:
            lines.append(
                f"  {item['symbol']:<6} {item['structure'] or '':<26} "
                f"{item['resolution'] or ''}"
            )
        return "\n".join(lines)


def _reason_bucket(reason: str) -> str:
    """Group free-text reasons into stable buckets for ranking."""
    text = (reason or "").lower()
    if "hard gate" in text:
        for cid, name in CHECK_NAMES.items():
            if name in text:
                return f"hard gate #{cid} {name}"
        return "hard gate (unspecified)"
    if "below the execution threshold" in text:
        return "score below execution threshold"
    if "moderate band" in text:
        return "score in moderate band"
    if "risk gate" in text or "circuit breaker" in text or "position sizing" in text:
        return "risk gate refusal"
    if "promoting condition not met" in text:
        return "WATCH expired"
    if "no structure" in text or "stand aside" in text:
        return "no eligible structure"
    return text[:60] if text else "unspecified"


def build(store: Store, session_only: bool = True) -> ActivityLedger:
    """
    Assemble the ledger from recorded decisions and watch items.

    Defaults to in-session decisions only. Out-of-session candidates are priced
    off wide after-hours quotes, so their rejection reasons and failed checks
    describe the spread, not the strategy.
    """
    decisions = store.all_decisions(session_only=session_only)
    counts = store.decision_session_counts()

    by_symbol = Counter(d["symbol"] for d in decisions)
    by_state = Counter(d["state"] for d in decisions)

    rejections = Counter(
        _reason_bucket(d.get("reason") or "")
        for d in decisions
        if d["state"] in {"REJECT", "EXPIRED"}
    )

    failed = Counter()
    for decision in decisions:
        detail = decision.get("detail")
        if not detail:
            continue
        try:
            parsed = json.loads(detail)
        except (TypeError, ValueError):
            continue
        for check in parsed.get("checks", []) or []:
            if not check.get("passed"):
                failed[f"#{check['id']} {check['name']}"] += 1

    promotions = [
        item for item in store.all_watch_items(limit=500)
        if item.get("status") == "PROMOTED"
    ]

    return ActivityLedger(
        opportunities_by_underlying=dict(sorted(by_symbol.items())),
        decisions_by_state=dict(by_state),
        rejection_reasons=rejections.most_common(),
        failed_checks=failed.most_common(),
        watch_promotions=promotions,
        total_decisions=len(decisions),
        session_only=session_only,
        out_of_session=counts["out_of_session"] if session_only else 0,
        unrecorded_session=counts["unrecorded"] if session_only else 0,
    )
