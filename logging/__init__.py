"""
Section 7 / Section 6 logging package.

IMPORTANT — why this file looks unusual
---------------------------------------
docs/implementation.md Section 12 mandates a top-level package named `logging`.
Because the repository root is `sys.path[0]`, that name shadows Python's stdlib
`logging` for the whole process, which would break every third-party library
that calls `logging.getLogger()` (mcp, anyio, uvicorn, fastapi ...).

To honour the mandated layout without breaking the stdlib, this module loads the
real stdlib `logging` package from its own file location, installs it as
`sys.modules["logging"]`, and then extends its `__path__` to include this
directory. The result:

    import logging                 -> the genuine stdlib module
    import logging.handlers        -> stdlib submodule
    from logging.events import ... -> this package's submodule

Both namespaces coexist. Import our submodules explicitly (`logging.events`,
`logging.decision_card`, `logging.store`); never rely on names being re-exported
from the package root.
"""
import importlib.util as _importlib_util
import os as _os
import sys as _sys
import sysconfig as _sysconfig

_stdlib_dir = _os.path.join(_sysconfig.get_paths()["stdlib"], "logging")
_here = _os.path.dirname(_os.path.abspath(__file__))

_spec = _importlib_util.spec_from_file_location(
    __name__,
    _os.path.join(_stdlib_dir, "__init__.py"),
    submodule_search_locations=[_stdlib_dir, _here],
)
_module = _importlib_util.module_from_spec(_spec)
_sys.modules[__name__] = _module
_spec.loader.exec_module(_module)
# Search stdlib first, then this directory, so stdlib submodules always win.
_module.__path__ = [_stdlib_dir, _here]
