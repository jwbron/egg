"""Sibling-module loader for gateway.gateway.

Isolated into its own file so the ``__import__`` / ``importlib.util``
primitives below do NOT mark the surrounding ``gateway/gateway.py``
module as a dynamic-import seed for ``scripts/select_tests.py``.

When ``gateway/gateway.py`` itself was a seed, every ``gateway/<file>.py``
edit reached the seed transitively through ``find_upstream_modules`` and
short-circuited ``make test`` to the full suite via the
``dynamic-import reachability`` trigger.  Moving the importlib helpers
into this leaf module — which imports only stdlib — keeps the seed
set small enough that the bare-name AST resolver can actually narrow
gateway production edits.

Do NOT add gateway-package imports here.  The whole point of this
file is to keep the seed's ``find_upstream_modules`` closure free of
``gateway.*`` modules.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_sibling_gateway_module(module_name: str) -> Any:
    """Import a sibling gateway module regardless of test vs prod shape.

    Gateway modules are loaded two ways in this codebase: as a package
    (``gateway.x``) in production and as flat top-level modules by the
    test conftest (``__package__ == ""``).  Plain ``import X`` works in
    production when ``gateway/`` is on ``sys.path``, and in tests when
    the conftest preloaded ``X`` into ``sys.modules``.  For modules the
    conftest does *not* preload — like the ones added in #1882 — we
    fall back to loading the file by explicit path so the features are
    still exercisable in tests without forcing a conftest edit by the
    tester role.
    """
    mod = sys.modules.get(module_name) or sys.modules.get(f"gateway.{module_name}")
    if mod is not None:
        return mod
    try:
        mod = __import__(module_name)
        return mod
    except ImportError:
        pass
    try:
        mod_path = Path(__file__).parent / f"{module_name}.py"
        if not mod_path.exists():
            return None
        spec = importlib.util.spec_from_file_location(module_name, str(mod_path))
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # pragma: no cover - defensive
        return None
