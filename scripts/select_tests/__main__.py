"""Entry point so the selector can be invoked as a path-style script.

Both the Makefile and the end-to-end test subprocesses run the
selector via ``python scripts/select_tests/__main__.py [args]``. When
Python loads a script that way, it puts the script's directory
(``scripts/select_tests/``) on ``sys.path[0]`` — that's not enough
to import the package as ``select_tests``, so this thin wrapper
prepends the parent ``scripts/`` directory to ``sys.path`` and then
resolves ``main`` through the package's re-export barrel.

Keeping the entry-point logic isolated to this file means the package
itself (``__init__.py`` + ``_*.py`` submodules) doesn't have to think
about path-vs-package invocation forms; the package is consumed
uniformly via ``import select_tests``.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Prepend ``scripts/`` (the parent of this package) to ``sys.path`` so
# the ``select_tests`` package import below resolves to the directory
# that contains this ``__main__.py``. Doing it before the import is
# required because path-style invocation only puts the script's own
# directory on ``sys.path[0]``.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from select_tests import main  # noqa: E402 — sys.path manipulation must come first

if __name__ == "__main__":
    sys.exit(main())
