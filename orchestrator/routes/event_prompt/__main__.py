"""Standalone wrapper-bash entry point for the per-event prompt composer.

The event-pump wrapper (``orchestrator/consensus_wrapper.py``) invokes
this file directly::

    python3 /opt/egg-runtime/orchestrator/routes/event_prompt/__main__.py <action>

run as a plain script so the heavy ``orchestrator.routes`` package
``__init__`` (which imports Flask) is bypassed — exactly the property the
pre-split ``routes/event_prompt.py`` standalone invocation relied on
(#3312 slice-6 decomposition preserves it).

Because the file is run as a script (``__name__ == "__main__"``) it has
no package context, so it cannot use relative imports. Instead it puts
the ``routes/`` directory (the parent of this package) on ``sys.path``
and imports the package by name — which loads only this sub-package's
barrel and its private submodules (all stdlib-only at import time), never
``routes/__init__``. The barrel re-exports :func:`_cli`; we call it and
propagate its exit code.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _main() -> int:
    # ``sys.path[0]`` when run as ``python3 .../event_prompt/__main__.py``
    # is this package directory; add its parent (``routes/``) so
    # ``import event_prompt`` resolves to this sub-package without pulling
    # in ``routes/__init__`` (Flask). Insert at the front so the package
    # name resolves here even if a same-named module sits later on the path.
    routes_dir = Path(__file__).resolve().parent.parent
    if str(routes_dir) not in sys.path:
        sys.path.insert(0, str(routes_dir))
    from event_prompt import _cli

    return _cli()


if __name__ == "__main__":  # pragma: no cover — wrapper-bash entry-point
    sys.exit(_main())
