"""
Pytest configuration for orchestrator tests.

Adds orchestrator and shared directories to sys.path so that modules
can be imported with bare names (e.g., ``from models import Pipeline``).
"""

import sys
from pathlib import Path

# Project root
_project_root = Path(__file__).parent.parent.parent
_orchestrator_path = _project_root / "orchestrator"
_shared_path = _project_root / "shared"

for p in (_orchestrator_path, _shared_path):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))
