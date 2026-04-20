"""Conftest for babysit-pr integration tests.

After #1748 the legacy ``shared/egg_babysit`` package is removed. The
babysit-pr workflow now lives as:

- The ``babysit_pr`` MCP tool in ``orchestrator.mcp_tools`` and the
  user-facing ``skills/babysit-pr/SKILL.md`` skill file.
- The ``POST /api/v1/pipelines`` route in ``orchestrator.routes.pipelines``
  which accepts ``mode=babysit`` and creates an implement-phase pipeline
  with ``has_contract=False``.
- The BRC (Broadcast-Review-Converge) consensus machinery in
  ``orchestrator.concurrent_executor``.

These integration tests exercise those surfaces end-to-end via the HTTP
route + MCP tool contract, with subprocess calls mocked.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Ensure orchestrator/ and shared/ are importable so the MCP tool module
# and route handler can be loaded without installing the package.
_repo_root = Path(__file__).resolve().parent.parent.parent
for _dir in ("orchestrator", "shared"):
    _p = str(_repo_root / _dir)
    if _p not in sys.path:
        sys.path.insert(0, _p)

# The orchestrator route handler imports ``docker`` at module load time;
# stub it out so these tests run in environments without the SDK installed.
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())
