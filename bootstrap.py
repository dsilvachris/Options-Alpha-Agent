"""
Makes the spec-mandated `logging/` package importable in any import order.

docs/implementation.md Section 12 requires a top-level package named `logging`.
That name collides with Python's stdlib `logging`, and which one wins depends on
import order:

* If our package is touched first, `logging/__init__.py` loads the real stdlib
  module, installs it as `sys.modules["logging"]` and extends its `__path__` to
  cover this directory. Both namespaces then work.
* If anything imports stdlib `logging` first — `asyncio`, `dotenv` and `uvicorn`
  all do — then `sys.modules["logging"]` is the stdlib module with a `__path__`
  that does not include this repository, and `from logging.store import ...`
  fails with ModuleNotFoundError.

This module repairs the second case by appending our directory to the loaded
`logging` package's `__path__`. It is idempotent and safe to import repeatedly.

Import it before the first `from logging.<submodule> import ...` in any module
that could be an entry point:

    import bootstrap  # noqa: F401  - must precede `logging.*` submodule imports
"""
from __future__ import annotations

import logging as _stdlib_logging
import os as _os

_OURS = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "logging")

if hasattr(_stdlib_logging, "__path__") and _OURS not in list(_stdlib_logging.__path__):
    _stdlib_logging.__path__.append(_OURS)
