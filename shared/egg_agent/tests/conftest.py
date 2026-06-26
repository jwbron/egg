"""Test configuration for the ``shared/egg_agent/tests`` root.

Ensures the repo's ``shared/`` directory is importable so
``egg_agent.midturn_messages`` (and siblings) resolve against the local tree
rather than any older copy installed at ``/opt/egg-runtime/shared/`` — the same
stance as ``shared/tests/conftest.py``, which does not cover this sibling root.

Collection note (#2270 slice-7, task-7-5): this directory is the contract-named
home for ``test_midturn_messages.py`` but is NOT yet listed in
``pyproject.toml::tool.pytest.ini_options.testpaths`` nor in the ``make test-all``
roots — both coder-owned. The slice-7 coder must add ``shared/egg_agent/tests``
to those collection roots (handed off via a tester→coder coverage gap) so this
suite runs under ``make test`` / ``make test-all`` / CI.
"""

import sys
from pathlib import Path

_shared_dir = str(Path(__file__).resolve().parents[2])
if _shared_dir not in sys.path[:3]:
    sys.path.insert(0, _shared_dir)
