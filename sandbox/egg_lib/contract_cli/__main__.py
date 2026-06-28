#!/usr/bin/env python3
"""Entry point so ``egg-contract`` can be invoked as a path-style script.

``sandbox/bin/egg-contract`` symlinks to this file. When Python executes a
script by path, it puts the script's own directory
(``…/egg_lib/contract_cli/``) on ``sys.path[0]`` — that is not enough to
import the package as ``egg_lib.contract_cli`` (the barrel uses relative
imports). This thin wrapper prepends the sandbox root (two directories up
from this file: ``…/sandbox``) to ``sys.path`` and resolves ``main`` through
the package's re-export barrel, mirroring ``orch_cli/__main__.py`` and
``scripts/select_tests/__main__.py``.

Keeping the entry-point/path-fixup logic isolated here means the package
itself (``__init__.py`` + ``_*.py`` submodules) is consumed uniformly via
``import egg_lib.contract_cli`` and does not have to reason about
path-vs-package invocation forms.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Prepend the sandbox root (parent of ``egg_lib``) so ``egg_lib.contract_cli``
# resolves when this file is run directly via the ``egg-contract`` symlink.
# Path: …/sandbox/egg_lib/contract_cli/__main__.py -> parents[2] == …/sandbox
_SANDBOX_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _SANDBOX_ROOT not in sys.path:
    sys.path.insert(0, _SANDBOX_ROOT)

from egg_lib.contract_cli import main  # noqa: E402 — sys.path setup must precede import

if __name__ == "__main__":
    sys.exit(main())
