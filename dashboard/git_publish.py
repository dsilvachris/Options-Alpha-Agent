"""
Commit and push the published snapshot, only when it materially changed.

A scan cycle runs every 15 minutes. Committing each one would produce ~26
commits a session, almost all of them a changed timestamp and nothing else, so
the caller compares `material_digest()` (which excludes the volatile meta block)
and only commits on a real change.

Degrades quietly and loudly: if the working tree is not a git repository, or has
no remote, the publish still writes the files and the reason is logged. It never
raises into the scan loop.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import bootstrap  # noqa: F401  - must precede `logging.*` submodule imports
from config import ROOT
from logging.events import EventLog, Stage


@dataclass
class GitResult:
    committed: bool = False
    pushed: bool = False
    reason: str = ""
    commit: str = ""


def _run(args: list[str], cwd: Path = ROOT) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=120)
    except (subprocess.SubprocessError, OSError) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def is_repo() -> bool:
    return _run(["git", "rev-parse", "--is-inside-work-tree"])[0] == 0


def has_remote() -> bool:
    code, out = _run(["git", "remote"])
    return code == 0 and bool(out.strip())


def commit_and_push(paths: list[Path], message: str, events: EventLog | None = None,
                    push: bool = True) -> GitResult:
    """Stage the published files, commit, and push if a remote exists."""
    if not is_repo():
        result = GitResult(reason="not a git repository — files written, nothing committed")
    else:
        rel = [str(p.relative_to(ROOT)) for p in paths if p.is_relative_to(ROOT)]
        code, out = _run(["git", "add", "--", *rel])
        if code != 0:
            result = GitResult(reason=f"git add failed: {out[:200]}")
        else:
            code, out = _run(["git", "diff", "--cached", "--quiet"])
            if code == 0:
                result = GitResult(reason="no staged changes")
            else:
                code, out = _run(["git", "commit", "-m", message])
                if code != 0:
                    result = GitResult(reason=f"git commit failed: {out[:200]}")
                else:
                    sha = _run(["git", "rev-parse", "--short", "HEAD"])[1]
                    result = GitResult(committed=True, commit=sha, reason="committed")
                    if push and has_remote():
                        code, out = _run(["git", "push"])
                        result.pushed = code == 0
                        if code != 0:
                            result.reason = f"committed {sha}, push failed: {out[:200]}"
                    elif push:
                        result.reason = f"committed {sha}, no remote configured"

    if events is not None:
        events.emit(
            Stage.EXECUTION,
            f"PUBLISH — {result.reason}"
            + (f" ({result.commit})" if result.commit else ""),
            payload={"committed": result.committed, "pushed": result.pushed,
                     "reason": result.reason},
        )
    return result
