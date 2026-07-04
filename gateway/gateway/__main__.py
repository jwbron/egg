"""Container / CLI entry point for the gateway package.

``gateway.py`` became the ``gateway/gateway/`` sub-package in #3312 slice-18.
The pre-split file was launched as a script (``python3 gateway.py``); a package
is launched with ``python3 -m gateway`` instead (see ``gateway/entrypoint.sh``),
which runs this module. It is a thin shim: the actual server bootstrap lives in
``main`` in the barrel (``__init__.py``), unchanged.
"""

from __future__ import annotations

from . import main

if __name__ == "__main__":
    main()
